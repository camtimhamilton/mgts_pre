"""Bounded internal HTTP client. No scoring, fallback or direct LLM access."""
import math
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import get_settings


class MLUnavailable(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False, strict=True)


class MLRisk(WireModel):
    score: float | None = Field(ge=0, le=100)
    score_band: Literal["routine", "elevated", "high"] | None
    priority: Literal["routine", "elevated", "high", "insufficient_data", "pending_review"]
    data_coverage: float = Field(ge=0, le=1)
    missing_required_roles: list[Literal["intern", "mentor"]]
    critical_flags: list[str]


class MLEvidence(WireModel):
    role: Literal["intern", "mentor"]
    source_kind: Literal["rating", "text"]
    validation_status: Literal["accepted", "review", "rejected"]
    factor_id: str
    rule_id: str
    quote: str | None
    value: float | None
    scale_min: float | None
    scale_max: float | None


class MLRecommendation(WireModel):
    id: str
    name: str
    target_factors: list[str]
    potential_reduction: float = Field(ge=0, le=100)
    responsible: list[str]
    completion_criteria: list[str]
    resolution_criteria: list[str]


class MLDiscrepancy(WireModel):
    factor_id: str
    normalized_gap: float = Field(ge=0, le=1)
    threshold_exceeded: bool


class MLFactor(WireModel):
    id: str
    name: str
    contribution: float = Field(ge=0, le=100)
    weight: float = Field(ge=0, le=100)
    evidence: list[MLEvidence]
    state: Literal["active", "observed_no_signal", "unknown", "conflicting"]


class MLAnalysis(WireModel):
    analysis_id: str
    program_id: str
    intern_id: str
    period: int
    status: Literal["completed", "partial", "insufficient_data"]
    risk: MLRisk
    factors: list[MLFactor]
    recommendations: list[MLRecommendation]
    discrepancies: list[MLDiscrepancy]
    signals: list[MLEvidence]
    metadata: dict
    warnings: list[str]
    capabilities: dict[str, bool]


class MLClient:
    def __init__(self, transport=None):
        settings = get_settings()
        self.url = settings.ml_api_url.rstrip("/")
        self.timeout = settings.ml_api_timeout
        self.transport = transport

    def _request(self, method: str, path: str, payload=None) -> dict[str, Any]:
        limit = 32_000_000 if path == "/v2/analytics" else 2_000_000
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout, follow_redirects=False,
                              trust_env=False) as client:
                with client.stream(method, self.url + path, json=payload) as response:
                    if response.status_code != 200:
                        raise MLUnavailable(f"ml_http_{response.status_code}")
                    chunks = bytearray()
                    for chunk in response.iter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > limit:
                            raise MLUnavailable("ml_response_too_large")
            import json
            value = json.loads(chunks)
            if not isinstance(value, dict):
                raise ValueError("object required")
            return value
        except httpx.TimeoutException:
            raise MLUnavailable("ml_timeout") from None
        except httpx.RequestError:
            raise MLUnavailable("ml_unavailable") from None
        except (ValueError, RecursionError):
            raise MLUnavailable("ml_invalid_response") from None

    def analyze(self, payload: dict) -> dict:
        health = self._request("GET", "/health")
        if health.get("provider") != "mock" or health.get("data_mode") != "demo":
            raise MLUnavailable("ml_mock_required_for_integration")
        result = self._request("POST", "/v1/analyze", payload)
        try:
            parsed = MLAnalysis.model_validate(result)
            if any(result[k] != payload[k] for k in ("program_id", "intern_id", "period")):
                raise ValueError("subject mismatch")
            if parsed.metadata.get("provider") != "mock":
                raise ValueError("provider mismatch")
            if any(parsed.metadata.get(key) != payload[key] for key in ("mapping_version", "knowledge_version")):
                raise ValueError("version mismatch")
            if len({f.id for f in parsed.factors}) != len(parsed.factors):
                raise ValueError("duplicate factors")
            if parsed.risk.score is not None and not math.isclose(
                    parsed.risk.score, sum(f.contribution for f in parsed.factors), abs_tol=1e-8):
                raise ValueError("inconsistent contributions")
        except (ValidationError, ValueError, KeyError):
            raise MLUnavailable("ml_invalid_analysis") from None
        return parsed.model_dump(mode="json")

    def scenario(self, analysis_id: str, action_ids: list[str]) -> dict:
        result = self._request("POST", "/v1/scenario", {"analysis_id": analysis_id, "action_ids": action_ids})
        try:
            if result["analysis_id"] != analysis_id or result["assumption"] != "full_resolution":
                raise ValueError("invalid scenario")
            for key in ("current_score", "scenario_score", "potential_reduction"):
                if isinstance(result[key], bool) or not math.isfinite(result[key]) or not 0 <= result[key] <= 100:
                    raise ValueError("invalid scenario number")
        except (ValueError, KeyError, TypeError):
            raise MLUnavailable("ml_invalid_scenario") from None
        return result
