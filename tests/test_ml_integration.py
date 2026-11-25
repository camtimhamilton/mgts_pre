import copy
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import ml_service, repo
from app.config import get_settings
from app.db import connect, init_db
from app.main import app
from app.ml_client import MLClient, MLUnavailable
from app.ml_mapping import survey_payload
from app.normalize import from_payload

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("ANALYSIS_MODE", "ml")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "backend.sqlite3"))
    get_settings.cache_clear()
    init_db()
    conn = connect()
    yield conn
    conn.close()
    get_settings.cache_clear()


def payloads():
    return json.loads((ROOT / "integration/synthetic_ingest.json").read_text(encoding="utf-8"))


def seed(conn, values=None):
    for value in values or payloads():
        repo.upsert_submission(conn, from_payload(value))
    conn.commit()
    return conn.execute("SELECT id FROM pairs").fetchone()[0]


def sample():
    return json.loads((ROOT / "integration/examples/ml_response.json").read_text(encoding="utf-8"))


def test_actual_mapping_preserves_text_and_never_sends_names(db):
    values = payloads()
    survey = survey_payload(5, "M3", [from_payload(p).as_dict() for p in values])
    assert survey["period"] == 3 and survey["intern_id"] == "pair_5"
    assert "Синтетический" not in json.dumps(survey, ensure_ascii=False)
    answer = survey["responses"][0]["answers"][-1]
    assert answer["value"] == values[0]["q_barrier"]
    assert answer["question_text"] == "Что мешает работать или снижает желание продолжать?"


def test_ml_is_only_score_source_and_cache_survives_reopen(db, monkeypatch):
    pair_id = seed(db)
    calls = []
    def analyze(self, payload):
        calls.append(payload)
        result = sample()
        result.update(program_id=payload["program_id"], intern_id=payload["intern_id"], period=payload["period"])
        return result
    monkeypatch.setattr(MLClient, "analyze", analyze)
    monkeypatch.setattr(MLClient, "scenario", lambda self, aid, actions: {"analysis_id": aid,
        "current_score": 17, "scenario_score": 0, "potential_reduction": 17,
        "covered_factor_ids": ["R01", "R03"], "assumption": "full_resolution"})
    from app import analysis as legacy_analysis
    monkeypatch.setattr(legacy_analysis, "build_core", lambda *args: pytest.fail("legacy scoring invoked"))
    card = ml_service.recompute_pair_month(db, pair_id, "M3")
    assert card["risk"] == 17 and sum(f["points"] for f in card["factors"]) == 17
    assert len(card["factors"]) == 10 and card["data_coverage"] == 0.22
    assert card["gap_penalty"] == card["text_penalty"] == 0
    assert card["scenario"]["scenario_score"] == 0
    other = connect()
    try:
        assert ml_service.pair_card(other, pair_id, "M3")["risk"] == 17
        ml_service.recompute_pair_month(other, pair_id, "M3")
    finally:
        other.close()
    with TestClient(app) as client:
        for _ in range(3):
            assert client.get(f"/api/pairs/{pair_id}?month=M3").json()["risk"] == 17
        assert client.get("/api/program?month=M3").json()["kpi"]["avg_risk"] == 17
    assert len(calls) == 1


def test_missing_and_outage_never_fake_low(db, monkeypatch):
    pair_id = seed(db, [payloads()[0]])
    def fail(self, payload):
        raise MLUnavailable("ml_unavailable")
    monkeypatch.setattr(MLClient, "analyze", fail)
    card = ml_service.recompute_pair_month(db, pair_id, "M3")
    assert card["risk"] is None and card["analysis_state"] == "error"
    assert card["missing_required_roles"] == ["mentor"]
    assert card["priority"] == "insufficient_data"
    assert ml_service.pair_card(db, pair_id, "M2")["risk"] is None
    with TestClient(app) as client:
        assert client.get("/api/pairs?month=M3").headers["X-Data-Source"] == "db"
        assert client.get("/api/pairs?month=M7").status_code == 422
        assert client.post(f"/api/pairs/{pair_id}/scenario?month=M3", json={"action_ids": []}).status_code == 503


def test_stale_input_not_shown_or_persisted(db, monkeypatch):
    pair_id = seed(db)
    def changed_during_http(self, payload):
        correction = dict(payloads()[0], f_clarity=8)
        other = connect()
        try:
            repo.upsert_submission(other, from_payload(correction))
            other.commit()
        finally:
            other.close()
        return sample()
    monkeypatch.setattr(MLClient, "analyze", changed_during_http)
    monkeypatch.setattr(MLClient, "scenario", lambda *args: {})
    card = ml_service.recompute_pair_month(db, pair_id, "M3")
    assert card["analysis_state"] == "pending" and card["risk"] is None
    assert db.execute("SELECT COUNT(*) FROM ml_runs").fetchone()[0] == 0


