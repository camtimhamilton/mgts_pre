"""Статические проверки страницы импорта: файлы, разметка, контракт, синтаксис.

Браузер не нужен: проверяется содержимое статики и то, что import.js
разбирается Node. Поведение против реального бэкенда проверяется отдельно.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "static"
IMPORT_HTML = STATIC / "import.html"
IMPORT_JS = STATIC / "import.js"
STYLES = STATIC / "styles.css"
INDEX = STATIC / "index.html"

MAPPING_KEYS = (
    "intern_name",
    "mentor_name",
    "direction",
    "month",
    "role",
    "motivation",
    "clarity",
    "mentor",
    "workload",
    "inclusion",
    "future",
    "q_better",
    "q_barrier",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_import_page_files_exist():
    for path in (IMPORT_HTML, IMPORT_JS):
        assert path.is_file(), f"нет файла {path}"
        assert path.stat().st_size > 0, f"пустой файл {path}"


def test_import_html_has_required_controls():
    html = read(IMPORT_HTML)

    for element_id in (
        "file-input",
        "dropzone",
        "preview-btn",
        "commit-btn",
        "sheet-select",
        "default-role",
        "status-line",
        "preview-panel",
        "format-badge",
        "confidence-value",
        "warnings",
        "rows-total",
        "sample-head",
        "sample-body",
        "mapping",
        "commit-result",
        "result-counters",
        "errors-wrap",
        "errors",
    ):
        assert f'id="{element_id}"' in html, f"в import.html нет id={element_id}"

    # Имена полей совпадают с контрактом multipart/form-data.
    assert 'name="file"' in html
    assert 'name="sheet"' in html
    assert 'name="default_role"' in html

    assert 'type="file"' in html
    assert 'accept=".xlsx,.xlsm,.csv"' in html

    # Подключение статики и ссылка назад на дашборд.
    assert 'href="/static/styles.css"' in html
    assert 'src="/static/import.js"' in html
    assert 'type="module"' in html
    assert 'href="/"' in html
    assert "← на дашборд" in html

    # Основные действия и подписи по-русски.
    assert "Разобрать" in html
    assert "Загрузить" in html


def test_import_js_uses_contract_endpoints():
    js = read(IMPORT_JS)

    assert "/api/import/preview" in js
    assert "/api/import/commit" in js

    # multipart/form-data: файл, лист, роль по умолчанию, JSON-строка маппинга.
    assert "FormData" in js
    assert 'form.append("file"' in js
    assert 'form.append("sheet"' in js
    assert 'form.append("default_role"' in js
    assert 'form.append("mapping"' in js
    assert "JSON.stringify(collectMapping())" in js

    # 501 от бэкенда — штатная ситуация, её надо объяснить человеку.
    assert "501" in js

    # Все поля контракта собираются в маппинг.
    for key in MAPPING_KEYS:
        assert f'["{key}",' in js, f"в import.js нет поля маппинга {key}"

    # role — колонка файла, default_role — отдельный селект.
    assert "map-default-role" in js
    assert "Роль по умолчанию" in read(IMPORT_HTML)


def test_import_js_never_injects_html():
    js = read(IMPORT_JS)

    # Данные файла чужие: только textContent, никакой вставки разметки.
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert banned not in js, f"в import.js нельзя использовать {banned}"
    assert "textContent" in js


def test_import_js_syntax():
    node = shutil.which("node")
    if not node:
        pytest.skip("node не установлен — синтаксис import.js не проверить")

    proc = subprocess.run(
        [node, "--check", str(IMPORT_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_import_styles_are_appended():
    css = read(STYLES)

    for class_name in (
        ".import-panel",
        ".drop",
        ".import-actions",
        ".table-scroll",
        ".data-table",
        ".map-grid",
        ".counters",
        ".warn-list",
    ):
        assert class_name in css, f"в styles.css нет класса {class_name}"


def test_index_links_to_import_once():
    html = read(INDEX)

    assert 'href="/import"' in html
    assert html.count('href="/import"') == 1
    # Ссылка не должна ломать работающий дашборд.
    assert 'src="/static/dashboard.js"' in html
    assert 'href="/static/styles.css"' in html
