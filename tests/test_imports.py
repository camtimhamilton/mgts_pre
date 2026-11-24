"""Импорт анкет из файла: контракт preview/commit и зоопарк форматов.

Тесты не рассчитывают на пустую базу: у файлов зоопарка уникальные имена, а
сгенерированные строки получают случайный суффикс. База — временная (tests/conftest.py).
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.importers import parse_file
from app.main import app

ROOT = Path(__file__).resolve().parents[1]
ZOO = ROOT / "fixtures" / "zoo"

# (файл, ожидаемый формат)
CASES = [
    ("long_ru.csv", "long"),
    ("long_cp1251.csv", "long"),
    ("wide_months.xlsx", "wide"),
    ("two_sheets_roles.xlsx", "two_sheet"),
    ("messy_headers.csv", "long"),
]


def _files(name: str) -> dict:
    path = ZOO / name
    return {"file": (name, path.read_bytes(), "application/octet-stream")}


def _upload_bytes(filename: str, content: bytes) -> dict:
    return {"file": (filename, content, "application/octet-stream")}


def _preview(client: TestClient, name: str, **data):
    return client.post("/api/import/preview", files=_files(name), data=data)


def _commit(client: TestClient, name: str, mapping: dict, **data):
    payload = {"mapping": json.dumps(mapping, ensure_ascii=False), **data}
    return client.post("/api/import/commit", files=_files(name), data=payload)


def _db_counts() -> tuple[int, int, int]:
    conn = connect()
    try:
        return tuple(
            int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("pairs", "submissions", "ingest_log")
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_format"), CASES)
def test_preview_zoo(name: str, expected_format: str):
    with TestClient(app) as client:
        response = _preview(client, name)
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["format"] == expected_format
    assert body["file"]["name"] == name
    assert body["file"]["size"] > 0
    assert 0.0 < body["confidence"] <= 1.0
    assert body["rows_total"] > 0
    assert body["columns"]
    assert 0 < len(body["sample"]) <= min(5, body["rows_total"])

    mapping = body["mapping"]
    assert mapping["intern_name"], f"{name}: не угадано имя стажёра"
    assert mapping["month"], f"{name}: не угадан месяц"
    assert mapping["default_role"] == "intern"
    assert set(mapping["factors"]) == {
        "motivation", "clarity", "mentor", "workload", "inclusion", "future",
    }
    assert set(mapping["open"]) == {"q_better", "q_barrier"}

    assert body["warnings"], f"{name}: предупреждения не должны быть пустыми"
    assert all(isinstance(warning, str) and warning for warning in body["warnings"])

    if name.endswith(".csv"):
        assert body["file"]["sheets"] == []
    else:
        assert body["file"]["sheets"]
    if expected_format == "two_sheet":
        assert set(body["file"]["sheets"]) == {"Стажёры", "Наставники"}


def test_wide_is_collapsed_to_long_view():
    with TestClient(app) as client:
        body = _preview(client, "wide_months.xlsx").json()

    assert body["format"] == "wide"
    assert body["rows_total"] == 18  # 6 анкет × 3 месяца
    assert body["mapping"]["month"] == "Месяц"
    assert body["mapping"]["factors"]["clarity"] == "Ясность"

    months = {row.get("Месяц") for row in body["sample"]}
    assert {"M1", "M2", "M3"} <= months
    # Имена колонок уже схлопнуты: суффикса месяца в них нет.
    assert "Ясность" in body["columns"]
    assert not any(re.search(r"\bM[1-6]$", column) for column in body["columns"])


def test_two_sheet_roles_come_from_sheet_names():
    parsed = parse_file(
        "two_sheets_roles.xlsx",
        (ZOO / "two_sheets_roles.xlsx").read_bytes(),
    )
    assert parsed.format == "two_sheet"
    assert len(parsed.rows) == 8
    assert {row.role for row in parsed.rows} == {"intern", "mentor"}

    with TestClient(app) as client:
        body = _preview(client, "two_sheets_roles.xlsx").json()
    sample_roles = {row.get("Роль") for row in body["sample"]}
    assert {"intern", "mentor"} <= sample_roles


def test_preview_does_not_write_db():
    before = _db_counts()
    with TestClient(app) as client:
        for name, _expected in CASES:
            response = _preview(client, name)
            assert response.status_code == 200, response.text
    after = _db_counts()
    assert after == before, "preview не должен писать в pairs/submissions/ingest_log"


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "expected_format"), CASES)
def test_commit_is_idempotent(name: str, expected_format: str):
    with TestClient(app) as client:
        preview = _preview(client, name).json()
        mapping = preview["mapping"]

        first = _commit(client, name, mapping, source="zoo-test")
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["created"] > 0, f"{name}: первый commit ничего не создал"
        assert first_body["errors_total"] == 0
        assert (
            first_body["created"]
            + first_body["updated"]
            + first_body["duplicates"]
            + first_body["unmatched"]
            + first_body["errors_total"]
            == preview["rows_total"]
        )

        second = _commit(client, name, mapping, source="zoo-test")
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["created"] == 0
        assert second_body["duplicates"] > 0
        assert second_body["duplicates"] == (
            first_body["created"] + first_body["updated"] + first_body["duplicates"]
        )
        assert second_body["unmatched"] == first_body["unmatched"]

    if name == "two_sheets_roles.xlsx":
        # Роль из имени листа должна доехать до базы, а не потеряться.
        conn = connect()
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT s.role
                  FROM submissions s
                  JOIN pairs p ON p.id = s.pair_id
                 WHERE p.intern_name IN ('Морозов Денис', 'Максимова Ольга')
                """
            ).fetchall()
        finally:
            conn.close()
        assert {row["role"] for row in rows} == {"intern", "mentor"}


