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


def test_icon_assets_exist():
    for name in ("icon.svg", "icon-maskable.svg", "favicon.ico",
                 "icon-16.png", "icon-32.png", "icon-48.png",
                 "icon-192.png", "icon-512.png",
                 "icon-maskable-512.png", "apple-touch-icon.png",
                 "manifest.webmanifest"):
        path = STATIC / name
        assert path.exists(), name
        assert path.stat().st_size > 0, name


def test_icon_svg_is_valid_and_has_pulse():
    import xml.etree.ElementTree as ET

    root = ET.parse(STATIC / "icon.svg").getroot()
    ns = "{http://www.w3.org/2000/svg}"
    assert root.tag == f"{ns}svg"
    assert root.get("viewBox") == "0 0 128 128"
    paths = root.findall(f".//{ns}path")
    assert paths, "нет линии пульса"
    assert all(p.get("stroke") for p in paths)


def test_manifest_lists_icons():
    import json

    data = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["name"] and data["short_name"]
    assert data["start_url"] == "/"
    sizes = {(i["sizes"], i["purpose"]) for i in data["icons"]}
    assert ("192x192", "any") in sizes
    assert ("512x512", "any") in sizes
    assert ("512x512", "maskable") in sizes
    for icon in data["icons"]:
        assert (STATIC / icon["src"].split("/")[-1]).exists(), icon["src"]


def test_pages_link_the_icon():
    for name in ("index.html", "form.html", "import.html"):
        html = (STATIC / name).read_text(encoding="utf-8")
        assert 'rel="icon"' in html, name
        assert "/static/icon.svg" in html, name
        assert "/static/apple-touch-icon.png" in html, name
        assert 'class="logo"' in html, name
        assert "◉" not in html, name


def test_icon_endpoints():
    with TestClient(app) as client:
        favicon = client.get("/favicon.ico")
        assert favicon.status_code == 200
        assert favicon.headers["content-type"].startswith("image/")
        assert favicon.content[:4] == b"\x00\x00\x01\x00"

        icon = client.get("/static/icon.svg")
        assert icon.status_code == 200
        assert "svg" in icon.headers["content-type"]

        manifest = client.get("/static/manifest.webmanifest")
        assert manifest.status_code == 200
