"""Pure domain functions. No provider, HTTP, filesystem or persistence dependencies."""
from collections import defaultdict

from app.serialization import digest
from app.schemas import (
    AnalysisResult, Candidate, Discrepancy, FactorResult, KnowledgeSnapshot, Mapping,
    Recommendation, Response, Risk, ScenarioRequest, ScenarioResult, SurveyPeriod,
    ValidatedSignal,
)


def validate_candidate(candidate: Candidate, response: Response, kb: KnowledgeSnapshot,
                       profile: str = "fast") -> ValidatedSignal:
    """V2/V3 provenance, then conservative eligibility. V4 is explicitly not run."""
    rule = next((r for r in kb.rules if r.id == candidate.rule_id), None)
    answer = next((a for a in response.answers if a.question_id == candidate.question_id), None)
    status, reason, checks = "accepted", "provisional_text_classification", ["V1"]
    start = end = None
    if rule is None or rule.status != kb.status or rule.factor_id != candidate.factor_id:
        status, reason = "rejected", "invalid_rule_reference"
    elif response.respondent_role not in rule.applies_to:
        status, reason = "rejected", "role_not_allowed"
    elif answer is None or answer.type != "text" or not answer.value:
        status, reason = "rejected", "invalid_evidence_answer"
    else:
        checks.append("V2")
        quote = candidate.evidence
        if not quote.strip() or quote not in answer.value:
            status, reason = "rejected", "quote_not_found"
        else:
            checks.append("V3")
            start = answer.value.index(quote)
            end = start + len(quote)
            # Overlapping repeated occurrences count too.
            if candidate.temporal_status in {"historical", "resolved", "hypothetical"}:
                status, reason = "rejected", "not_current"
            elif candidate.polarity != "negative" and candidate.severity != "none":
                status, reason = "rejected", "polarity_severity_mismatch"
            elif candidate.assertion != "explicit" or candidate.temporal_status == "unknown":
                status, reason = "review", "uncertain_assertion_or_time"
            elif answer.value.find(quote, start + 1) >= 0:
                status, reason, start, end = "review", "ambiguous_quote_occurrence", None, None
            elif candidate.factor_id == "R10" and candidate.polarity == "negative":
                status, reason = "review", "critical_requires_confirmation"
            elif profile == "strict":
                status, reason = "review", "independent_semantic_review_not_available"
    fields = candidate.model_dump(exclude={"evidence"})
    return ValidatedSignal(
        **fields, signal_id="s_" + digest([response.response_id, candidate.model_dump()])[:24],
        response_id=response.response_id, role=response.respondent_role, source_kind="text",
        quote=candidate.evidence, start=start, end=end, validation_status=status,
        checks=tuple(checks), reason=reason, semantic_verification="not_run",
        source_refs=rule.source_refs if rule else (),
    )


def rating_signals(survey: SurveyPeriod, mapping: Mapping) -> tuple[list[ValidatedSignal], list[str]]:
    signals, warnings = [], []
    entries = {(e.role, e.question_id): e for e in mapping.entries}
    for response in survey.responses:
        for answer in response.answers:
            if answer.type != "rating" or answer.value is None:
                continue
            entry = entries.get((response.respondent_role, answer.question_id))
            if entry is None or entry.type != "rating":
                warnings.append("unmapped_rating")
                continue
            if (entry.scale_min, entry.scale_max) != (answer.scale_min, answer.scale_max):
                warnings.append("rating_scale_mismatch")
                continue
            if answer.value not in entry.levels:
                warnings.append("unmapped_rating_value")
                continue
            severity = entry.levels[answer.value]
            signals.append(ValidatedSignal(
                signal_id="s_" + digest([response.response_id, answer.model_dump(), mapping.version])[:24],
                response_id=response.response_id, role=response.respondent_role,
                question_id=answer.question_id, factor_id=entry.factor_id,
                rule_id=f"MAPPING_{mapping.version}_{entry.role}_{entry.question_id}",
                source_kind="rating", value=answer.value, scale_min=answer.scale_min,
                scale_max=answer.scale_max, mapping_id=f"{mapping.version}:{entry.role}:{entry.question_id}",
                polarity="negative" if severity != "none" else "neutral", severity=severity,
                assertion="explicit", temporal_status="current", validation_status="accepted",
                checks=("V1", "V2", "numeric_mapping"), reason="configured_rating_mapping",
                semantic_verification="deterministic_mapping",
            ))
    return signals, warnings


def aggregate(signals: list[ValidatedSignal], kb: KnowledgeSnapshot) -> tuple[FactorResult, ...]:
    # One quote used for several negative factors lacks independent semantic justification.
    overlaps = set()
    candidates = [s for s in signals if s.source_kind == "text" and s.validation_status == "accepted"
                  and s.polarity == "negative" and s.start is not None and s.end is not None]
    for i, a in enumerate(candidates):
        for b in candidates[i + 1:]:
            if (a.factor_id != b.factor_id and a.response_id == b.response_id and a.question_id == b.question_id
                    and a.start < b.end and b.start < a.end):
                overlaps.update((a.signal_id, b.signal_id))
    prepared = []
    for signal in signals:
        if signal.signal_id in overlaps:
            signal = signal.model_copy(update={"validation_status": "review", "reason": "cross_factor_overlap"})
        prepared.append(signal)
    unique = {s.signal_id: s for s in prepared}
    factors = []
    for factor in kb.factors:
        evidence = tuple(s for s in unique.values() if s.factor_id == factor.id)
        accepted = [s for s in evidence if s.validation_status == "accepted"]
        negative = [s for s in accepted if s.polarity == "negative" and s.assertion == "explicit"
                    and s.temporal_status == "current"]
        severity = max((s.severity for s in negative), key=kb.policy.coefficients.get, default="none")
        covered = bool(accepted)
        level = kb.policy.coefficients[severity]
        conflict = bool(negative and any(s.polarity == "positive" or (s.source_kind == "rating" and s.severity == "none")
                                         for s in accepted))
        factors.append(FactorResult(
            id=factor.id, name=factor.name,
            state="conflicting" if conflict else "active" if level else "observed_no_signal" if covered else "unknown",
            severity=severity if covered else None, weight=kb.policy.weights[factor.id],
            weight_origin=kb.policy.weight_origin, contribution=kb.policy.weights[factor.id] * level,
            covered=covered, missing_reason=None if covered else "no_accepted_factor_specific_evidence",
            evidence=evidence, max_signal_ids=tuple(s.signal_id for s in negative if s.severity == severity),
        ))
    return tuple(factors)


