"""Regression tests for historical import, revisions and scenario persistence."""
import copy
import importlib
import re
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.config import get_settings
from app.main import app
from app.ml_client import MLClient, MLUnavailable
from app.pair_import import INTERN_HEADERS, MENTOR_HEADERS, import_history
from app.pair_models import SurveyInput
from app.pair_store import connect, current_records, ingest, init_store, source


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "pair.db"))
    get_settings.cache_clear()
    with TestClient(app) as value:
        yield value
    get_settings.cache_clear()


def mentor(**changes):
    return {"intern_id": "СТ-001", "mentor_id": "Н-001", "department": "Технический блок",
            "role": "mentor", "month": 1, "ratings": {"clarity": 2}, "texts": {"comment": "Уточнить задачи"}, **changes}


def fake_ml(monkeypatch):
    from ml_service.app import pair_schemas, pair_semantics
    monkeypatch.setitem(sys.modules, "app.pair_schemas", pair_schemas)
    monkeypatch.setitem(sys.modules, "app.pair_semantics", pair_semantics)
    monkeypatch.setitem(sys.modules, "app.pulse_nlp", SimpleNamespace(EXIT=re.compile(r"$^")))
    engine = importlib.import_module("ml_service.app.pair_analytics")
    calls = []

    def request(self, method, path, payload=None):
        if path == "/v2/methodology":
            return {"cache_version": "test-v2"}
        calls.append(copy.deepcopy(payload))
        try:
            return engine.analyze(**payload)
        except KeyError:
            raise MLUnavailable("ml_http_404") from None
        except ValueError:
            raise MLUnavailable("ml_http_422") from None

    monkeypatch.setattr(MLClient, "_request", request)
    return calls


def test_revision_replay_and_conflicting_delivery(client):
    first = ingest(SurveyInput(**mentor()), "event-1")
    assert ingest(SurveyInput(**mentor()), "event-1")["status"] == "duplicate"
    with pytest.raises(ValueError, match="delivery_id_content_conflict"):
        ingest(SurveyInput(**mentor(ratings={"clarity": 3})), "event-1")
    second = ingest(SurveyInput(**mentor(ratings={"clarity": 4})), "event-2")
    assert len(current_records()) == 1
    assert current_records()[0]["version"] == 2
    assert source(first["source_id"])["record"]["ratings"]["clarity"] == 2
    assert source(second["source_id"])["record"]["ratings"]["clarity"] == 4
    assert len(source(first["source_id"])["versions"]) == 2


def test_identity_conflicts_roll_back(client):
    ingest(SurveyInput(**mentor()), "event-1")
    with pytest.raises(ValueError, match="pair_registry_conflict"):
        ingest(SurveyInput(**mentor(mentor_id="Н-999")), "event-2")
    with pytest.raises(ValueError, match="intern_pair_not_registered"):
        ingest(SurveyInput(**mentor(intern_id="СТ-999", mentor_id=None, role="intern")), "event-3")
    assert len(current_records()) == 1


def test_cache_failure_and_input_invalidation(client, monkeypatch):
    calls = fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "event-1")
    first = client.post("/api/v2/analytics", json={}).json()
    assert client.post("/api/v2/analytics", json={}).json() == first
    assert len(calls) == 1
    ingest(SurveyInput(**mentor(ratings={"clarity": 5})), "event-2")
    assert client.post("/api/v2/analytics", json={"expected_data_version": first["data_version"]}).status_code == 409
    assert client.post("/api/v2/analytics", json={}).json()["data_version"] != first["data_version"]
    assert len(calls) == 2

    def broken(*args, **kwargs):
        raise MLUnavailable("ml_timeout")

    monkeypatch.setattr(MLClient, "_request", broken)
    ingest(SurveyInput(**mentor(ratings={"clarity": 1})), "event-3")
    response = client.post("/api/v2/analytics", json={})
    assert response.status_code == 503
    assert response.json()["detail"] == "ml_timeout"


