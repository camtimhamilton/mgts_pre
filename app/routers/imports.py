"""Импорт анкет из файла. Контракт: docs/import-contract.md.

Здесь заканчивается разбор (`app.importers`) и начинается запись: строка длинного
вида собирается в payload по маппингу, приводится к каноническому `Submission`
через `app.normalize.from_payload` и сохраняется `app.repo.upsert_submission`.
Других путей записи анкет из файла нет.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import repo, service
from ..broker import broker
from ..db import connect, write_lock
from ..importers import (
    PREVIEW_SAMPLE_SIZE,
    ImportFileError,
    ParsedFile,
    ParsedRow,
    UnsupportedFormatError,
    guess_mapping,
    parse_file,
)
from ..normalize import FACTORS, from_payload, parse_month, parse_role, parse_score

logger = logging.getLogger("pulse.import")
router = APIRouter(prefix="/api/import", tags=["import"])

MAX_ERRORS = 20

# Пересчёт после импорта: один на всех, параллельные загрузки не должны
# запускать несколько одинаковых проходов по базе.
_recompute_lock = asyncio.Lock()
_recompute_pending = False


async def _recompute_after_import() -> None:
    global _recompute_pending
    _recompute_pending = True
    if _recompute_lock.locked():
        return

    def work() -> dict[str, Any]:
        conn = connect()
        try:
            return service.recompute_all(conn)
        finally:
            conn.close()

    async with _recompute_lock:
        while _recompute_pending:
            _recompute_pending = False
            try:
                result = await asyncio.to_thread(work)
                broker.publish({"event": "program.ready", **result})
            except Exception:  # noqa: BLE001 — фоновая задача не должна ронять процесс
                logger.exception("пересчёт после импорта не удался")


# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------


def _load_parsed(file: UploadFile, content: bytes, sheet: str | None) -> ParsedFile:
    """Разбор файла с переводом ошибок парсера в коды контракта."""
    try:
        return parse_file(file.filename or "", content, sheet=sheet or None)
    except UnsupportedFormatError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except ImportFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _form_role(value: str | None) -> str | None:
    """Валидирует default_role из формы. Пустая строка = «не задано»."""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    if text not in ("intern", "mentor"):
        raise HTTPException(
            status_code=422,
            detail="default_role: ожидается intern или mentor",
        )
    return text


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sample(row: ParsedRow) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in row.values.items()}


def _column(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_text(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _score_cell(value: Any) -> Any:
    """«7 , 5» → «7,5»: оценки в выгрузках любят пробелы вокруг запятой."""
    if isinstance(value, str):
        return re.sub(r"\s*,\s*", ",", value.replace("\xa0", " ").strip())
    return value


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


@router.post("/preview")
async def preview(
    file: UploadFile = File(...),
    sheet: str | None = Form(None),
    default_role: str | None = Form(None),
) -> dict[str, Any]:
    """Разбирает файл и предлагает маппинг. В базу ничего не пишет."""
    content = await file.read()
    parsed = _load_parsed(file, content, sheet)
    role_default = _form_role(default_role) or "intern"
    mapping, warnings = guess_mapping(parsed, role_default)

    return {
        "file": {
            "name": parsed.name,
            "size": parsed.size,
            "sheets": parsed.sheets,
        },
        "format": parsed.format,
        "confidence": parsed.confidence,
        "columns": parsed.columns,
        "rows_total": len(parsed.rows),
        "sample": [_sample(row) for row in parsed.rows[:PREVIEW_SAMPLE_SIZE]],
        "mapping": mapping,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Commit
# ---------------------------------------------------------------------------


def _parse_mapping(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"mapping: некорректный JSON ({exc.msg})",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="mapping: ожидается JSON-объект")
    return parsed


def _row_payload(
    row: ParsedRow,
    spec: dict[str, Any],
    role_default: str,
) -> tuple[dict[str, Any], str | None]:
    """Строка длинного вида → payload для `normalize.from_payload`.

    Возвращает (payload, причина unmatched). Если причина есть, payload пуст.
    """
    values = row.values

    intern_column = _column(spec.get("intern_name"))
    name = values.get(intern_column) if intern_column else None
    if not _has_text(name):
        return {}, "не удалось определить имя стажёра"

    month_column = _column(spec.get("month"))
    month_value = values.get(month_column) if month_column else None
    month = parse_month(month_value)
    if month is None:
        return {}, "не удалось определить месяц"

    role_column = _column(spec.get("role"))
    role = parse_role(values.get(role_column)) if role_column else None
    role = role or row.role or role_default
    if role not in ("intern", "mentor"):
        role = "intern"

    payload: dict[str, Any] = {
        "intern_name": str(name).strip(),
        "month": month,
        "role": role,
    }

    mentor_column = _column(spec.get("mentor_name"))
    if mentor_column and _has_text(values.get(mentor_column)):
        payload["mentor_name"] = values[mentor_column]

    direction_column = _column(spec.get("direction"))
    if direction_column and _has_text(values.get(direction_column)):
        payload["direction"] = values[direction_column]

    factors = spec.get("factors")
    if not isinstance(factors, dict):
        factors = {}
    for factor in FACTORS:
        column = _column(factors.get(factor))
        if not column:
            continue
        value = values.get(column)
        # Нет колонки или пустая ячейка — фактора просто нет в scores.
        if _has_text(value):
            payload[factor] = _score_cell(value)

    open_block = spec.get("open")
    if not isinstance(open_block, dict):
        open_block = {}
    for key in ("q_better", "q_barrier"):
        column = _column(open_block.get(key))
        if column and _has_text(values.get(column)):
            payload[key] = values[column]

    return payload, None


def _has_scores(payload: dict[str, Any]) -> bool:
    """Есть ли в строке хотя бы одна распознанная оценка — для origin=mapped."""
    return any(
        factor in payload and parse_score(payload[factor]) is not None
        for factor in FACTORS
    )


def _raw_row(row: ParsedRow) -> str:
    data: dict[str, Any] = {"row": row.number, "values": row.values}
    if row.sheet:
        data["sheet"] = row.sheet
    if row.role:
        data["role"] = row.role
    return json.dumps(data, ensure_ascii=False, default=str)


def _error_text(row: ParsedRow, exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    if row.sheet:
        return f"{message} (лист «{row.sheet}», строка {row.number})"
    return message


def _commit_rows(
    conn: sqlite3.Connection,
    parsed: ParsedFile,
    spec: dict[str, Any],
    role_default: str,
    source: str,
) -> dict[str, Any]:
    created = updated = duplicates = unmatched = errors_total = 0
    errors: list[dict[str, Any]] = []

    for row in parsed.rows:
        # Своя точка сохранения: кривая строка не откатывает уже импортированные.
        conn.execute("SAVEPOINT import_row")
        failure: Exception | None = None
        try:
            payload, reason = _row_payload(row, spec, role_default)
            if reason is not None:
                repo.log_unmatched(conn, source, _raw_row(row), reason)
                unmatched += 1
            else:
                origin = "mapped" if _has_scores(payload) else "answer"
                submission = from_payload(payload, source=source, origin=origin)
                _submission_id, status = repo.upsert_submission(conn, submission)
                if status == "created":
                    created += 1
                elif status == "updated":
                    updated += 1
                else:
                    duplicates += 1
        except Exception as exc:  # ошибки строки не прерывают файл
            failure = exc
        finally:
            if failure is None:
                conn.execute("RELEASE import_row")
            else:
                conn.execute("ROLLBACK TO import_row")
                conn.execute("RELEASE import_row")

        if failure is not None:
            errors_total += 1
            if len(errors) < MAX_ERRORS:
                errors.append({"row": row.number, "error": _error_text(row, failure)})

    return {
        "created": created,
        "updated": updated,
        "duplicates": duplicates,
        "errors_total": errors_total,
        "unmatched": unmatched,
        "errors": errors,
    }


@router.post("/commit")
async def commit(
    file: UploadFile = File(...),
    mapping: str = Form(...),
    sheet: str | None = Form(None),
    default_role: str | None = Form(None),
    source: str = Form("import"),
) -> dict[str, Any]:
    """Пишет строки файла в базу по маппингу. Идемпотентно по (пара, роль, месяц)."""
    content = await file.read()
    parsed = _load_parsed(file, content, sheet)

    spec = _parse_mapping(mapping)
    form_role = _form_role(default_role)
    mapping_role = (
        _form_role(spec.get("default_role"))
        if isinstance(spec.get("default_role"), str)
        else None
    )
    role_default = form_role or mapping_role or "intern"
    source_name = (source or "").strip() or "import"

    conn = connect()
    try:
        with write_lock:
            # BEGIN IMMEDIATE сразу берёт блокировку записи: иначе SELECT+INSERT
            # внутри одной транзакции может получить «database is locked»
            # без ожидания, если параллельно кто-то успел записать.
            conn.execute("BEGIN IMMEDIATE")
            result = _commit_rows(conn, parsed, spec, role_default, source_name)
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Файл уже в базе; расчёт уходит в фон, ответ не ждёт модель.
    asyncio.create_task(_recompute_after_import())
    return result