def test_client_requires_mock_before_posting(db):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={"provider": "deepseek", "data_mode": "demo"})
    with pytest.raises(MLUnavailable, match="ml_mock_required_for_integration"):
        MLClient(httpx.MockTransport(handler)).analyze({})
    assert calls == ["GET"]


@pytest.mark.parametrize("change", ["score", "subject", "provider", "factor"])
def test_invalid_ml_result_rejected(db, change):
    result = sample()
    payload = {k: result[k] for k in ("program_id", "intern_id", "period")}
    payload.update({k: result["metadata"][k] for k in ("mapping_version", "knowledge_version")})
    if change == "score":
        result["risk"]["score"] = 18
    elif change == "subject":
        result["intern_id"] = "wrong_person"
    elif change == "provider":
        result["metadata"]["provider"] = "deepseek"
    else:
        result["factors"].append(result["factors"][0])
    def handler(request):
        return httpx.Response(200, json={"provider": "mock", "data_mode": "demo"} if request.method == "GET" else result)
    with pytest.raises(MLUnavailable, match="ml_invalid_analysis"):
        MLClient(httpx.MockTransport(handler)).analyze(payload)


def test_scenario_only_calls_ml_and_keeps_snapshot(db, monkeypatch):
    pair_id = seed(db)
    monkeypatch.setattr(MLClient, "analyze", lambda *args: sample())
    actions_seen = []
    def scenario(self, aid, actions):
        actions_seen.append(actions)
        return {"analysis_id": aid, "current_score": 17, "scenario_score": 5,
                "potential_reduction": 12, "covered_factor_ids": ["R01"], "assumption": "full_resolution"}
    monkeypatch.setattr(MLClient, "scenario", scenario)
    card = ml_service.recompute_pair_month(db, pair_id, "M3")
    before = db.execute("SELECT result_json FROM ml_runs").fetchone()[0]
    with TestClient(app) as client:
        response = client.post(f"/api/pairs/{pair_id}/scenario?month=M3", json={"action_ids": ["A01", "A11"]})
        assert response.json()["scenario_score"] == 5
        assert client.get(f"/api/pairs/{pair_id}?month=M3").json()["risk"] == 17
    assert actions_seen[-1] == ["A01", "A11"]
    assert db.execute("SELECT result_json FROM ml_runs").fetchone()[0] == before


@pytest.mark.parametrize("field", ["evidence", "recommendations", "discrepancies", "score_type"])
def test_malformed_nested_response_never_poisoned_cache(db, monkeypatch, field):
    pair_id = seed(db)
    result = sample()
    if field == "evidence":
        result["factors"][0]["evidence"] = [{}]
    elif field == "score_type":
        result["risk"]["score"] = "17"
    else:
        result[field] = [{}]
    def handler(request):
        return httpx.Response(200, json={"provider": "mock", "data_mode": "demo"} if request.method == "GET" else result)
    original = MLClient
    monkeypatch.setattr(ml_service, "MLClient", lambda: original(httpx.MockTransport(handler)))
    card = ml_service.recompute_pair_month(db, pair_id, "M3")
    assert card["analysis_state"] == "error" and card["risk"] is None
    assert card["error_code"] == "ml_invalid_analysis"
    with TestClient(app) as client:
        assert client.get(f"/api/pairs/{pair_id}?month=M3").status_code == 200


def test_observation_counts_are_pair_and_period_scoped(db):
    pair_id = seed(db)
    seed(db, [dict(p, s_month="M1") for p in payloads()])
    seed(db, [dict(payloads()[0], s_name="Другой Синтетический", s_month="M2")])
    assert ml_service.pair_card(db, pair_id, "M3")["observation"] == {"months": 2, "with_both_forms": 2}
    assert ml_service.pair_card(db, pair_id, "M1")["observation"] == {"months": 1, "with_both_forms": 1}


def test_import_during_analysis_queues_another_pass(db, monkeypatch):
    import asyncio
    from app.routers import imports
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def fake_thread(work):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return {"pairs": 1, "snapshots": 1}
        monkeypatch.setattr(imports.asyncio, "to_thread", fake_thread)
        monkeypatch.setattr(imports, "_recompute_lock", asyncio.Lock())
        monkeypatch.setattr(imports, "_recompute_pending", False)
        first = asyncio.create_task(imports._recompute_after_import())
        await started.wait()
        await imports._recompute_after_import()
        release.set()
        await first
        assert len(calls) == 2
    asyncio.run(run())
