"""HTTP-слой.

Фаза 2: приёмник доводит анкету до базы, расчёт идёт в фоне, API отдаёт снапшоты.
Если снапшотов нет — отдаём фикстуры с заголовком X-Data-Source: fixture, чтобы
фронт мог строиться до появления данных и никто не путал демо с расчётом.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from fastapi.responses import JSONResponse, StreamingResponse

from . import repo, service
from .broker import broker
from .config import get_settings
from .db import connect, ping, write_lock
from .normalize import from_payload

logger = logging.getLogger("pulse.api")
router = APIRouter()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

# Пары, которые уже считаются, и те, что попросили пересчитать ещё раз.
# Если анкеты стажёра и наставника пришли в одну секунду, второй запрос не должен
# потеряться: он ставит пару в очередь, и после текущего прохода она считается снова.
_inflight: set[tuple[int, str]] = set()
_pending: set[tuple[int, str]] = set()


# ---------------------------------------------------------------- служебное


@router.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": "pulse",
        "version": "0.5",
        "phase": 3,
        "db": ping(),
        "view": settings.app_view,
        "team": settings.team_name,
        "demo_mode": settings.demo_mode,
        "llm_mode": settings.llm_mode,
        "analysis_mode": settings.analysis_mode,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- приёмник


def _log_ingest(
    source: str,
    delivery_id: str | None,
    status: str,
    raw: str,
    headers: dict[str, str],
    error: str | None,
) -> tuple[int, bool]:
    """Пишет сырой запрос. Возвращает (id, duplicate).

    Дедупликация по x-delivery-id: Яндекс Формы могут прислать один ответ дважды.
    Проверка и вставка — под одной блокировкой, иначе два одновременных запроса
    с одним delivery_id упадут на UNIQUE.
    """
    conn = connect()
    try:
        with write_lock:
            if delivery_id:
                row = conn.execute(
                    "SELECT id FROM ingest_log WHERE delivery_id = ?", (delivery_id,)
                ).fetchone()
                if row:
                    return int(row["id"]), True

            cursor = conn.execute(
                """
                INSERT INTO ingest_log (source, delivery_id, status, raw, headers_json, error)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    source,
                    delivery_id,
                    status,
                    raw[:20000],
                    json.dumps(headers, ensure_ascii=False),
                    error,
                ),
            )
            conn.commit()
        return int(cursor.lastrowid or 0), False
    finally:
        conn.close()


def _payload_from_request(parsed: Any, is_rpc: bool) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    if is_rpc:
        params = parsed.get("params")
        return params if isinstance(params, dict) else None
    return parsed


async def _recompute_in_background(pair_id: int, month: str) -> None:
    key = (pair_id, month)
    if key in _inflight:
        _pending.add(key)
        return
    _inflight.add(key)

    def work() -> dict | None:
        conn = connect()
        try:
            return service.recompute_pair_month(conn, pair_id, month)
        finally:
            conn.close()

    try:
        while True:
            data = await asyncio.to_thread(work)
            if data:
                broker.publish(
                    {
                        "event": "snapshot.ready",
                        "pair_id": pair_id,
                        "month": month,
                        "risk": data["risk"],
                        "band": data["band"],
                    }
                )
            if key not in _pending:
                break
            _pending.discard(key)
    except Exception:  # noqa: BLE001 — фоновая задача не должна ронять процесс
        logger.exception("не удалось пересчитать пару %s за %s", pair_id, month)
    finally:
        _pending.discard(key)
        _inflight.discard(key)


@router.post("/api/ingest")
async def ingest(request: Request) -> JSONResponse:
    """Приём ответа формы.

    Всегда 200: по документации Яндекса 404 и 5XX заставляют форму повторять запрос.
    Анкета пишется в базу сразу, а расчёт (с вызовами модели) уходит в фон —
    иначе не уложиться в пятисекундный таймаут формы.
    """
    settings = get_settings()
    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    raw = body.decode("utf-8", errors="replace")

    parsed: Any = None
    is_rpc = False
    error = None
    try:
        parsed = json.loads(raw) if raw.strip() else None
        is_rpc = isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0"
    except json.JSONDecodeError as exc:
        error = f"json: {exc.msg}"

    payload = _payload_from_request(parsed, is_rpc)

    # Ключ ищем в заголовке или в params.key: JSON-RPC-интеграция Яндекс Форм
    # не поддерживает произвольные заголовки, а поле в теле — поддерживает.
    supplied_key = headers.get("x-ingest-key")
    if not supplied_key and isinstance(payload, dict):
        supplied_key = payload.get("key")
    key_ok = not settings.ingest_key or supplied_key == settings.ingest_key

    if error:
        status = "unparsed"
    else:
        status = "accepted" if key_ok else "rejected"
    if not key_ok and not error:
        error = "bad ingest key"

    # Секрет не должен оседать в журнале приёма.
    logged_raw = raw
    if settings.ingest_key and settings.ingest_key in logged_raw:
        logged_raw = logged_raw.replace(settings.ingest_key, "***")

    row_id, duplicate = _log_ingest(
        source="webhook",
        delivery_id=headers.get("x-delivery-id"),
        status=status,
        raw=logged_raw,
        headers=headers,
        error=error,
    )

    result: dict[str, Any] = {
        "ok": True,
        "id": row_id,
        "duplicate": duplicate,
        "status": "duplicate" if duplicate else status,
    }

    if key_ok and payload is not None and not duplicate:
        try:
            submission = from_payload(payload, source="form")
            conn = connect()
            try:
                with write_lock:
                    submission_id, submission_status = repo.upsert_submission(conn, submission)
                row = conn.execute(
                    "SELECT pair_id FROM submissions WHERE id = ?", (submission_id,)
                ).fetchone()
                conn.commit()
            finally:
                conn.close()

            result["submission"] = {"id": submission_id, "status": submission_status}
            if row is not None:
                asyncio.create_task(
                    _recompute_in_background(int(row["pair_id"]), submission.month)
                )
                result["analysis"] = "queued"
        except ValueError as exc:
            conn = connect()
            try:
                with write_lock:
                    repo.log_unmatched(conn, "webhook", raw, str(exc))
                    conn.commit()
            finally:
                conn.close()
            result["submission"] = {"status": "unmatched", "error": str(exc)}

    if is_rpc:
        return JSONResponse(
            {"jsonrpc": "2.0", "result": result, "id": parsed.get("id")},
            status_code=200,
        )
    return JSONResponse(result, status_code=200)


