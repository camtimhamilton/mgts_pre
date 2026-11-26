"""Isolated, evidence-grounded KB proposals and explicit human review artifacts."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.knowledge import ROOT
from app.pair_analytics import load_policy
from app.serialization import canonical, digest


class ProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class SourceVerification(ProposalModel):
    reviewer: str = Field(min_length=1, max_length=160, pattern=r"\S")
    reviewed_at: AwareDatetime
    basis: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class ProposalSource(ProposalModel):
    source_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
    classification: Literal["verified_scientific", "corporate", "expert_project_assumption", "demo"]
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(min_length=1, max_length=100000)
    quote: str = Field(min_length=1, max_length=12000)
    locator: str | None = Field(default=None, max_length=2000)
    verification: SourceVerification | None = None

    @model_validator(mode="after")
    def evidence_and_classification(self):
        if not self.quote.strip() or self.quote not in self.content:
            raise ValueError("quote_must_belong_to_source")
        if self.classification == "verified_scientific" and (not self.locator or self.verification is None):
            raise ValueError("scientific_source_requires_human_verification_metadata")
        return self


class ProposalTarget(ProposalModel):
    role: Literal["mentor", "intern"]
    factor_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    action_id: str = Field(pattern=r"^action_(mentor|intern|pair)_[a-z][a-z0-9_]*$")


class ProposedRule(ProposalModel):
    title: str = Field(min_length=1, max_length=500)
    criterion: str = Field(min_length=1, max_length=2000)
    quote: str = Field(min_length=1, max_length=12000)
    moderate: str = Field(min_length=1, max_length=2000)
    high: str = Field(min_length=1, max_length=2000)
    exclusions: list[str] = Field(min_length=1, max_length=30)


class ProposalInput(ProposalModel):
    source: ProposalSource
    target: ProposalTarget
    proposal: ProposedRule

    @model_validator(mode="after")
    def exact_proposal_quote(self):
        if self.proposal.quote != self.source.quote:
            raise ValueError("proposal_quote_must_equal_selected_source_quote")
        return self


class ExtractionResult(ProposalModel):
    status: Literal["proposed", "abstain"]
    proposal: ProposedRule | None

    @model_validator(mode="after")
    def consistent_status(self):
        if (self.status == "proposed") != (self.proposal is not None):
            raise ValueError("invalid_extraction_status")
        return self


class HumanReview(ProposalModel):
    reviewer: str = Field(min_length=1, max_length=160, pattern=r"\S")
    decision: Literal["approved", "rejected"]
    reviewed_at: AwareDatetime
    note: str = Field(min_length=1, max_length=3000, pattern=r"\S")
    reviewed_draft_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProposalArtifact(ProposalModel):
    schema_version: Literal["rule-proposal-v1"] = "rule-proposal-v1"
    proposal_id: str
    artifact_version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    status: Literal["draft", "approved", "rejected"]
    active: Literal[False] = False
    runtime_activation: Literal["disabled"] = "disabled"
    base_policy_version: str
    base_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: ProposalInput
    patch: dict
    generation: dict
    review: HumanReview | None = None
    audit: list[dict]
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def integrity_and_review(self):
        if digest(self.model_dump(mode="json", exclude={"artifact_hash"})) != self.artifact_hash:
            raise ValueError("artifact_integrity_mismatch")
        if digest(self.input.source.model_dump(mode="json")) != self.source_hash:
            raise ValueError("source_integrity_mismatch")
        if self.status == "draft" and self.review is not None:
            raise ValueError("draft_cannot_contain_human_approval")
        if self.status != "draft" and (self.review is None or self.review.decision != self.status):
            raise ValueError("review_metadata_required")
        return self


def _validate_target(body: ProposalInput, policy: dict) -> str:
    target = body.target
    if target.factor_id not in policy["roles"][target.role]:
        raise ValueError("unknown_role_factor")
    expected = {f"action_{target.role}_{target.factor_id}"}
    if all(target.factor_id in factors for factors in policy["roles"].values()):
        expected.add(f"action_pair_{target.factor_id}")
    if target.action_id not in expected or target.factor_id not in policy["actions"]:
        raise ValueError("action_does_not_target_role_factor")
    rule_id = f"rule_{target.role}_{target.factor_id}"
    if rule_id not in policy["rules"]:
        raise ValueError("active_rule_reference_missing")
    return rule_id


def _patch(body: ProposalInput, policy: dict) -> dict:
    return {"operation": "propose_rule_revision", "base_rule_id": _validate_target(body, policy),
            "target": body.target.model_dump(), "source_id": body.source.source_id,
            "source_classification": body.source.classification, "rule": body.proposal.model_dump()}


def _artifact(payload: dict) -> ProposalArtifact:
    payload["artifact_hash"] = digest(payload)
    return ProposalArtifact.model_validate(payload)


def create_draft(body: ProposalInput, author: str, *, generation: dict | None = None) -> ProposalArtifact:
    if not author.strip():
        raise ValueError("author_required")
    policy = load_policy()
    patch = _patch(body, policy)
    proposal_id = digest([body.model_dump(mode="json"), digest(policy)])[:24]
    payload = {"schema_version": "rule-proposal-v1", "proposal_id": proposal_id,
        "artifact_version": f"draft-{proposal_id}", "status": "draft", "active": False,
        "runtime_activation": "disabled", "base_policy_version": policy["version"],
        "base_policy_hash": digest(policy), "source_hash": digest(body.source.model_dump(mode="json")),
        "input": body.model_dump(mode="json"), "patch": patch,
        "generation": generation or {"mode": "manual_local"}, "review": None,
        "audit": [{"event": "draft_created", "actor": author, "at": datetime.now(timezone.utc).isoformat(),
                   "input_hash": digest(body.model_dump(mode="json")), "patch_hash": digest(patch)}]}
    return _artifact(payload)


def review_draft(draft: ProposalArtifact, reviewer: str, decision: Literal["approved", "rejected"],
                 version: str, note: str) -> ProposalArtifact:
    draft = ProposalArtifact.model_validate(draft.model_dump(mode="json"))
    if draft.status != "draft":
        raise ValueError("only_draft_can_be_reviewed")
    policy = load_policy()
    if draft.base_policy_hash != digest(policy):
        raise ValueError("stale_base_policy")
    if draft.patch != _patch(draft.input, policy):
        raise ValueError("patch_does_not_match_grounded_input")
    if version == draft.artifact_version or version == policy["version"]:
        raise ValueError("review_requires_separate_artifact_version")
    review = HumanReview(reviewer=reviewer, decision=decision, reviewed_at=datetime.now(timezone.utc),
                         note=note, reviewed_draft_hash=draft.artifact_hash)
    payload = draft.model_dump(mode="json", exclude={"artifact_hash"})
    payload.update(status=decision, artifact_version=version, review=review.model_dump(mode="json"))
    payload["audit"].append({"event": "human_review", "actor": reviewer, "decision": decision,
                             "at": review.reviewed_at.isoformat(), "input_hash": draft.artifact_hash,
                             "patch_hash": digest(draft.patch)})
    return _artifact(payload)


def write_artifact(path: Path, artifact: ProposalArtifact) -> None:
    artifact = ProposalArtifact.model_validate(artifact.model_dump(mode="json"))
    target = path.resolve()
    protected = [ROOT / name for name in ("app", "knowledge_base", "prompts", "mappings")]
    protected.append(ROOT.parent / "app")
    if target.suffix.lower() != ".json" or any(target.is_relative_to(root.resolve()) for root in protected):
        raise ValueError("proposal_output_must_be_a_separate_json_artifact")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8") as stream:
        stream.write(canonical(artifact.model_dump(mode="json")) + "\n")


async def extract_synthetic(author: str) -> ProposalArtifact:
    """Optional inference on one pinned synthetic source through rule_extractor profile."""
    from app.deepseek import DeepSeekProvider
    from app.llm import ProviderError
    body = ProposalInput.model_validate_json((ROOT / "tests/fixtures/rule_proposal_synthetic.json").read_text("utf-8"))
    if digest(body.model_dump(mode="json")) != SYNTHETIC_INPUT_HASH:
        raise ValueError("reviewed_synthetic_source_integrity_mismatch")
    prompt = (ROOT / "prompts/rule_proposal_v1.md").read_text("utf-8")
    provider = DeepSeekProvider(component="rule_extractor")
    result = await provider.generate_structured(prompt, canonical({"source": body.source.model_dump(mode="json"),
        "target": body.target.model_dump()}), ExtractionResult.model_json_schema())
    extraction = ExtractionResult.model_validate_json(result.raw)
    if extraction.status == "abstain":
        raise ProviderError("rule_extractor_abstained")
    candidate = ProposalInput(source=body.source, target=body.target, proposal=extraction.proposal)
    return create_draft(candidate, author, generation={"mode": "deepseek_reviewed_synthetic",
        "prompt_hash": digest(prompt), "provider_call": result.telemetry.model_dump(mode="json") if result.telemetry else None})


SYNTHETIC_INPUT_HASH = "79e3267fb4316360eb6ab7f7e0f368389b7db6b902cc0598337274286ddc155a"
