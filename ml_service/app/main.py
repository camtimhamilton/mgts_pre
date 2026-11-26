"""Local demo REST boundary; authentication and user authorization belong to backend."""
import os
import sqlite3
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core import scenario
from app.config import provider_name
from app.knowledge import load_knowledge, load_mapping
from app.llm import FixtureProvider, ProviderError
from app.orchestrator import CAPABILITIES, Pipeline
from app.schemas import AnalysisResult, ScenarioRequest, ScenarioResult, SurveyPeriod
from app.storage import SQLiteSnapshots
from app.pair_schemas import AnalyticsRequest, AnalyticsResponse
from app.pair_analytics import analyze as analyze_pairs
from app.pair_analytics import load_policy as load_pair_policy
from app.pair_analytics import PIPELINE_VERSION as PAIR_PIPELINE_VERSION
from app.pair_semantics import load_semantic_rules
from app.serialization import digest


class APIError(BaseModel):
    code: str
    message: str
    trace_id: str
    details: list[str] = []


class Health(BaseModel):
    status: Literal["ok"]
    provider: Literal["mock", "deepseek"]
    data_mode: Literal["demo"]
    knowledge_version: str
    mapping_version: str
    capabilities: dict[str, bool]


def create_app(database: Path | None = None, provider=None) -> FastAPI:
    name = provider_name()
    if provider is None:
        if name == "deepseek":
            from app.deepseek import DeepSeekProvider
            provider = DeepSeekProvider()
        else:
            catalog = os.getenv("MGTS_FIXTURE_CATALOG")
            provider = FixtureProvider(Path(catalog)) if catalog else FixtureProvider()
    kb, mapping = load_knowledge(), load_mapping()
    repository = SQLiteSnapshots(database or Path(os.getenv("MGTS_DATABASE", "data/demo.sqlite3")))
    pipeline = Pipeline(kb, mapping, provider, repository)
    api = FastAPI(title="ПУЛЬС · аналитика стажировок МГТС", version="2.0.0",
                  description="Pair survey analytics v2 and the original synthetic v1 API. "
                              "Local evidence, independent role indices and conditional scenarios.",
                  responses={422: {"model": APIError}, 409: {"model": APIError}, 503: {"model": APIError}})
    api.state.pipeline = pipeline

    @api.middleware("http")
    async def trace_and_bound(request: Request, call_next):
        request.state.trace_id = "trace_" + uuid4().hex
        if request.method == "POST":
            # Check the streamed body too: Content-Length may be absent or dishonest.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > (10000000 if request.url.path == "/v2/analytics" else 300000):
                    return JSONResponse(status_code=413, content=error_body(request, "request_too_large"))
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Trace-ID"] = request.state.trace_id
        return response

    @api.exception_handler(RequestValidationError)
    async def invalid_input(request, error):
        # Pydantic errors normally embed the rejected input. Never echo it.
        details = [".".join(str(p) for p in e["loc"]) + ":" + e["type"] for e in error.errors()]
        return JSONResponse(status_code=422, content=error_body(request, "invalid_input", details))

    @api.exception_handler(ProviderError)
    async def provider_failed(request, error):
        return JSONResponse(status_code=503, content=error_body(request, error.code))

    @api.exception_handler(sqlite3.Error)
    async def storage_failed(request, error):
        return JSONResponse(status_code=503, content=error_body(request, "snapshot_storage_unavailable"))

    @api.exception_handler(ValueError)
    async def conflict(request, error):
        code = "version_conflict" if str(error) == "version_conflict" else "operation_unavailable"
        return JSONResponse(status_code=409, content=error_body(request, code))

    @api.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse(status_code=404, content=error_body(request, "snapshot_or_action_not_found"))

    @api.get("/health", response_model=Health)
    async def health():
        return Health(status="ok", provider=pipeline.provider_name, data_mode="demo", knowledge_version=kb.version,
                      mapping_version=mapping.version, capabilities=pipeline.capabilities)

    @api.post("/v1/analyze", response_model=AnalysisResult)
    async def analyze(survey: SurveyPeriod, request: Request, mode: Literal["live"] = "live",
                      profile: Literal["fast", "strict"] = "fast"):
        return await pipeline.analyze(survey, profile, request.state.trace_id)

    @api.get("/v1/analysis/{analysis_id}", response_model=AnalysisResult,
             responses={404: {"model": APIError}})
    async def get_analysis(analysis_id: str):
        result = repository.get(analysis_id)
        if result is None:
            raise KeyError("snapshot_not_found")
        return result

    @api.post("/v1/scenario", response_model=ScenarioResult, responses={404: {"model": APIError}})
    async def calculate_scenario(body: ScenarioRequest):
        result = repository.get(body.analysis_id)
        if result is None:
            raise KeyError("snapshot_not_found")
        return scenario(result, body)

    @api.post("/v2/analytics", response_model=AnalyticsResponse)
    async def pair_analytics(body: AnalyticsRequest):
        from app.pulse_nlp import enrich
        records = [await enrich(record.model_dump(), retry_errors=body.retry_errors) for record in body.records]
        return analyze_pairs(records, body.scope.model_dump(),
                             [selection.model_dump() for selection in body.selected], body.current_month,
                             pairs=[pair.model_dump() for pair in body.pairs])

    @api.get("/v2/methodology")
    async def pair_methodology():
        from app.pulse_nlp import configuration
        policy = load_pair_policy()
        mode, _, nlp_version = configuration()
        return {"cache_version": digest([policy, load_semantic_rules(), nlp_version, PAIR_PIPELINE_VERSION]),
                "policy": policy, "nlp_version": nlp_version, "text_mode": mode,
                "pipeline": PAIR_PIPELINE_VERSION}

    return api


def error_body(request: Request, code: str, details: list[str] | None = None) -> dict:
    return APIError(code=code, message=code, trace_id=request.state.trace_id, details=details or []).model_dump()