@router.get("/api/ingest/log")
def ingest_log(limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT id, source, status, delivery_id, error, received_at,
                   length(raw) AS size, raw
            FROM ingest_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM ingest_log").fetchone()[0]
    finally:
        conn.close()

    items = []
    for row in rows:
        item = dict(row)
        item["raw"] = (item.get("raw") or "")[:1200]
        items.append(item)
    return {"total": total, "items": items}


# ---------------------------------------------------------------- контракт


def _fixture(name: str) -> Any:
    path = FIXTURES / name
    if not path.exists():
        return {"error": "fixture missing", "name": name}
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_response(name: str, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        _fixture(name), status_code=status_code, headers={"X-Data-Source": "fixture"}
    )


def _db_response(data: Any) -> JSONResponse:
    return JSONResponse(data, headers={"X-Data-Source": "db"})


def _use_fixture(conn) -> bool:
    return get_settings().analysis_mode == "demo" and not service.has_snapshots(conn)


@router.get("/api/program")
def program(month: str | None = Query(default=None, pattern=r"^M[1-6]$")) -> JSONResponse:
    conn = connect()
    try:
        if _use_fixture(conn):
            return _fixture_response("program.json")
        target = month or service.latest_month(conn)
        return _db_response(service.program(conn, target or "M1"))
    finally:
        conn.close()


@router.get("/api/pairs")
def pairs(month: str | None = Query(default=None, pattern=r"^M[1-6]$")) -> JSONResponse:
    conn = connect()
    try:
        if _use_fixture(conn):
            return _fixture_response("pairs.json")
        target = month or service.latest_month(conn)
        return _db_response(service.list_pairs(conn, target or "M1"))
    finally:
        conn.close()


@router.post("/api/recompute")
def recompute() -> dict[str, Any]:
    conn = connect()
    try:
        return service.recompute_all(conn)
    finally:
        conn.close()


@router.post("/api/pairs/{pair_id}/recompute")
def recompute_pair(pair_id: int, month: str | None = Query(default=None, pattern=r"^M[1-6]$")) -> JSONResponse:
    conn = connect()
    try:
        if month:
            data = service.recompute_pair_month(conn, pair_id, month)
            if data is None:
                return JSONResponse(
                    {"error": "нет анкет за этот месяц", "pair_id": pair_id, "month": month},
                    status_code=404,
                )
            return _db_response(_summary(data))
        data = service.recompute_pair(conn, pair_id)
        return _db_response({"pair_id": pair_id, "snapshots": len(data)})
    finally:
        conn.close()


def _summary(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "pair_id": data["id"],
        "month": data["month"],
        "risk": data["risk"],
        "delta": data["delta"],
        "band": data["band"],
        "factors": len(data.get("factors", [])),
        "steps": len(data.get("steps", [])),
    }


@router.get("/api/pairs/{pair_id}/submissions")
def submissions(pair_id: int) -> JSONResponse:
    conn = connect()
    try:
        if _use_fixture(conn):
            return _fixture_response("submissions.json")
        return _db_response(service.submissions_for(conn, pair_id))
    finally:
        conn.close()


@router.get("/api/pairs/{pair_id}")
def pair(pair_id: int, month: str | None = Query(default=None, pattern=r"^M[1-6]$")) -> JSONResponse:
    conn = connect()
    try:
        if _use_fixture(conn):
            return _fixture_response("pair.json")
        target = month or service.latest_month(conn)
        data = service.pair_card(conn, pair_id, target or "M1")
        if data is None:
            return JSONResponse(
                {"error": "нет снапшота", "pair_id": pair_id, "month": target},
                status_code=404,
            )
        return _db_response(data)
    finally:
        conn.close()


# ---------------------------------------------------------------- события


class ScenarioSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_ids: list[str] = Field(max_length=50)


@router.post("/api/pairs/{pair_id}/scenario")
def scenario(pair_id: int, body: ScenarioSelection, month: str = Query(pattern=r"^M[1-6]$")) -> JSONResponse:
    from .ml_client import MLUnavailable
    if get_settings().analysis_mode != "ml":
        return JSONResponse({"code": "scenario_requires_ml_mode"}, status_code=409)
    conn = connect()
    try:
        return _db_response(service.calculate_scenario(conn, pair_id, month, body.action_ids))
    except MLUnavailable as error:
        return JSONResponse({"code": error.code}, status_code=503)
    finally:
        conn.close()


@router.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    queue = broker.subscribe()

    async def stream():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield (
                    f"event: {event.get('event', 'message')}\n"
                    f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                )
        finally:
            broker.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
