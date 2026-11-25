"""Экран: статические проверки разметки и связей с API.

Браузера в тестах нет, поэтому проверяем контракт страницы: что она ссылается
на нужные модули, что ручки существуют и что обязательные блоки есть в разметке.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"


def test_static_files_exist():
    for name in ("index.html", "dashboard.js", "dashboard.css", "styles.css"):
        assert (STATIC / name).exists(), name


def test_page_references_dashboard_only():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "/static/dashboard.js" in html
    assert "/static/dashboard.css" in html
    assert "app.js" not in html


def test_required_blocks_present():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in ("id=\"kpi\"", "id=\"list\"", "id=\"heatmap\"", "id=\"causes\"",
                   "id=\"card\"", "id=\"month\"", "id=\"originals\"", "id=\"log\""):
        assert marker in html, marker


def test_script_calls_contract_endpoints():
    script = (STATIC / "dashboard.js").read_text(encoding="utf-8")
    for endpoint in ("/api/program", "/api/pairs", "/api/events", "/api/ingest/log"):
        assert endpoint in script, endpoint
    assert "EventSource" in script


def test_index_served_with_script():
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "dashboard.js" in response.text
