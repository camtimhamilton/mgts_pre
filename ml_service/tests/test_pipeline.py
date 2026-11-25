import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import scenario
from app.knowledge import ROOT
from app.llm import ProviderError, ProviderResult
from app.main import create_app
from app.schemas import AnalysisResult, ScenarioRequest
from app.storage import SQLiteSnapshots


@pytest.fixture
def payload():
    return json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "snapshots.sqlite3")) as client:
        yield client


def test_end_to_end(client, payload):
    result = client.post("/v1/analyze", json=payload)
    assert result.status_code == 200, result.text
    body = result.json()
    AnalysisResult.model_validate(body)
    assert body["risk"]["score"] == 17
    assert body["risk"]["data_coverage"] == 0.22
    assert body["risk"]["priority"] == "insufficient_data"
    assert body["discrepancies"][0]["normalized_gap"] == 0.75
    assert body["metadata"]["accepted_count"] == 4
    assert body["metadata"]["trace_id"] == result.headers["X-Trace-ID"]
    for actions in (["A01"], ["A01", "A11"], ["A01", "A01"]):
        result = client.post("/v1/scenario", json={"analysis_id": body["analysis_id"], "action_ids": actions})
        assert result.status_code == 200 and result.json()["scenario_score"] == 5
    assert client.get("/v1/analysis/" + body["analysis_id"]).json() == body
    again = client.post("/v1/analyze", json=payload).json()
    assert again["risk"] == body["risk"] and again["factors"] == body["factors"]
    assert again["analysis_id"] != body["analysis_id"]  # separate run, same calculation key
    assert again["metadata"]["analysis_key"] == body["metadata"]["analysis_key"]


def test_health_and_openapi(client):
    health = client.get("/health").json()
    assert health["provider"] == "mock" and not health["capabilities"]["replay"]
    schema = client.get("/openapi.json").json()
    assert "SurveyPeriod" in schema["components"]["schemas"]
    assert "ScenarioResult" in schema["components"]["schemas"]


def test_versions_modes_and_validation(client, payload):
    payload["knowledge_version"] = "unknown"
    result = client.post("/v1/analyze", json=payload)
    assert result.status_code == 409 and result.json()["code"] == "version_conflict"
    payload["knowledge_version"] = "0.1.0"
    assert client.post("/v1/analyze?mode=replay", json=payload).status_code == 422
    payload["period"] = 7
    result = client.post("/v1/analyze", json=payload)
    assert result.status_code == 422 and "Наставник" not in result.text
    assert client.get("/v1/analysis/absent").status_code == 404


def test_unknown_fixture_partial_and_failure(client, payload):
    payload["responses"][0]["answers"][1]["value"] = "Неизвестный mock текст"
    result = client.post("/v1/analyze", json=payload)
    assert result.status_code == 200
    body = result.json()
    assert body["status"] == "partial" and body["risk"]["score"] == 6
    assert body["risk"]["priority"] == "insufficient_data"
    assert "unknown_mock_fixture" in body["warnings"]
    payload["responses"] = [payload["responses"][0]]
    payload["responses"][0]["answers"] = payload["responses"][0]["answers"][1:]
    assert client.post("/v1/analyze", json=payload).status_code == 503


def test_strict_and_no_data(client, payload):
    body = client.post("/v1/analyze?profile=strict", json=payload).json()
    assert body["risk"]["score"] == 6 and body["metadata"]["review_count"] == 2
    assert "strict_semantic_verifier_not_implemented" in body["warnings"]
    payload["responses"] = []
    body = client.post("/v1/analyze", json=payload).json()
    assert body["risk"]["score"] is None
    assert client.post("/v1/scenario", json={"analysis_id": body["analysis_id"], "action_ids": []}).status_code == 409


def test_missing_action_and_snapshot_immutability(client, payload, tmp_path):
    body = client.post("/v1/analyze", json=payload).json()
    result = client.post("/v1/scenario", json={"analysis_id": body["analysis_id"], "action_ids": ["A99"]})
    assert result.status_code == 404
    repo = SQLiteSnapshots(tmp_path / "immutable.sqlite3")
    analysis = AnalysisResult.model_validate(body)
    repo.save(analysis)
    repo.save(analysis)
    with pytest.raises(ValueError, match="immutable_snapshot_conflict"):
        repo.save(analysis.model_copy(update={"period": 4}))
    assert repo.get(analysis.analysis_id) == analysis


class BadProvider:
    def __init__(self, raw):
        self.raw = raw

    async def generate_structured(self, *args):
        return ProviderResult(raw=self.raw, provider="mock", model_id="fixture-v1")


