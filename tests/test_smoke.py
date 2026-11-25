"""Смоук фазы 0: приёмник не должен падать и не должен дублировать."""

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def test_health_ok():
    with TestClient(app) as client:
        res = client.get("/api/health")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["db"] is True
        assert body["version"] == "0.5"


def test_index_and_form_are_served():
    with TestClient(app) as client:
        for path in ("/", "/form"):
            res = client.get(path)
            assert res.status_code == 200
            assert "text/html" in res.headers["content-type"]


def test_ingest_accepts_rpc_and_deduplicates():
    payload = {"jsonrpc": "2.0", "method": "submit", "params": {"a": 1}, "id": 7}
    headers = {"x-delivery-id": "smoke-1"}

    with TestClient(app) as client:
        first = client.post("/api/ingest", json=payload, headers=headers)
        assert first.status_code == 200
        assert first.json()["result"]["duplicate"] is False

        second = client.post("/api/ingest", json=payload, headers=headers)
        assert second.status_code == 200
        assert second.json()["result"]["duplicate"] is True
        assert second.json()["id"] == 7


def test_ingest_returns_200_even_on_garbage():
    """404 и 5XX заставляют Яндекс Формы повторять запрос до семи раз."""
    with TestClient(app) as client:
        res = client.post(
            "/api/ingest",
            content=b"{this is not json",
            headers={"content-type": "application/json"},
        )
        assert res.status_code == 200
        assert res.json()["status"] == "unparsed"


def test_fixtures_parse():
    for name in ("program.json", "pairs.json", "pair.json", "submissions.json"):
        data = json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))
        assert data, name
