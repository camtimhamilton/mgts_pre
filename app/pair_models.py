"""Versioned wire contracts for pair analytics and historical surveys."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from ml_service.app.pair_schemas import AnalyticsResponse


class DashboardAnalysis(AnalyticsResponse):
    ml_analysis_version: str
    analysis_id: str
    trace_id: str
    processing_ms: float = Field(ge=0)
    data_version: str
    methodology_cache_version: str


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class AnalyticsScope(WireModel):
    type: Literal["GLOBAL", "PAIR_MONTH", "PAIR_RANGE"] = "GLOBAL"
    pair_id: str | None = None
    start: int | None = Field(default=None, ge=1, le=6)
    end: int | None = Field(default=None, ge=1, le=6)

    @model_validator(mode="after")
    def validate_scope(self):
        if self.type == "GLOBAL":
            if any(v is not None for v in (self.pair_id, self.start, self.end)):
                raise ValueError("GLOBAL has no pair or period")
        else:
            if not self.pair_id or self.start is None:
                raise ValueError("pair_id and start are required")
            if self.end is None:
                self.end = self.start
            if self.end < self.start:
                self.start, self.end = self.end, self.start
            if self.type == "PAIR_MONTH" and self.end != self.start:
                raise ValueError("invalid scope period")
        return self


class SelectedAction(WireModel):
    action_id: str = Field(min_length=1, max_length=160)
    scope: AnalyticsScope


class AnalyticsRequest(WireModel):
    scope: AnalyticsScope = Field(default_factory=AnalyticsScope)
    selected: list[SelectedAction] = Field(default_factory=list, max_length=500)
    expected_data_version: str | None = None
    expected_methodology_version: str | None = None
    current_month: int = Field(default=6, ge=1, le=6, strict=True)


class SurveyInput(WireModel):
    intern_id: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    mentor_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[\w-]+$")
    department: str = Field(min_length=1, max_length=200)
    month: int = Field(ge=1, le=6, strict=True)
    role: Literal["mentor", "intern"]
    ratings: dict[str, StrictInt] = Field(default_factory=dict)
    texts: dict[str, str] = Field(default_factory=dict)
    external_id: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_answers(self):
        allowed = {
            "mentor": {"clarity", "quality", "autonomy", "deadlines", "initiative", "team"},
            "intern": {"clarity", "support", "workload", "interest", "growth", "team", "satisfaction"},
        }
        if set(self.ratings) - allowed[self.role]:
            raise ValueError("unknown rating question")
        if any(type(value) is not int or not 1 <= value <= 5 for value in self.ratings.values()):
            raise ValueError("ratings must be integers 1 through 5")
        if len(self.texts) > 20 or any(len(text) > 20000 for text in self.texts.values()):
            raise ValueError("text limits exceeded")
        if self.role == "mentor" and not self.mentor_id:
            raise ValueError("mentor_id required for mentor survey")
        return self


class ScenarioSave(AnalyticsRequest):
    name: str = Field(default="Мой сценарий", min_length=1, max_length=160)
    scenario_id: str | None = None


class PairRegistration(WireModel):
    intern_id: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    mentor_id: str = Field(min_length=1, max_length=80, pattern=r"^[\w-]+$")
    department: str = Field(min_length=1, max_length=200)
