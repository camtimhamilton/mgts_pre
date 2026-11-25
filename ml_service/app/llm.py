"""Provider port, offline fixtures and mandatory domain extraction validation."""
import json
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from app.knowledge import ROOT, canonical, digest
from app.schemas import LLMExtraction, Model, ProviderCall, Response, SurveyPeriod


class ProviderError(Exception):
    """Sanitized provider failure; never includes input or raw model output."""

    def __init__(self, code: str, telemetry: ProviderCall | None = None):
        self.code = code
        self.telemetry = telemetry
        super().__init__(code)


class ProviderResult(Model):
    raw: str
    provider: str
    model_id: str
    finish_status: str = "completed"
    telemetry: ProviderCall | None = None


class LLMProvider(Protocol):
    async def generate_structured(self, system_prompt: str, user_prompt: str,
                                  response_schema: dict) -> ProviderResult: ...


class FixtureProvider:
    name = "mock"
    model_id = "fixture-v1"
    configuration = {"adapter_version": "fixture-v1"}
    total_timeout_seconds = 10

    def __init__(self, path: Path = ROOT / "tests" / "fixtures" / "mock_extractions.json"):
        self.fixtures = json.loads(path.read_text(encoding="utf-8"))

    async def generate_structured(self, system_prompt: str, user_prompt: str,
                                  response_schema: dict) -> ProviderResult:
        request = json.loads(user_prompt)
        if (request["knowledge_hash"] != self.fixtures["knowledge_hash"] or
                digest(system_prompt) != self.fixtures["prompt_hash"]):
            raise ProviderError("fixture_configuration_mismatch")
        visible_input = {k: request[k] for k in ("respondent_role", "answers", "period", "program_day", "context")}
        for fixture in self.fixtures["cases"]:
            if fixture["input"] == visible_input:
                payload = {**fixture["output"], "response_id": request["response_id"]}
                return ProviderResult(raw=canonical(payload), provider="mock", model_id="fixture-v1")
        raise ProviderError("unknown_mock_fixture")


def build_user_prompt(survey: SurveyPeriod, response: Response, knowledge_hash: str, rules: list[dict]) -> str:
    return canonical({"response_id": response.response_id, "respondent_role": response.respondent_role,
        "period": survey.period, "program_day": survey.program_day, "context": survey.context,
        "answers": [a.model_dump(mode="json") for a in response.answers if a.type == "text" and a.value and a.value.strip()],
        "knowledge_hash": knowledge_hash, "rules": rules})


def parse_extraction(result: ProviderResult, response: Response, expected_provider: str = "mock") -> LLMExtraction:
    if result.provider != expected_provider or (expected_provider == "mock" and result.model_id != "fixture-v1"):
        raise ProviderError("provider_identity_mismatch")
    if result.finish_status != "completed" or len(result.raw.encode("utf-8")) > 256000:
        raise ProviderError("invalid_provider_result")
    try:
        extraction = LLMExtraction.model_validate_json(result.raw)
    except (ValidationError, ValueError):
        raise ProviderError("invalid_extraction_schema") from None
    if extraction.response_id != response.response_id:
        raise ProviderError("extraction_response_mismatch")
    for fragment in extraction.unclassified_fragments:
        answer = next((a for a in response.answers if a.question_id == fragment.question_id and a.type == "text"), None)
        if answer is None or not answer.value or fragment.quote not in answer.value or not fragment.quote.strip():
            raise ProviderError("invalid_fragment_provenance")
    return extraction
