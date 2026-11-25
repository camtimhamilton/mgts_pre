"""Приём ровно в том виде, в каком его пришлёт Яндекс Форма.

Форма настроена как JSON-RPC POST: params — плоский объект с нашими ключами.
Ключ приёма (если задан) приходит полем params.key, потому что JSON-RPC-интеграция
Яндекса не умеет произвольные заголовки.
"""

import uuid

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import connect
from app.main import app


def _rpc_params(name: str, month: str = "M2", role: str = "intern") -> dict:
    return {
        "s_name": name,
        "s_mentor": "Проверкин Иван",
        "s_direction": "Техподдержка",
        "s_month": month,
        "s_role": role,
        "f_motivation": "5",
        "f_clarity": "3",
        "f_mentor": "6",
        "f_workload": "5",
        "f_inclusion": "6",
        "f_future": "4",
        "q_better": "Стало понятнее, что делать.",
        "q_barrier": "Всё ещё не хватает обратной связи.",
    }


def _rpc(params: dict) -> dict:
    return {"jsonrpc": "2.0", "method": "submit", "id": 1, "params": params}


def test_yandex_rpc_creates_submission():
    name = f"Яндекс Тестов {uuid.uuid4().hex[:8]}"
    with TestClient(app) as client:
        response = client.post(
            "/api/ingest",
            json=_rpc(_rpc_params(name)),
            headers={"x-delivery-id": uuid.uuid4().hex},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["jsonrpc"] == "2.0"
        assert body["id"] == 1
        assert body["result"]["submission"]["status"] == "created"
        assert body["result"]["analysis"] == "queued"

    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT s.month, s.role
              FROM submissions s
              JOIN pairs p ON p.id = s.pair_id
             WHERE p.intern_name = ?
            """,
            (name,),
        ).fetchone()
    finally:
        conn.close()
    assert row["month"] == "M2"
    assert row["role"] == "intern"


def test_yandex_rpc_is_idempotent_on_retry():
    name = f"Повтор Тестов {uuid.uuid4().hex[:8]}"
    delivery = uuid.uuid4().hex
    payload = _rpc(_rpc_params(name, "M3"))

    with TestClient(app) as client:
        first = client.post("/api/ingest", json=payload, headers={"x-delivery-id": delivery})
        second = client.post("/api/ingest", json=payload, headers={"x-delivery-id": delivery})
        assert first.json()["result"]["duplicate"] is False
        assert second.json()["result"]["duplicate"] is True


def test_names_are_matched_without_case_and_spaces():
    base = f"Иванов Пётр {uuid.uuid4().hex[:6]}"
    with TestClient(app) as client:
        client.post("/api/ingest", json=_rpc(_rpc_params(base)), headers={"x-delivery-id": uuid.uuid4().hex})
        client.post(
            "/api/ingest",
            json=_rpc(_rpc_params(f"  {base.lower()}  ")),
            headers={"x-delivery-id": uuid.uuid4().hex},
        )

    conn = connect()
    try:
        pairs = conn.execute(
            "SELECT COUNT(*) FROM pairs WHERE intern_name LIKE ?", (f"%{base}%",)
        ).fetchone()[0]
        # Вторая отправка — тот же месяц и роль, поэтому это правка, а не новая пара.
        same = conn.execute(
            "SELECT COUNT(*) FROM submissions WHERE pair_id = (SELECT id FROM pairs WHERE intern_name = ?)",
            (base,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert pairs == 1
    assert same == 1


def test_ingest_key_can_come_in_params():
    settings = get_settings()
    previous = settings.ingest_key
    settings.ingest_key = "s3cret"
    try:
        name = f"Ключ Тестов {uuid.uuid4().hex[:8]}"
        with TestClient(app) as client:
            without = client.post(
                "/api/ingest",
                json=_rpc(_rpc_params(name)),
                headers={"x-delivery-id": uuid.uuid4().hex},
            )
            assert without.json()["result"]["status"] == "rejected"

            params = _rpc_params(name)
            params["key"] = "s3cret"
            with_key = client.post(
                "/api/ingest",
                json=_rpc(params),
                headers={"x-delivery-id": uuid.uuid4().hex},
            )
            assert with_key.json()["result"]["submission"]["status"] == "created"
    finally:
        settings.ingest_key = previous
