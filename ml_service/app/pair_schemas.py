"""Version two survey and scope contracts."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AnalyticsScope(Contract):
    type: Literal["GLOBAL", "PAIR_MONTH", "PAIR_RANGE"] = "GLOBAL"
    pair_id: str | None = None
    start: int | None = Field(default=None, ge=1, le=6)
    end: int | None = Field(default=None, ge=1, le=6)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.type == "GLOBAL":
            if any(value is not None for value in (self.pair_id, self.start, self.end)):
                raise ValueError("global scope has no pair or period")
            return self
        if not self.pair_id or self.start is None:
            raise ValueError("pair scope requires pair_id and start")
        if self.end is None:
            self.end = self.start
        if self.start > self.end:
            self.start, self.end = self.end, self.start
        if self.type == "PAIR_MONTH" and self.start != self.end:
            raise ValueError("month scope requires one month")
        return self


class SurveyRecord(Contract):
    pair_id: str = Field(min_length=1, max_length=160)
    mentor_id: str = Field(min_length=1, max_length=100)
    intern_id: str = Field(min_length=1, max_length=100)
    department: str = Field(default="", max_length=500)
    month: int = Field(ge=1, le=6, strict=True)
    role: Literal["mentor", "intern"]
    version: int = Field(default=1, ge=1, strict=True)
    source_id: str = Field(min_length=1, max_length=200)
    ratings: dict[str, Annotated[int, Field(strict=True, ge=1, le=5)] | None] = Field(default_factory=dict)
    texts: dict[str, str | None] = Field(default_factory=dict)
    received_at: str | None = None
    analyzed_at: str | None = None
    source: str | None = None
    external_id: str | None = None
    raw: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_ratings(self):
        if any(value is not None and (type(value) is not int or not 1 <= value <= 5)
               for value in self.ratings.values()):
            raise ValueError("ratings require integers 1..5")
        return self


class ActionSelection(Contract):
    action_id: str
    scope: AnalyticsScope = Field(default_factory=AnalyticsScope)


class PairIdentity(Contract):
    pair_id: str = Field(min_length=1, max_length=160)
    mentor_id: str = Field(min_length=1, max_length=100)
    intern_id: str = Field(min_length=1, max_length=100)
    department: str = Field(default="", max_length=500)


class AnalyticsRequest(Contract):
    records: list[SurveyRecord] = Field(default_factory=list, max_length=10000)
    pairs: list[PairIdentity] = Field(default_factory=list, max_length=10000)
    scope: AnalyticsScope = Field(default_factory=AnalyticsScope)
    selected: list[ActionSelection] = Field(default_factory=list, max_length=500)
    current_month: int = Field(default=6, ge=1, le=6)
    retry_errors: bool = False

    @model_validator(mode="after")
    def unique_current_records(self):
        keys = [(record.pair_id, record.month, record.role) for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("resolve survey revisions before analysis")
        pair_keys = [pair.pair_id for pair in self.pairs]
        if len(pair_keys) != len(set(pair_keys)):
            raise ValueError("duplicate pair identity")
        for pair_id in {record.pair_id for record in self.records}:
            identities = {(record.mentor_id, record.intern_id, record.department)
                          for record in self.records if record.pair_id == pair_id}
            if len(identities) != 1:
                raise ValueError("conflicting pair identity")
            registry = next((pair for pair in self.pairs if pair.pair_id == pair_id), None)
            if registry and identities != {(registry.mentor_id, registry.intern_id, registry.department)}:
                raise ValueError("survey identity conflicts with pair registry")
        return self


Score = Annotated[float, Field(ge=0, le=100)]
Coverage = Annotated[float, Field(ge=0, le=1)]
Color = Literal["gray", "green", "yellow", "red"]


class Evidence(Contract):
    id: str
    source_id: str
    version: int = Field(ge=1)
    pair_id: str
    month: int = Field(ge=1, le=6)
    role: Literal["mentor", "intern"]
    question_id: str
    quote: str | None
    kind: Literal["text", "rating"]
    value: int | None
    validation_status: str | None = None
    rule_id: str | None = None
    temporal_status: str | None = None
    polarity: str | None = None


class PairFactor(Contract):
    id: str
    instance_id: str
    name: str
    weight: Score
    severity: Coverage | None
    state: Literal["unknown", "active", "observed_no_signal"]
    contribution: Score
    scenario_contribution: Score
    rating: int | None
    rule_id: str
    activation_rule: str
    source: str
    rule_status: str
    validation_status: str
    review_flags: list[str]
    evidence: list[Evidence]
    score_evidence_refs: list[str]
    active_rules: list[str] = Field(default_factory=list)


class SideResult(Contract):
    score: Score | None
    actual_score: Score | None
    band: Color
    coverage: Coverage
    status: Literal["missing", "future", "insufficient_data", "partial", "completed"]
    factors: list[PairFactor]
    unknown_factors: list[str]
    evidence: list[Evidence]
    critical_flags: list[str]
    signals: list[dict]
    text_analysis_status: str
    text_interpretation_status: Literal["missing", "empty", "error", "not_run", "local_abstain", "abstained", "local_partial", "partial", "local_matched", "matched", "local_encoder_review"]
    unclassified_fragments: list[Evidence]
    source_id: str | None = None
    version: int | None = None
    nlp_audit: dict = Field(default_factory=dict)
    text_mode: str
    nlp_identity: dict


class Indices(Contract):
    x: Score | None
    y: Score | None
    z: Score | None

    @model_validator(mode="after")
    def consistent_mean(self):
        if self.z is not None and (self.x is None or self.y is None or abs(self.z - (self.x + self.y) / 2) > 1e-8):
            raise ValueError("inconsistent pair mean")
        return self


class MatrixCell(Contract):
    pair_id: str
    month: int = Field(ge=1, le=6)
    x: SideResult
    y: SideResult
    z: Score | None
    eligible: bool
    actual: Indices
    scenario: Indices


class MatrixRow(Contract):
    pair_id: str
    mentor_id: str
    intern_id: str
    department: str
    months: list[MatrixCell] = Field(min_length=6, max_length=6)


class SideBands(Contract):
    x: Color
    y: Color


class ObservationRef(Contract):
    pair_id: str
    month: int = Field(ge=1, le=6)


class SideStatistic(Contract):
    mean: Score | None
    count: int = Field(ge=0)


class AggregateResult(Indices):
    actual: Indices
    scenario: Indices
    bands: SideBands
    coverage: Coverage
    observation_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    observations: list[ObservationRef]
    side_statistics: dict[Literal["mentor", "intern"], SideStatistic]
    received_surveys: int = Field(ge=0)
    expected_surveys: int = Field(ge=0)
    risk_distribution: dict[Color, int]
    factor_contributions: dict[str, list[dict]]


class TargetObservation(ObservationRef):
    role: Literal["mentor", "intern"]


class PairRecommendation(Contract):
    id: str
    action_id: str
    scope: AnalyticsScope
    impact_scope: Literal["PAIR", "MENTOR", "INTERN"]
    title: str
    name: str
    rationale: str
    responsible: str
    target_factors: list[str]
    targets: list[str]
    target_observations: list[TargetObservation]
    evidence_refs: list[str]
    delta: Indices
    additional_delta: Indices
    completion_criteria: str
    resolution_criteria: str
    status: str
    assumption: Literal["full_resolution", "no_score_change"]


class Claim(Contract):
    text: str
    pair_id: str
    month: int = Field(ge=1, le=6)
    source_id: str | None
    evidence_refs: list[str]
    factor_instance_id: str


class Section(Contract):
    title: str
    characteristic: str
    generation: Literal["deterministic_evidence_summary"]
    claims: list[Claim]
    evidence: list[Evidence]
    recommendations: list[PairRecommendation]
    critical_flags: list[str]
    coverage: Coverage
    review_flags: list[str]
    history_summary: dict
    text_interpretation: dict
    highlighted_evidence: Evidence | None
    characteristic_evidence_refs: list[str]
    source_facets: dict


class Change(Contract):
    month: int = Field(ge=1, le=6)
    from_value: int = Field(alias="from", ge=1, le=5)
    to: int = Field(ge=1, le=5)
    direction: Literal["improvement", "deterioration"]


class Dynamics(Contract):
    pair_id: str
    role: Literal["mentor", "intern"]
    factor_id: str
    name: str
    values: list[int | None] = Field(min_length=6, max_length=6)
    changes: list[Change]
    scale: list[int]


class PairDiscrepancy(ObservationRef):
    aspect: str
    mentor_rating: int
    intern_rating: int
    normalized_gap: Coverage
    direction: Literal["mentor_higher", "intern_higher"]
    type: Literal["rating_gap"]
    status: Literal["requires_clarification"]
    explanation: str
    evidence: list[Evidence]


class SemanticComparison(ObservationRef):
    aspect: str
    type: Literal["AGREEMENT", "CONTRADICTION", "AWARENESS_GAP", "COMPLEMENTARY", "NOT_COMPARABLE"]
    status: Literal["abstained", "project_rule_provisional", "requires_clarification"]
    mentor_states: list[Literal["positive", "negative", "proposal", "awareness_gap"]]
    intern_states: list[Literal["positive", "negative", "proposal", "awareness_gap"]]
    explanation: str
    evidence: list[Evidence]
    evidence_refs: list[str]
    rule_version: str
    score_effect: Literal[0]


class AnalyticsResponse(Contract):
    analysis_version: str
    methodology: dict
    scope: AnalyticsScope
    updated_at: str | None
    matrix: list[MatrixRow]
    indices: AggregateResult
    sections: dict[Literal["pair", "mentor", "intern"], Section]
    recommendations: list[PairRecommendation]
    dynamics: list[Dynamics]
    discrepancies: list[PairDiscrepancy]
    patterns: list[dict]
    warnings: list[str]
    versions: dict
    summary: dict
    index_dynamics: list[dict]
    semantic_comparisons: list[SemanticComparison]