def test_bad_rows_go_to_unmatched_and_do_not_break_others():
    suffix = uuid.uuid4().hex[:8]
    valid_name = f"Уникумов Иван {suffix}"
    no_month_name = f"Безмесяцев Олег {suffix}"
    source = f"zoo-unmatched-{suffix}"
    content = (
        "ФИО;Наставник;Направление;Месяц;Мотивация;Ясность\n"
        ";Сидоров Пётр;Разработка;M1;7;8\n"
        f"{valid_name};Сидоров Пётр;Разработка;M1;8;9\n"
        f"{no_month_name};Сидоров Пётр;Разработка;;7;7\n"
    ).encode("utf-8")

    with TestClient(app) as client:
        preview_response = client.post(
            "/api/import/preview",
            files=_upload_bytes("broken-row.csv", content),
        )
        assert preview_response.status_code == 200, preview_response.text
        preview = preview_response.json()
        assert preview["rows_total"] == 3

        response = client.post(
            "/api/import/commit",
            files=_upload_bytes("broken-row.csv", content),
            data={"mapping": json.dumps(preview["mapping"], ensure_ascii=False), "source": source},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unmatched"] == 2  # без имени и без месяца
    assert body["created"] == 1
    assert body["errors_total"] == 0

    conn = connect()
    try:
        unmatched = conn.execute(
            "SELECT COUNT(*) FROM ingest_log WHERE source = ? AND status = 'unmatched'",
            (source,),
        ).fetchone()[0]
        imported = conn.execute(
            """
            SELECT COUNT(*)
              FROM submissions s
              JOIN pairs p ON p.id = s.pair_id
             WHERE p.intern_name = ?
            """,
            (valid_name,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert unmatched == 2, "плохие строки должны попасть в ingest_log"
    assert imported == 1, "хорошая строка в том же файле должна импортироваться"


def test_commit_keeps_only_mapped_factors_and_marks_origin_mapped():
    suffix = uuid.uuid4().hex[:8]
    name = f"Частиков Пётр {suffix}"
    content = (
        "ФИО;Месяц;Мотивация;Ясность\n"
        f"{name};M1;8,5;7\n"
    ).encode("utf-8")

    with TestClient(app) as client:
        preview = client.post(
            "/api/import/preview",
            files=_upload_bytes("partial-factors.csv", content),
        ).json()
        source = f"zoo-partial-{suffix}"
        response = client.post(
            "/api/import/commit",
            files=_upload_bytes("partial-factors.csv", content),
            data={
                "mapping": json.dumps(preview["mapping"], ensure_ascii=False),
                "source": source,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["created"] == 1

    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT s.payload_json, s.source
              FROM submissions s
              JOIN pairs p ON p.id = s.pair_id
             WHERE p.intern_name = ?
            """,
            (name,),
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(row["payload_json"])
    assert row["source"] == source
    assert payload["origin"] == "mapped"
    assert payload["scores"] == {"motivation": 8.5, "clarity": 7.0}
    # Колонок остальных факторов в файле нет — выдумывать оценки нельзя.
    assert set(payload["scores"]) == {"motivation", "clarity"}
    assert payload["role"] == "intern"


# ---------------------------------------------------------------------------
# Ошибки контракта
# ---------------------------------------------------------------------------


def test_unsupported_extension_returns_415():
    with TestClient(app) as client:
        response = client.post(
            "/api/import/preview",
            files=_upload_bytes("surveys.txt", b"a,b,c\n1,2,3\n"),
        )
    assert response.status_code == 415


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("empty.csv", b""),
        ("broken.xlsx", b"this is not a zip archive"),
    ],
)
def test_unreadable_file_returns_422(filename: str, content: bytes):
    with TestClient(app) as client:
        response = client.post(
            "/api/import/preview",
            files=_upload_bytes(filename, content),
        )
    assert response.status_code == 422


def test_bad_mapping_json_returns_422():
    with TestClient(app) as client:
        response = client.post(
            "/api/import/commit",
            files=_files("long_ru.csv"),
            data={"mapping": "{not a json"},
        )
    assert response.status_code == 422