@pytest.mark.parametrize("raw", ["not JSON", "{}", '{"response_id":"other","signals":[],"unclassified_fragments":[],"requires_review":false}',
    '{"response_id":"response_001","signals":[],"unclassified_fragments":[{"question_id":"q02","quote":"invented","reason":"x"}],"requires_review":true}'])
def test_malformed_provider_is_partial(tmp_path, payload, raw):
    with TestClient(create_app(tmp_path / "bad.sqlite3", BadProvider(raw))) as client:
        response = client.post("/v1/analyze", json=payload)
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "partial" and result["risk"]["priority"] == "insufficient_data"


def test_body_limit_and_safe_logs(client, payload, caplog):
    caplog.set_level("INFO", logger="mgts.pipeline")
    result = client.post("/v1/analyze", json=payload)
    assert result.status_code == 200
    assert "stage=rating" in caplog.text and "Наставник" not in caplog.text
    assert "intern_014" not in caplog.text
    oversized = client.post("/v1/analyze", content=b" " * 300001)
    assert oversized.status_code == 413


def test_storage_failure_sanitized(tmp_path, payload, monkeypatch):
    import sqlite3
    api = create_app(tmp_path / "bad_storage.sqlite3")
    def fail_save(result):
        raise sqlite3.OperationalError("database path secret / raw comment")
    monkeypatch.setattr(api.state.pipeline.repository, "save", fail_save)
    with TestClient(api) as client:
        response = client.post("/v1/analyze", json=payload)
        assert response.status_code == 503
        assert response.json()["code"] == "snapshot_storage_unavailable"
        assert "secret" not in response.text and response.json()["trace_id"]


def test_wrong_provider_identity(tmp_path, payload):
    class WrongProvider:
        async def generate_structured(self, *args):
            return ProviderResult(raw="{}", provider="other", model_id="other-model")
    with TestClient(create_app(tmp_path / "identity.sqlite3", WrongProvider())) as client:
        result = client.post("/v1/analyze", json=payload).json()
        assert result["status"] == "partial" and "provider_identity_mismatch" in result["warnings"]


def test_timeout_is_partial(tmp_path, payload, monkeypatch):
    class TimeoutProvider:
        async def generate_structured(self, *args):
            raise TimeoutError()
    with TestClient(create_app(tmp_path / "timeout.sqlite3", TimeoutProvider())) as client:
        result = client.post("/v1/analyze", json=payload).json()
        assert result["status"] == "partial" and "provider_timeout" in result["warnings"]


def test_snapshot_reopen_and_delete(tmp_path, client, payload):
    path = tmp_path / "reopen.sqlite3"
    repository = SQLiteSnapshots(path)
    result = AnalysisResult.model_validate(client.post("/v1/analyze", json=payload).json())
    repository.save(result)
    assert SQLiteSnapshots(path).get(result.analysis_id) == result
    path.unlink()  # On Windows this proves no live connection holds the database file.


def test_busy_admission(tmp_path, payload):
    from app.schemas import SurveyPeriod
    api = create_app(tmp_path / "busy.sqlite3")
    pipeline = api.state.pipeline
    async def run():
        for _ in range(4):
            await pipeline.slots.acquire()
        try:
            with pytest.raises(ProviderError, match="service_busy"):
                await pipeline.analyze(SurveyPeriod.model_validate(payload))
        finally:
            for _ in range(4):
                pipeline.slots.release()
    asyncio.run(run())


CATALOGUE = json.loads((ROOT / "tests/fixtures/mock_extractions.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture", CATALOGUE["cases"], ids=lambda item: item["id"])
def test_catalogue(client, fixture, monkeypatch):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("network forbidden"))
    data = {"program_id": "synthetic", "intern_id": "synthetic", "period": 3, "responses": [{
        "response_id": "fixture_response", "respondent_role": fixture["input"]["respondent_role"],
        "answers": fixture["input"]["answers"]}]}
    result = client.post("/v1/analyze", json=data)
    assert result.status_code == 200, result.text
    body = result.json()
    actual = sorted(f["id"] for f in body["factors"] if f["contribution"] > 0)
    assert actual == sorted(fixture["expected_active_factors"])
    assert body["risk"]["priority"] in {"insufficient_data", "pending_review"}
    if fixture["id"] == "critical_intern":
        assert body["risk"]["critical_flags"] and body["metadata"]["review_count"] == 1
    if fixture["id"] == "critical_mentor":
        assert body["metadata"]["rejected_count"] == 1 and not body["risk"]["critical_flags"]
