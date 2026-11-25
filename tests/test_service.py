"""Пайплайн целиком: анкеты → снапшоты → API. Плюс живой приём через /api/ingest."""

import json
import time
import uuid

from fastapi.testclient import TestClient

from app import service
from app.db import connect, init_db
from app.generator import generate
from app.main import app
from app.normalize import FACTORS
from app.repo import upsert_submission

FORM = {
    "f_motivation": 4,
    "f_clarity": 2,
    "f_mentor": 5,
    "f_workload": 5,
    "f_inclusion": 6,
    "f_future": 4,
}


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


_SEEDED = False


def _ensure_data() -> None:
    """Свой набор данных: тесты не должны зависеть от порядка запуска и чужих модулей."""
    global _SEEDED
    if _SEEDED:
        return
    init_db()
    conn = connect()
    try:
        for submission in generate(seed=7, pairs=3):
            upsert_submission(conn, submission)
        conn.commit()
    finally:
        conn.close()
    _SEEDED = True


def _pay(name: str, month: str, role: str, mentor: str) -> dict:
    return {
        "role": role,
        "month": month,
        "s_name": name,
        "s_mentor": mentor,
        "s_direction": "Тесты",
        "answers": dict(FORM),
        "open": {
            "q_better": "Стало понятнее, но не во всём.",
            "q_barrier": "Не понимаю, что от меня хотят, и не хватает обратной связи.",
        },
    }


def _pair_id(name: str) -> int:
    conn = connect()
    try:
        return int(
            conn.execute("SELECT id FROM pairs WHERE intern_name = ?", (name,)).fetchone()["id"]
        )
    finally:
        conn.close()


def test_recompute_all_builds_snapshots_and_scenarios():
    _ensure_data()
    with TestClient(app):
        conn = connect()
        try:
            service.recompute_all(conn)
            total = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
            assert total > 0

            bad = conn.execute("SELECT COUNT(*) FROM snapshots WHERE risk < 0 OR risk > 100").fetchone()[0]
            assert bad == 0

            for row in conn.execute("SELECT data_json FROM snapshots LIMIT 20").fetchall():
                data = json.loads(row["data_json"])
                assert set(FACTORS) >= {factor["key"] for factor in data["factors"]}
                assert 0 <= data["risk"] <= 100
                assert data["band"] in ("low", "watch", "critical")
                assert len(data["reasons"]) <= 3
                assert 2 <= len(data["steps"]) <= 3
                if data["month"] != "M6":
                    assert data["scenarios"]["base"], "у не-последнего месяца должен быть сценарий"
        finally:
            conn.close()


def test_risk_equals_sum_of_contributions():
    _ensure_data()
    with TestClient(app):
        conn = connect()
        try:
            service.recompute_all(conn)
            for row in conn.execute("SELECT data_json FROM snapshots").fetchall():
                data = json.loads(row["data_json"])
                total = sum(factor["points"] for factor in data["factors"]) + data["gap_penalty"] + data["text_penalty"]
                if data["risk"] >= 100:
                    assert total >= 100
                else:
                    assert total == data["risk"]
        finally:
            conn.close()


def test_api_serves_database_not_fixture():
    _ensure_data()
    with TestClient(app) as client:
        conn = connect()
        try:
            service.recompute_all(conn)
            month = service.latest_month(conn)
        finally:
            conn.close()

        program = client.get("/api/program")
        assert program.status_code == 200
        assert program.headers["x-data-source"] == "db"
        body = program.json()
        assert body["kpi"]["total_pairs"] == len(body["heatmap"])
        assert sum(body["bands"].values()) == body["kpi"]["total_pairs"]

        pairs = client.get("/api/pairs").json()
        assert pairs["items"]
        first = pairs["items"][0]
        assert first["urgency"] >= first["risk"]

        card = client.get(f"/api/pairs/{first['id']}", params={"month": month}).json()
        assert card["intern_name"] == first["intern_name"]
        assert card["risk"] == first["risk"]
        assert card["delta"] == first["delta"]


def test_m6_has_no_forecast():
    _ensure_data()
    with TestClient(app):
        conn = connect()
        try:
            service.recompute_all(conn)
            rows = conn.execute("SELECT data_json FROM snapshots").fetchall()
        finally:
            conn.close()

    for row in rows:
        data = json.loads(row["data_json"])
        if data["month"] == "M6":
            assert data["scenarios"]["base"] == []
            assert data["scenarios"]["with_actions"] == []
        else:
            assert data["scenarios"]["base"]


def test_ingest_writes_submission_and_snapshot():
    name = _unique("Ингест Тестов")
    mentor = _unique("Наставник")
    payload = _pay(name, "M1", "intern", mentor)

    with TestClient(app) as client:
        delivery = uuid.uuid4().hex
        first = client.post(
            "/api/ingest", json=payload, headers={"x-delivery-id": delivery}
        )
        assert first.status_code == 200
        body = first.json()
        assert body["submission"]["status"] == "created"
        assert body["analysis"] == "queued"

        pair_id = _pair_id(name)

        # Ждём фоновый пересчёт, но не бесконечно.
        deadline = time.time() + 5
        card = None
        while time.time() < deadline:
            response = client.get(f"/api/pairs/{pair_id}", params={"month": "M1"})
            if response.status_code == 200:
                card = response.json()
                break
            time.sleep(0.05)
        assert card is not None, "снапшот не появился за 5 секунд"
        assert card["risk"] > 0
        assert card["observation"]["months"] >= 1

        # Повторная отправка того же запроса — дубликат, второй анкеты нет.
        second = client.post(
            "/api/ingest", json=payload, headers={"x-delivery-id": delivery}
        )
        assert second.json()["duplicate"] is True

        conn = connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE pair_id = ? AND month = 'M1'",
                (pair_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 1


def test_ingest_unmatched_is_logged_not_fatal():
    with TestClient(app) as client:
        response = client.post(
            "/api/ingest",
            json={"role": "intern", "month": "M9", "s_name": "Кто-то"},
            headers={"x-delivery-id": uuid.uuid4().hex},
        )
        assert response.status_code == 200
        assert response.json()["submission"]["status"] == "unmatched"

    conn = connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE status = 'unmatched'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert row >= 1


def test_mentor_only_month_still_computes():
    name = _unique("Только Наставник")
    mentor = _unique("Наставник")
    with TestClient(app) as client:
        response = client.post(
            "/api/ingest",
            json=_pay(name, "M2", "mentor", mentor),
            headers={"x-delivery-id": uuid.uuid4().hex},
        )
        assert response.json()["submission"]["status"] == "created"

        pair_id = _pair_id(name)
        recomputed = client.post(f"/api/pairs/{pair_id}/recompute", params={"month": "M2"})
        assert recomputed.status_code == 200
        data = recomputed.json()
        assert data["risk"] > 0

        card = client.get(f"/api/pairs/{pair_id}", params={"month": "M2"}).json()
        assert card["gap_penalty"] == 0
        assert all(factor["score_mentor"] is not None for factor in card["factors"])
