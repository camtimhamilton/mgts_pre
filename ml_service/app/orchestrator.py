"""Wiring and safe tracing, independent of HTTP."""
import asyncio
import logging
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4

from app.core import aggregate, discrepancies, rating_signals, recommendations, score, validate_candidate
from app.knowledge import ROOT, digest
from app.llm import LLMProvider, ProviderError, build_user_prompt, parse_extraction
from app.schemas import AnalysisResult, KnowledgeSnapshot, LLMExtraction, Mapping, Metadata, StageEvent, SurveyPeriod, ValidatedFragment
from app.storage import SnapshotRepository
from app.synthetic import SyntheticInputPolicy

logger = logging.getLogger("mgts.pipeline")
CAPABILITIES = {"rating_analysis": True, "fixture_text_extraction": True, "numeric_gap": True,
    "immutable_snapshots": True, "scenario": True, "strict_semantic_verification": False,
    "real_provider": False, "replay": False, "temporal": False, "semantic_discrepancy": False,
    "rule_extractor": False, "batch": False}


def now():
    return datetime.now(timezone.utc)


class Pipeline:
    def __init__(self, kb: KnowledgeSnapshot, mapping: Mapping, provider: LLMProvider,
                 repository: SnapshotRepository):
        self.kb = kb
        self.mapping = mapping
        self.provider = provider
        self.provider_name = getattr(provider, "name", "mock")
        if self.provider_name not in {"mock", "deepseek"}:
            raise ValueError("unsupported_llm_provider")
        self.provider_config = getattr(provider, "configuration", {})
        self.capabilities = {**CAPABILITIES, "real_provider": self.provider_name == "deepseek",
                             "fixture_text_extraction": self.provider_name == "mock"}
        self.synthetic_policy = SyntheticInputPolicy() if self.provider_name == "deepseek" else None
        self.repository = repository
        self.prompt = (ROOT / "prompts" / "signal_extractor_v1.md").read_text(encoding="utf-8")
        # Bound pending analyses; each admitted analysis performs at most two provider calls.
        self.slots = asyncio.Semaphore(4)

    async def analyze(self, survey: SurveyPeriod, profile: str = "fast", trace_id: str | None = None) -> AnalysisResult:
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=0.1)
        except TimeoutError:
            raise ProviderError("service_busy") from None
        try:
            return await self._analyze(survey, profile, trace_id or "trace_" + uuid4().hex)
        finally:
            self.slots.release()

    async def _analyze(self, survey: SurveyPeriod, profile: str, trace_id: str) -> AnalysisResult:
        if survey.knowledge_version != self.kb.version or survey.mapping_version != self.mapping.version:
            raise ValueError("version_conflict")
        if self.synthetic_policy is not None:
            self.synthetic_policy.require_allowed(survey)
        started, tick = now(), perf_counter()
        events, warnings, artifacts, fragments, provider_calls = [], [], [], [], []
        input_hash = digest(survey.model_dump(mode="json"))

        def event(stage, begin, timer, status, count, code=None):
            item = StageEvent(stage=stage, started_at=begin, ended_at=now(),
                elapsed_ms=round((perf_counter() - timer) * 1000, 3), status=status,
                error_code=code, input_ref="normalized_input", output_ref=stage + "_artifacts", output_count=count,
                versions={"knowledge": self.kb.version, "policy": self.kb.policy.version,
                          "mapping": self.mapping.version, "pipeline": "0.1.1", "validation": "1"})
            events.append(item)
            logger.info("stage=%s status=%s trace_id=%s code=%s count=%d", stage, status, trace_id, code or "none", count)

        begin, timer = now(), perf_counter()
        signals, numeric_warnings = rating_signals(survey, self.mapping)
        warnings.extend(numeric_warnings)
        failures = bool(numeric_warnings)
        event("rating", begin, timer, "partial" if numeric_warnings else "completed", len(signals),
              "rating_mapping_incomplete" if numeric_warnings else None)
        requires_review = False
        for response in survey.responses:
            if not any(a.type == "text" and a.value and a.value.strip() for a in response.answers):
                continue
            begin, timer = now(), perf_counter()
            user_prompt = build_user_prompt(survey, response, self.kb.content_hash,
                [r.model_dump(mode="json") for r in self.kb.rules if r.status == self.kb.status])
            call_telemetry = None
            try:
                # Transport retries live in adapter, never retry domain/schema validation.
                raw = await asyncio.wait_for(self.provider.generate_structured(
                    self.prompt, user_prompt, LLMExtraction.model_json_schema()),
                    timeout=getattr(self.provider, "total_timeout_seconds", 10) + 1)
                call_telemetry = raw.telemetry
                extraction = parse_extraction(raw, response, self.provider_name)
                if call_telemetry is not None:
                    provider_calls.append(call_telemetry)
                artifacts.append({"request_hash": digest([self.prompt, user_prompt, LLMExtraction.model_json_schema(), profile]),
                    "provider": raw.provider, "model_id": raw.model_id,
                    "config_hash": digest(self.provider_config),
                    "effective_prompt_hash": call_telemetry.effective_prompt_hash if call_telemetry else digest(self.prompt),
                    "payload": extraction.model_dump(mode="json")})
                signals.extend(validate_candidate(candidate, response, self.kb, profile) for candidate in extraction.signals)
                fragments.extend(ValidatedFragment(**f.model_dump(), response_id=response.response_id,
                                                  role=response.respondent_role) for f in extraction.unclassified_fragments)
                requires_review |= extraction.requires_review or bool(extraction.unclassified_fragments)
                event("text_extraction_validation", begin, timer, "completed", len(extraction.signals))
            except (ProviderError, TimeoutError) as error:
                failures = True
                code = error.code if isinstance(error, ProviderError) else "provider_timeout"
                failed_call = (error.telemetry if isinstance(error, ProviderError) else None) or call_telemetry
                if failed_call is not None:
                    provider_calls.append(failed_call.model_copy(update={"error_code": code}))
                warnings.append(code)
                event("text_extraction_validation", begin, timer, "failed", 0, code)
        begin, timer = now(), perf_counter()
        factors = aggregate(signals, self.kb)
        # Aggregator may mark cross-factor overlaps for review; expose final dispositions everywhere.
        final_signals = tuple(s for f in factors for s in f.evidence)
        risk = score(factors, self.kb, survey, failures, requires_review)
        if failures and risk.score is None and not risk.critical_flags and not final_signals and not fragments:
            raise ProviderError("no_usable_analysis")
        gaps, gap_warnings = discrepancies(survey, self.mapping)
        warnings.extend(gap_warnings)
        if risk.data_coverage < self.kb.policy.min_coverage:
            warnings.append("insufficient_factor_coverage")
        if risk.missing_required_roles:
            warnings.append("missing_required_roles")
        if any(s.source_kind == "text" and s.validation_status == "accepted" for s in final_signals):
            warnings.append("provisional_text_semantics_not_verified")
        if risk.priority == "pending_review" or requires_review or any(s.validation_status != "accepted" for s in final_signals):
            warnings.append("review_required")
        if any(f.state == "conflicting" for f in factors):
            warnings.append("conflicting_reports_require_review")
        if profile == "strict":
            warnings.append("strict_semantic_verifier_not_implemented")
        event("aggregate_score_recommend", begin, timer, "completed", len(factors))
        extraction_hash = digest(artifacts)
        mapping_hash = digest(self.mapping.model_dump(mode="json"))
        prompt_hash = digest(self.prompt)
        key = digest([input_hash, self.kb.content_hash, mapping_hash, prompt_hash, extraction_hash,
                      digest(self.provider_config), self.provider_name, profile, "0.1.1", "V1"])
        result = AnalysisResult(
            analysis_id="analysis_" + uuid4().hex, program_id=survey.program_id, intern_id=survey.intern_id,
            period=survey.period, status="partial" if failures else "insufficient_data" if risk.score is None else "completed",
            risk=risk, factors=factors, recommendations=recommendations(factors, self.kb), discrepancies=gaps,
            signals=final_signals, unclassified_fragments=tuple(fragments), warnings=tuple(sorted(set(warnings))),
            capabilities=self.capabilities, stages=tuple(events), metadata=Metadata(
                knowledge_version=self.kb.version, policy_version=self.kb.policy.version,
                mapping_version=self.mapping.version, profile=profile, trace_id=trace_id, input_hash=input_hash,
                provider=self.provider_name, model_id=next((c.model_id for c in reversed(provider_calls) if c.model_id),
                                                         getattr(self.provider, "model_id", "fixture-v1")),
                provider_config=self.provider_config, provider_config_hash=digest(self.provider_config),
                provider_calls=tuple(provider_calls),
                knowledge_hash=self.kb.content_hash, mapping_hash=mapping_hash, prompt_hash=prompt_hash,
                extraction_hash=extraction_hash, analysis_key=key, created_at=started,
                latency_ms=round((perf_counter() - tick) * 1000, 3),
                accepted_count=sum(s.validation_status == "accepted" for s in final_signals),
                rejected_count=sum(s.validation_status == "rejected" for s in final_signals),
                review_count=sum(s.validation_status == "review" for s in final_signals)))
        self.repository.save(result)
        return result
