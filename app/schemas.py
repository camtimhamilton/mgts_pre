"""Strict versioned contracts; numeric values are finite and missing is explicit."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
FactorID = Annotated[str, Field(pattern=r"^R(0[1-9]|10)$")]
Role = Literal["intern", "mentor"]
Severity = Literal["none", "moderate", "high"]


class FrozenDict(dict):
    """Serializable immutable configuration map, including nested maps."""

    def _immutable(self, *args, **kwargs):
        raise TypeError("immutable configuration")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable

    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)

    @model_validator(mode="after")
    def immutable_maps(self):
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, dict):
                object.__setattr__(self, name, freeze(value))
        return self


class TextAnswer(Model):
    type: Literal["text"]
    question_id: Identifier
    question_text: str = Field(min_length=1, max_length=2000)
    value: str | None = Field(max_length=12000)


class RatingAnswer(Model):
    type: Literal["rating"]
    question_id: Identifier
    question_text: str = Field(min_length=1, max_length=2000)
    value: Annotated[float, Field(strict=True)] | None
    scale_min: float = Field(strict=True)
    scale_max: float = Field(strict=True)

    @model_validator(mode="after")
    def valid_scale(self):
        if self.scale_min >= self.scale_max:
            raise ValueError("scale must have positive width")
        if self.value is not None and not self.scale_min <= self.value <= self.scale_max:
            raise ValueError("rating outside scale")
        return self


Answer = Annotated[TextAnswer | RatingAnswer, Field(discriminator="type")]


class Response(Model):
    response_id: Identifier
    respondent_role: Role
    mentor_id: Identifier | None = None
    submitted_at: datetime | None = None
    answers: tuple[Answer, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({a.question_id for a in self.answers}) != len(self.answers):
            raise ValueError("duplicate question_id")
        return self


class SurveyPeriod(Model):
    program_id: Identifier
    intern_id: Identifier
    period: int = Field(strict=True, ge=1, le=6)
    program_day: int | None = Field(default=None, strict=True, ge=1, le=366)
    context: dict[str, bool] = Field(default_factory=dict, max_length=1)
    mapping_version: str = "demo-1"
    knowledge_version: str = "0.1.0"
    responses: tuple[Response, ...] = Field(max_length=2)

    @model_validator(mode="after")
    def unique_responses(self):
        if set(self.context) - {"training_plan_confirmed"}:
            raise ValueError("unsupported context")
        if len({r.response_id for r in self.responses}) != len(self.responses):
            raise ValueError("duplicate response_id")
        if len({r.respondent_role for r in self.responses}) != len(self.responses):
            raise ValueError("backend must resolve response revisions per role")
        return self


class Source(Model):
    source_id: Identifier
    kind: Literal["paper", "book", "corporate_policy", "expert_interview", "author_demo"]
    title: str
    locator: str | None = None
    verification_status: Literal["reference_only", "author_demo", "verified"]


class SourceRef(Model):
    source_id: Identifier
    relationship: str


class Factor(Model):
    id: FactorID
    name: str
    definition: str
    boundary: str
    allowed_roles: tuple[Role, ...]


class Rule(Model):
    id: Annotated[str, Field(pattern=r"^RULE_R(0[1-9]|10)_\d{3}$")]
    revision: int = Field(ge=1)
    factor_id: FactorID
    name: str
    description: str
    applies_to: tuple[Role, ...]
    indicators: tuple[str, ...]
    exclusions: tuple[str, ...]
    severity: dict[Severity, str]
    action_ids: tuple[Identifier, ...]
    source_refs: tuple[SourceRef, ...]
    status: Literal["demo", "approved", "draft", "pending", "rejected"]


class Action(Model):
    id: Annotated[str, Field(pattern=r"^A\d{2}$")]
    name: str
    targets: tuple[FactorID, ...]
    description: str
    responsible: tuple[Literal["mentor", "hr", "intern", "team_lead", "it"], ...]
    completion_criteria: tuple[str, ...]
    resolution_criteria: tuple[str, ...]
    status: Literal["demo", "approved", "draft"]


class Policy(Model):
    version: str
    weights: dict[FactorID, int]
    coefficients: dict[Severity, float]
    routine_below: float
    high_from: float
    min_coverage: float = Field(ge=0, le=1)
    required_roles: tuple[Role, ...]
    weight_origin: Identifier

    @model_validator(mode="after")
    def valid_policy(self):
        if set(self.weights) != {f"R{i:02}" for i in range(1, 11)}:
            raise ValueError("ten fixed factors required")
        if any(w < 0 for w in self.weights.values()) or sum(self.weights.values()) != 100:
            raise ValueError("weights must be nonnegative and sum to 100")
        if self.coefficients != {"none": 0, "moderate": 0.5, "high": 1}:
            raise ValueError("unsupported coefficients")
        if not 0 <= self.routine_below < self.high_from <= 100:
            raise ValueError("invalid thresholds")
        return self


class KnowledgeSnapshot(Model):
    version: str
    status: Literal["demo", "approved"]
    sources: tuple[Source, ...]
    factors: tuple[Factor, ...]
    rules: tuple[Rule, ...]
    actions: tuple[Action, ...]
    policy: Policy
    content_hash: str


class MappingEntry(Model):
    role: Role
    question_id: Identifier
    type: Literal["rating", "text"]
    factor_id: FactorID | None = None
    comparison_key: str | None = None
    direction: Literal["higher_is_better", "higher_is_worse"] = "higher_is_better"
    scale_min: float | None = None
    scale_max: float | None = None
    levels: dict[int, Severity] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_mapping(self):
        if self.type == "rating":
            if self.factor_id is None or self.factor_id == "R10":
                raise ValueError("rating factor required; R10 cannot use ratings")
            if self.scale_min is None or self.scale_max is None or self.scale_min >= self.scale_max:
                raise ValueError("invalid mapping scale")
            expected = set(range(int(self.scale_min), int(self.scale_max) + 1))
            if self.scale_min % 1 or self.scale_max % 1 or set(self.levels) != expected:
                raise ValueError("explicit complete integer level mapping required")
        return self


class Mapping(Model):
    version: str
    entries: tuple[MappingEntry, ...]

    @model_validator(mode="after")
    def unique_keys(self):
        if len({(e.role, e.question_id) for e in self.entries}) != len(self.entries):
            raise ValueError("duplicate mapping key")
        return self


class Candidate(Model):
    factor_id: FactorID
    rule_id: str
    question_id: Identifier
    polarity: Literal["negative", "positive", "neutral"]
    severity: Severity
    evidence: str = Field(min_length=1, max_length=12000)
    assertion: Literal["explicit", "implicit", "ambiguous"]
    temporal_status: Literal["current", "historical", "resolved", "hypothetical", "unknown"]


class Fragment(Model):
    question_id: Identifier
    quote: str = Field(min_length=1, max_length=12000)
    reason: str = Field(min_length=1, max_length=500)


class LLMExtraction(Model):
    response_id: Identifier
    signals: tuple[Candidate, ...] = Field(max_length=100)
    unclassified_fragments: tuple[Fragment, ...] = Field(max_length=100)
    requires_review: bool


class ValidatedFragment(Fragment):
    response_id: Identifier
    role: Role


class ValidatedSignal(Model):
    signal_id: str
    response_id: Identifier
    role: Role
    question_id: Identifier
    factor_id: FactorID
    rule_id: str
    source_kind: Literal["text", "rating"]
    quote: str | None = None
    start: int | None = None
    end: int | None = None
    value: float | None = None
    scale_min: float | None = None
    scale_max: float | None = None
    mapping_id: str | None = None
    polarity: Literal["negative", "positive", "neutral"]
    severity: Severity
    assertion: Literal["explicit", "implicit", "ambiguous"]
    temporal_status: Literal["current", "historical", "resolved", "hypothetical", "unknown"]
    validation_status: Literal["accepted", "review", "rejected"]
    checks: tuple[str, ...]
    reason: str
    semantic_verification: Literal["not_run", "deterministic_mapping"]
    source_refs: tuple[SourceRef, ...] = ()


class FactorResult(Model):
    id: FactorID
    name: str
    state: Literal["active", "observed_no_signal", "unknown", "conflicting"]
    severity: Severity | None
    weight: int
    weight_origin: str
    contribution: float
    covered: bool
    missing_reason: str | None
    evidence: tuple[ValidatedSignal, ...]
    max_signal_ids: tuple[str, ...]


class Risk(Model):
    score: float | None
    max_score: int = 100
    score_band: Literal["routine", "elevated", "high"] | None
    priority: Literal["routine", "elevated", "high", "insufficient_data", "pending_review"]
    data_coverage: float
    coverage_status: Literal["complete", "incomplete"]
    critical_flags: tuple[str, ...]
    missing_factors: tuple[FactorID, ...]
    missing_required_roles: tuple[Role, ...]


class Recommendation(Model):
    id: str
    name: str
    target_factors: tuple[FactorID, ...]
    potential_reduction: float
    responsible: tuple[str, ...]
    completion_criteria: tuple[str, ...]
    resolution_criteria: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    status: Literal["proposed"] = "proposed"


class Discrepancy(Model):
    factor_id: FactorID
    comparison_key: str
    type: Literal["rating_gap"] = "rating_gap"
    intern_rating: float
    mentor_rating: float
    normalized_gap: float
    threshold_exceeded: bool


class StageEvent(Model):
    stage: str
    started_at: datetime
    ended_at: datetime
    elapsed_ms: float
    status: Literal["completed", "partial", "failed"]
    error_code: str | None = None
    input_ref: str
    output_ref: str
    output_count: int
    versions: dict[str, str]


class TokenUsage(Model):
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    completion_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_hit_tokens: int | None = Field(default=None, ge=0, strict=True)
    prompt_cache_miss_tokens: int | None = Field(default=None, ge=0, strict=True)


class ProviderAttempt(Model):
    attempt: int
    latency_ms: float
    http_status: int | None = None
    error_code: str | None = None
    model_id: str | None = None
    finish_reason: str | None = None
    usage: TokenUsage | None = None


class ProviderCall(Model):
    component: str
    provider: str
    requested_model: str
    model_id: str | None = None
    system_fingerprint: str | None = None
    model_identity_status: Literal["reported_alias", "reported_id", "not_reported"] = "not_reported"
    config_hash: str
    effective_prompt_hash: str
    schema_hash: str
    latency_ms: float
    attempts: tuple[ProviderAttempt, ...]
    error_code: str | None = None


class Metadata(Model):
    knowledge_version: str
    policy_version: str
    mapping_version: str
    prompt_version: str = "1"
    pipeline_version: str = "0.1.1"
    validation_version: str = "1"
    provider: Literal["mock", "deepseek"] = "mock"
    model_id: str = "fixture-v1"
    data_mode: Literal["demo"] = "demo"
    execution_mode: Literal["live"] = "live"
    profile: Literal["fast", "strict"]
    trace_id: str
    input_hash: str
    knowledge_hash: str
    mapping_hash: str
    prompt_hash: str
    extraction_hash: str
    analysis_key: str
    created_at: datetime
    latency_ms: float
    accepted_count: int
    rejected_count: int
    review_count: int
    provider_config: dict = Field(default_factory=dict)
    provider_config_hash: str | None = None
    provider_calls: tuple[ProviderCall, ...] = ()


class AnalysisResult(Model):
    analysis_id: str
    program_id: Identifier
    intern_id: Identifier
    period: int
    status: Literal["completed", "partial", "insufficient_data"]
    risk: Risk
    factors: tuple[FactorResult, ...]
    recommendations: tuple[Recommendation, ...]
    discrepancies: tuple[Discrepancy, ...]
    signals: tuple[ValidatedSignal, ...]
    unclassified_fragments: tuple[ValidatedFragment, ...]
    warnings: tuple[str, ...]
    capabilities: dict[str, bool]
    stages: tuple[StageEvent, ...]
    metadata: Metadata


class ScenarioRequest(Model):
    analysis_id: Identifier
    action_ids: tuple[Annotated[str, Field(pattern=r"^A\d{2}$")], ...] = Field(max_length=50)


class ScenarioResult(Model):
    analysis_id: str
    current_score: float
    scenario_score: float
    potential_reduction: float
    covered_factor_ids: tuple[FactorID, ...]
    factor_reductions: dict[FactorID, float]
    assumption: Literal["full_resolution"] = "full_resolution"