def test_scenario_save_restore_history_and_stale(client, monkeypatch):
    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "event-1")
    saved = client.post("/api/v2/scenarios", json={"name": "Тест"})
    assert saved.status_code == 200, saved.text
    scenario_id = saved.json()["scenario_id"]
    restored = client.post(f"/api/v2/scenarios/{scenario_id}/restore").json()
    assert restored["result"] == saved.json()["result"]
    assert [h["kind"] for h in restored["history"]] == ["created", "restored"]
    changed = client.post("/api/v2/scenarios", json={"scenario_id": scenario_id, "selected": [
        {"action_id": "action_mentor_clarity", "scope": {"type": "GLOBAL"}}]})
    assert changed.status_code == 200
    assert changed.json()["history"][-1]["kind"] == "selected"
    ingest(SurveyInput(**mentor(ratings={"clarity": 5})), "event-2")
    assert client.post(f"/api/v2/scenarios/{scenario_id}/restore").status_code == 409


def test_yandex_jsonrpc_replay_and_edit(client, monkeypatch):
    fake_ml(monkeypatch)
    payload = {"jsonrpc": "2.0", "method": "submit", "id": 1,
               "params": {**mentor(), "response_id": "answer-1", "revision_id": "1"}}
    first = client.post("/api/v2/forms/yandex", json=payload).json()
    assert first["result"]["status"] == "accepted"
    assert client.post("/api/v2/forms/yandex", json=payload).json()["result"]["status"] == "duplicate"
    payload["params"]["ratings"] = {"clarity": 4}
    assert "error" in client.post("/api/v2/forms/yandex", json=payload).json()
    payload["params"]["revision_id"] = "2"
    assert client.post("/api/v2/forms/yandex", json=payload).json()["result"]["status"] == "accepted"
    assert current_records()[0]["version"] == 2


def workbook(path, headers, rows):
    book = Workbook()
    book.active.append(headers)
    for row in rows:
        book.active.append(row)
    book.save(path)
    book.close()


def test_import_is_idempotent_and_preserves_raw(client, tmp_path):
    mentor_path, intern_path = tmp_path / "mentor.xlsx", tmp_path / "intern.xlsx"
    workbook(mentor_path, MENTOR_HEADERS, [["Н-001", "СТ-001", "ИТ", "Месяц 1", *([4] * 6), "Комментарий", "Сложности"]])
    workbook(intern_path, INTERN_HEADERS, [["СТ-001", "ИТ", "Месяц 1", *([5] * 7), "Нет", "Нет"]])
    first = import_history(mentor_path, intern_path)
    assert first["accepted"] == 2
    assert import_history(mentor_path, intern_path)["duplicates"] == 2
    record = current_records()[0]
    assert source(record["source_id"])["raw"]["headers"] in [MENTOR_HEADERS, INTERN_HEADERS]
    for record in current_records():
        raw = source(record["source_id"])["raw"]
        expected = (["Н-001", "СТ-001", "ИТ", "Месяц 1", *([4] * 6), "Комментарий", "Сложности"]
                    if record["role"] == "mentor" else ["СТ-001", "ИТ", "Месяц 1", *([5] * 7), "Нет", "Нет"])
        assert raw["values"] == expected
        assert raw["row"] == 2 and raw["sheet"] == "Sheet"
    workbook(intern_path, INTERN_HEADERS, [["СТ-999", "ИТ", "Месяц 1", *([5] * 7), "Нет", "Нет"]])
    with pytest.raises(ValueError, match="intern_pair_not_registered"):
        import_history(mentor_path, intern_path)
    assert len(current_records()) == 2


