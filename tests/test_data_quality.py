"""Фаза 0: полнота данных, конфликты и защита топа списка."""

import uuid

from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.normalize import FACTORS

ALL_SIX = {key: 6 for key in FACTORS}


def _name(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:6]}"


def _payload(name: str, role: str, month: str, scores: dict, barrier: str = "") -> dict:
    return {
        "role": role,
        "month": month,
        "s_name": name,
        "s_mentor": "Проверкин Иван",
        "s_direction": "Тесты",
        "answers": {f"f_{key}": value for key, value in scores.items()},
        "open": {"q_better": "", "q_barrier": barrier},
    }


def _post(client, name: str, role: str, month: str, scores: dict, barrier: str = "") -> dict:
    response = client.post(
        "/api/ingest",
        json=_payload(name, role, month, scores, barrier),
        headers={"x-delivery-id": uuid.uuid4().hex},
    )
    assert response.status_code == 200
    body = response.json()
    return body.get("result", body)["submission"]


def _pair_id(name: str) -> int:
    conn = connect()
    try:
        row = conn.execute("SELECT id FROM pairs WHERE intern_name = ?", (name,)).fetchone()
        return int(row["id"])
    finally:
        conn.close()


def _card(client, name: str, month: str) -> dict:
    pair_id = _pair_id(name)
    recomputed = client.post(f"/api/pairs/{pair_id}/recompute", params={"month": month})
    assert recomputed.status_code == 200, recomputed.text
    response = client.get(f"/api/pairs/{pair_id}", params={"month": month})
    assert response.status_code == 200, response.text
    return response.json()


def test_complete_pair_has_full_coverage():
    name = _name("Полные Данные")
    with TestClient(app) as client:
        _post(client, name, "intern", "M4", ALL_SIX)
        _post(client, name, "mentor", "M4", ALL_SIX)
        card = _card(client, name, "M4")

    assert card["coverage"] == 1.0
    assert card["data_status"] == "complete"
    assert set(card["roles_present"]) == {"intern", "mentor"}
    assert card["conversation_required"] is False
    assert all(factor["state"] == "active" for factor in card["factors"])


def test_big_gap_marks_factor_conflicting():
    name = _name("Конфликт Данных")
    intern = dict(ALL_SIX, clarity=2)
    mentor = dict(ALL_SIX, clarity=8)
    with TestClient(app) as client:
        _post(client, name, "intern", "M4", intern)
        _post(client, name, "mentor", "M4", mentor)
        card = _card(client, name, "M4")

    clarity = next(factor for factor in card["factors"] if factor["key"] == "clarity")
    assert clarity["gap"] == 6
    assert clarity["state"] == "conflicting"
    assert card["gap_penalty"] > 0


def test_one_side_only_is_partial():
    name = _name("Без Наставника")
    with TestClient(app) as client:
        _post(client, name, "intern", "M4", ALL_SIX)
        card = _card(client, name, "M4")

    assert card["coverage"] == 1.0
    assert card["data_status"] == "partial"
    assert card["roles_present"] == ["intern"]


def test_two_factors_is_insufficient_and_sorts_last():
    weak = _name("Мало Данных")
    strong = _name("Много Данных")
    with TestClient(app) as client:
        # Два фактора с худшими оценками дают высокий индекс, но данных мало.
        _post(client, weak, "intern", "M4", {"clarity": 0, "motivation": 0})
        _post(client, strong, "intern", "M4", ALL_SIX)
        _post(client, strong, "mentor", "M4", ALL_SIX)

        weak_card = _card(client, weak, "M4")
        _card(client, strong, "M4")

        items = client.get("/api/pairs", params={"month": "M4"}).json()["items"]

    assert weak_card["data_status"] == "insufficient"
    assert weak_card["coverage"] < 0.5
    assert weak_card["risk"] > 0

    names = [item["intern_name"] for item in items]
    assert names.index(weak) > names.index(strong), "пара с неполными данными не должна быть выше"


def test_direct_intent_requires_conversation():
    name = _name("Хочу Уйти")
    with TestClient(app) as client:
        _post(
            client,
            name,
            "intern",
            "M4",
            dict(ALL_SIX, motivation=3),
            barrier="Уже хожу по собеседованиям, ищу другую работу.",
        )
        card = _card(client, name, "M4")

    assert card["conversation_required"] is True
    assert any(signal["key"] == "left_intent" for signal in card["signals"])