def score(factors: tuple[FactorResult, ...], kb: KnowledgeSnapshot, survey: SurveyPeriod,
          incomplete: bool = False, review_required: bool = False) -> Risk:
    coverage = sum(f.weight for f in factors if f.covered) / sum(f.weight for f in factors)
    meaningful = any(f.covered for f in factors)
    observed = sum(f.contribution for f in factors) if meaningful else None
    band = ("high" if observed >= kb.policy.high_from else
            "elevated" if observed >= kb.policy.routine_below else "routine") if observed is not None else None
    # A submitted empty response is not a meaningful required source.
    roles = {s.role for f in factors for s in f.evidence if s.validation_status == "accepted"}
    missing_roles = tuple(r for r in kb.policy.required_roles if r not in roles)
    critical = any(s.factor_id == "R10" and s.validation_status == "review" and "V3" in s.checks
                   and s.polarity == "negative" and s.temporal_status == "current" and s.assertion == "explicit"
                   for f in factors for s in f.evidence)
    flags = ("R10_pending_confidential_review",) if critical else ()
    complete = coverage >= kb.policy.min_coverage and not missing_roles and not incomplete
    needs_review = review_required or any(f.state == "conflicting" or
                                       any(s.validation_status != "accepted" for s in f.evidence) for f in factors)
    priority = "pending_review" if critical else "insufficient_data" if not complete or needs_review else band
    return Risk(score=observed, score_band=band, priority=priority or "insufficient_data", data_coverage=coverage,
                coverage_status="complete" if complete else "incomplete", critical_flags=flags,
                missing_factors=tuple(f.id for f in factors if not f.covered), missing_required_roles=missing_roles)


def recommendations(factors: tuple[FactorResult, ...], kb: KnowledgeSnapshot) -> tuple[Recommendation, ...]:
    active = {f.id: f for f in factors if f.contribution > 0}
    result = []
    for action in kb.actions:
        targets = tuple(f for f in action.targets if f in active)
        if action.status != kb.status or not targets:
            continue
        result.append(Recommendation(
            id=action.id, name=action.name, target_factors=targets,
            potential_reduction=sum(active[f].contribution for f in targets), responsible=action.responsible,
            completion_criteria=action.completion_criteria, resolution_criteria=action.resolution_criteria,
            evidence_refs=tuple(s for f in targets for s in active[f].max_signal_ids),
        ))
    return tuple(result)


def scenario(analysis: AnalysisResult, request: ScenarioRequest) -> ScenarioResult:
    if request.analysis_id != analysis.analysis_id:
        raise ValueError("snapshot_id_mismatch")
    if analysis.risk.score is None:
        raise ValueError("scenario_unavailable_without_score")
    actions = {a.id: a for a in analysis.recommendations}
    if not set(request.action_ids) <= actions.keys():
        raise KeyError("action_not_available")
    targets = sorted({f for action in request.action_ids for f in actions[action].target_factors})
    reductions = {f.id: f.contribution for f in analysis.factors if f.id in targets}
    reduction = sum(reductions.values())
    return ScenarioResult(analysis_id=analysis.analysis_id, current_score=analysis.risk.score,
                          scenario_score=analysis.risk.score - reduction, potential_reduction=reduction,
                          covered_factor_ids=tuple(targets), factor_reductions=reductions)


def discrepancies(survey: SurveyPeriod, mapping: Mapping) -> tuple[tuple[Discrepancy, ...], tuple[str, ...]]:
    entries = {(e.role, e.question_id): e for e in mapping.entries}
    pairs = defaultdict(lambda: defaultdict(list))
    for response in survey.responses:
        for answer in response.answers:
            entry = entries.get((response.respondent_role, answer.question_id))
            if (answer.type != "rating" or answer.value is None or entry is None or entry.type != "rating"
                    or not entry.comparison_key or (answer.scale_min, answer.scale_max) != (entry.scale_min, entry.scale_max)):
                continue
            pairs[entry.comparison_key][response.respondent_role].append((answer, entry))
    results, warnings = [], []
    for key, roles in pairs.items():
        if set(roles) != {"intern", "mentor"}:
            warnings.append("rating_pair_missing")
            continue
        if any(len(values) != 1 for values in roles.values()):
            warnings.append("rating_pair_not_comparable")
            continue
        a, ea = roles["intern"][0]
        b, eb = roles["mentor"][0]
        if ea.factor_id != eb.factor_id:
            warnings.append("rating_pair_not_comparable")
            continue
        def normalize(answer, entry):
            value = (answer.value - answer.scale_min) / (answer.scale_max - answer.scale_min)
            return value if entry.direction == "higher_is_better" else 1 - value
        gap = abs(normalize(a, ea) - normalize(b, eb))
        results.append(Discrepancy(factor_id=ea.factor_id, comparison_key=key, intern_rating=a.value,
                                  mentor_rating=b.value, normalized_gap=gap, threshold_exceeded=gap >= 0.5))
    return tuple(results), tuple(warnings)