def test_registered_pair_without_forms_changes_revision(client, monkeypatch):
    calls = fake_ml(monkeypatch)
    before = client.get("/api/v2/revision").json()
    created = client.post("/api/v2/pairs", json={"intern_id": "СТ-100", "mentor_id": "Н-100", "department": "ИТ"})
    assert created.status_code == 200
    assert client.get("/api/v2/revision").json() != before
    assert client.post("/api/v2/analytics", json={}).status_code == 200
    assert len(calls[-1]["pairs"]) == 1
    assert calls[-1]["records"] == []


def test_methodology_change_invalidates_cache_and_scenario(client, monkeypatch):
    calls = fake_ml(monkeypatch)
    saved = client.post("/api/v2/scenarios", json={}).json()
    original = MLClient._request

    def changed(self, method, path, payload=None):
        if path == "/v2/methodology":
            return {"cache_version": "new-method"}
        return original(self, method, path, payload)

    monkeypatch.setattr(MLClient, "_request", changed)
    assert client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore').status_code == 409
    assert client.post("/api/v2/analytics", json={"expected_methodology_version": "test-v2"}).status_code == 409
    assert client.post("/api/v2/analytics", json={}).status_code == 200
    assert len(calls) == 2


def test_malformed_remote_response_is_not_cached(client, monkeypatch):
    original_calls = fake_ml(monkeypatch)
    original = MLClient._request

    def malformed(self, method, path, payload=None):
        result = original(self, method, path, payload)
        if path == "/v2/analytics":
            result["indices"].update(x=20, y=40, z=99)
        return result

    monkeypatch.setattr(MLClient, "_request", malformed)
    assert client.post("/api/v2/analytics", json={}).status_code == 502
    monkeypatch.setattr(MLClient, "_request", original)
    assert client.post("/api/v2/analytics", json={}).status_code == 200
    assert len(original_calls) == 2


def test_reverse_range_is_normalized(client, monkeypatch):
    calls = fake_ml(monkeypatch)
    pair_id = client.post("/api/v2/pairs", json={"intern_id": "СТ-001", "mentor_id": "Н-001", "department": "ИТ"}).json()["pair_id"]
    response = client.post("/api/v2/analytics", json={"scope": {
        "type": "PAIR_RANGE", "pair_id": pair_id, "start": 5, "end": 2}})
    assert response.status_code == 200
    assert calls[-1]["scope"]["start"] == 2
    assert calls[-1]["scope"]["end"] == 5


@pytest.mark.parametrize("ratings", [{"clarity": 0}, {"clarity": 6}, {"clarity": True}, {"random": 3}])
def test_bad_rating_is_rejected(client, ratings):
    assert client.post("/api/v2/ingest", json=mentor(ratings=ratings), headers={"x-delivery-id": "bad"}).status_code == 422


def paired_surveys():
    mentor_record = SurveyInput(**mentor(ratings={"clarity": 2, "quality": 5, "autonomy": 5,
        "deadlines": 5, "initiative": 5, "team": 5}))
    intern_record = SurveyInput(**mentor(role="intern", ratings={"clarity": 2, "support": 5,
        "workload": 5, "interest": 5, "growth": 5, "team": 5, "satisfaction": 5},
        texts={"difficulties": "Уточнить задачи"}))
    return mentor_record, intern_record


def test_replay_keeps_original_trace_and_survives_new_data(client, monkeypatch):
    fake_ml(monkeypatch)
    for role, survey in enumerate(paired_surveys()):
        ingest(survey, f"event-{role}")
    response = client.post("/api/v2/analytics", json={})
    assert response.status_code == 200, response.text
    original = response.json()
    assert original["trace_id"].startswith("trace-")
    assert original["processing_ms"] >= 0
    ingest(SurveyInput(**mentor(ratings={"clarity": 5})), "edit")
    replay = client.get(f'/api/v2/analysis/{original["analysis_id"]}')
    assert replay.status_code == 200
    assert replay.json() == original
    assert client.get("/api/v2/analysis/unknown").status_code == 404


def test_empty_matrix_cannot_return_fabricated_consistent_aggregate(client, monkeypatch):
    fake_ml(monkeypatch)
    original = MLClient._request

    def fabricated(self, method, path, payload=None):
        result = original(self, method, path, payload)
        if path == "/v2/analytics":
            result["indices"].update(x=99, y=99, z=99,
                actual={"x": 99, "y": 99, "z": 99}, scenario={"x": 99, "y": 99, "z": 99})
        return result

    monkeypatch.setattr(MLClient, "_request", fabricated)
    assert client.post("/api/v2/analytics", json={}).status_code == 502


@pytest.mark.parametrize("tamper", ["monthly_actual", "monthly_scenario", "factor", "evidence_cell", "aggregate_population", "section_quote"])
def test_remote_monthly_and_aggregate_integrity(client, monkeypatch, tamper):
    fake_ml(monkeypatch)
    for role, survey in enumerate(paired_surveys()):
        ingest(survey, f"event-{role}")
    extra = paired_surveys()[0].model_copy(update={"month": 2})
    ingest(extra, "unpaired-month")
    original = MLClient._request

    def malformed(self, method, path, payload=None):
        result = original(self, method, path, payload)
        if path != "/v2/analytics":
            return result
        cell = result["matrix"][0]["months"][0]
        if tamper == "monthly_actual":
            cell["actual"] = {"x": 30, "y": 30, "z": 30}
        elif tamper == "monthly_scenario":
            cell["scenario"] = {"x": 30, "y": 30, "z": 30}
        elif tamper == "factor":
            cell["x"]["factors"][0]["contribution"] = 19
            cell["x"]["factors"][0]["scenario_contribution"] = 19
            cell["x"]["actual_score"] = cell["x"]["score"] = 19
            cell["actual"]["x"] = cell["scenario"]["x"] = 19
            cell["actual"]["z"] = cell["scenario"]["z"] = cell["z"] = 17
        elif tamper == "evidence_cell":
            cell["x"]["evidence"].append(copy.deepcopy(cell["y"]["evidence"][0]))
        elif tamper == "aggregate_population":
            result["indices"].update(x=10, y=15, z=12.5,
                actual={"x": 10, "y": 15, "z": 12.5}, scenario={"x": 10, "y": 15, "z": 12.5})
        else:
            text_evidence = next(e for e in result["sections"]["mentor"]["evidence"] if e["kind"] == "text")
            text_evidence["quote"] = "Выдуманная цитата"
        return result

    monkeypatch.setattr(MLClient, "_request", malformed)
    assert client.post("/api/v2/analytics", json={}).status_code == 502


def test_new_identical_yandex_revision_is_preserved(client, monkeypatch):
    fake_ml(monkeypatch)
    monkeypatch.setenv("INGEST_KEY", "synthetic-test-key")
    get_settings.cache_clear()
    headers = {"x-ingest-key": "synthetic-test-key"}
    payload = {"jsonrpc": "2.0", "method": "submit", "id": 1,
        "params": {**mentor(), "response_id": "same-answer", "revision_id": "1"}}
    first = client.post("/api/v2/forms/yandex", json=payload, headers=headers).json()["result"]
    payload["params"]["revision_id"] = "2"
    second = client.post("/api/v2/forms/yandex", json=payload, headers=headers).json()["result"]
    assert second["status"] == "accepted"
    assert second["source_id"] != first["source_id"]
    assert source(second["source_id"])["record"]["version"] == 2
    assert source(first["source_id"])["raw"]["params"]["revision_id"] == "1"
    assert source(second["source_id"])["raw"] == payload
    assert client.post("/api/v2/forms/yandex", json=payload, headers=headers).json()["result"]["status"] == "duplicate"
    with connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pair_v2_deliveries").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM pair_v2_surveys").fetchone()[0] == 2


def test_restore_rechecks_after_interleaved_delivery(client, monkeypatch):
    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "event-1")
    saved = client.post("/api/v2/scenarios", json={}).json()
    original = MLClient._request

    def interleaved(self, method, path, payload=None):
        result = original(self, method, path, payload)
        if path == "/v2/methodology":
            ingest(SurveyInput(**mentor(ratings={"clarity": 5})), "concurrent-edit")
        return result

    monkeypatch.setattr(MLClient, "_request", interleaved)
    response = client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore')
    assert response.status_code == 409
    history = client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()["history"]
    assert [event["kind"] for event in history] == ["created"]


def test_invalid_scopes_and_actions_are_client_errors(client, monkeypatch):
    fake_ml(monkeypatch)
    assert client.post("/api/v2/analytics", json={"scope": {"type": "PAIR_MONTH", "pair_id": "missing", "start": 1}}).status_code == 404
    assert client.post("/api/v2/analytics", json={"selected": [{"action_id": "unknown", "scope": {"type": "GLOBAL"}}]}).status_code == 404
    assert client.post("/api/v2/analytics", json={"scope": {"type": "PAIR_MONTH", "pair_id": "missing", "start": 9}}).status_code == 422


def save_month_scenario(client, month=1, selected=None):
    pair_id = current_records()[0]["pair_id"]
    response = client.post("/api/v2/scenarios", json={"scope": {
        "type": "PAIR_MONTH", "pair_id": pair_id, "start": month}, "selected": selected or []})
    assert response.status_code == 200, response.text
    return response.json()


def test_unrelated_month_edit_preserves_scenario_and_restore_rebases_matrix(client, monkeypatch):
    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "month-1")
    saved = save_month_scenario(client)
    ingest(SurveyInput(**mentor(month=2, ratings={"clarity": 5})), "month-2")
    persisted = client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()
    assert persisted["stale"] is False
    assert persisted["result"] == saved["result"]
    restored = client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore').json()
    assert restored["data_version"] != saved["data_version"]
    assert restored["snapshot_data_version"] == saved["data_version"]
    assert restored["snapshot_result"] == saved["result"]
    assert restored["result"]["matrix"][0]["months"][1]["x"]["score"] == 0
    assert restored["result"]["indices"] == saved["result"]["indices"]
    assert client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()["result"] == saved["result"]
    # Active UI operations still compare the full current data version.
    assert client.post("/api/v2/analytics", json={"scope": saved["state"]["scope"],
        "expected_data_version": saved["data_version"]}).status_code == 409


def test_related_gained_survey_invalidates_month_scenario(client, monkeypatch):
    fake_ml(monkeypatch)
    mentor_record, intern_record = paired_surveys()
    ingest(mentor_record, "mentor")
    saved = save_month_scenario(client)
    ingest(intern_record, "newly-received-intern")
    assert client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()["stale"] is True
    assert client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore').status_code == 409


def test_registration_invalidates_global_and_preserves_unrelated_pair_scope(client, monkeypatch):
    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "month-1")
    local = save_month_scenario(client)
    global_scenario = client.post("/api/v2/scenarios", json={}).json()
    response = client.post("/api/v2/pairs", json={"intern_id": "СТ-999", "mentor_id": "Н-999", "department": "ИТ"})
    assert response.status_code == 200
    assert client.get(f'/api/v2/scenarios/{local["scenario_id"]}').json()["stale"] is False
    assert client.get(f'/api/v2/scenarios/{global_scenario["scenario_id"]}').json()["stale"] is True
    restored = client.post(f'/api/v2/scenarios/{local["scenario_id"]}/restore')
    assert restored.status_code == 200, restored.text
    assert len(restored.json()["result"]["matrix"]) == 2


@pytest.mark.parametrize("global_selection", [False, True])
def test_action_scopes_extend_saved_view_dependencies(client, monkeypatch, global_selection):
    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "month-1")
    ingest(SurveyInput(**mentor(month=2)), "month-2")
    pair_id = current_records()[0]["pair_id"]
    selection_scope = {"type": "GLOBAL"} if global_selection else {
        "type": "PAIR_RANGE", "pair_id": pair_id, "start": 2, "end": 3}
    saved = save_month_scenario(client, selected=[{"action_id": "action_mentor_clarity", "scope": selection_scope}])
    ingest(SurveyInput(**mentor(month=2, ratings={"clarity": 5})), "action-dependent-month")
    assert client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()["stale"] is True
    assert client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore').status_code == 409


def test_existing_blanket_staleness_migrates_without_rewriting_history(client, monkeypatch):
    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "month-1")
    local = save_month_scenario(client)
    global_scenario = client.post("/api/v2/scenarios", json={}).json()
    ingest(SurveyInput(**mentor(month=2)), "month-2")
    with connect() as db:
        db.execute("UPDATE pair_v2_scenarios SET stale=1")
        before = [tuple(row) for row in db.execute("SELECT * FROM pair_v2_scenario_events ORDER BY id")]
    init_store()
    assert client.get(f'/api/v2/scenarios/{local["scenario_id"]}').json()["stale"] is False
    assert client.get(f'/api/v2/scenarios/{global_scenario["scenario_id"]}').json()["stale"] is True
    with connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM pair_v2_scenario_events ORDER BY id")] == before
    assert client.get(f'/api/v2/scenarios/{local["scenario_id"]}').json()["result"] == local["result"]


def test_unrelated_delivery_during_restore_still_requires_full_current_cas(client, monkeypatch):
    from app import pair_api

    fake_ml(monkeypatch)
    ingest(SurveyInput(**mentor()), "month-1")
    saved = save_month_scenario(client)
    original = pair_api.analytics

    def interleaved(payload):
        result = original(payload)
        ingest(SurveyInput(**mentor(month=2)), "concurrent-unrelated-month")
        return result

    monkeypatch.setattr(pair_api, "analytics", interleaved)
    assert client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore').status_code == 409
    persisted = client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()
    assert persisted["stale"] is False
    assert [event["kind"] for event in persisted["history"]] == ["created"]
    monkeypatch.setattr(pair_api, "analytics", original)
    assert client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore').status_code == 200


def test_edit_of_other_pair_preserves_local_snapshot(client, monkeypatch):
    fake_ml(monkeypatch)
    target = ingest(SurveyInput(**mentor()), "target-pair")
    other = SurveyInput(**mentor(intern_id="СТ-999", mentor_id="Н-999"))
    ingest(other, "other-pair")
    saved = client.post("/api/v2/scenarios", json={"scope": {
        "type": "PAIR_MONTH", "pair_id": target["pair_id"], "start": 1}}).json()
    ingest(other.model_copy(update={"ratings": {"clarity": 5}}), "other-pair-edit")
    assert client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()["stale"] is False
    restored = client.post(f'/api/v2/scenarios/{saved["scenario_id"]}/restore')
    assert restored.status_code == 200, restored.text
    assert restored.json()["snapshot_result"] == saved["result"]
    other_row = next(row for row in restored.json()["result"]["matrix"] if row["intern_id"] == "СТ-999")
    assert other_row["months"][0]["x"]["score"] == 0


@pytest.mark.parametrize("received_month,stale", [(5, False), (2, True)])
def test_range_dependencies_include_missing_months_and_respect_boundaries(client, monkeypatch, received_month, stale):
    fake_ml(monkeypatch)
    target = ingest(SurveyInput(**mentor()), "month-1")
    saved = client.post("/api/v2/scenarios", json={"scope": {
        "type": "PAIR_RANGE", "pair_id": target["pair_id"], "start": 2, "end": 4}}).json()
    ingest(SurveyInput(**mentor(month=received_month)), "received-later")
    assert client.get(f'/api/v2/scenarios/{saved["scenario_id"]}').json()["stale"] is stale
