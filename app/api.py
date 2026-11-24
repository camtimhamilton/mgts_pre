"""HTTP-слой.

Фаза 0: health, приёмник, лог приёмника и контрактные заглушки на фикстурах.
Заглушки помечены заголовком X-Data-Source: fixture — чтобы никто, включая нас на
финале, не перепутал демонстрационный ответ с настоящим расчётом.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .config import get_settings
from .db import connect, ping

router = APIRouter()
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---------------------------------------------------------------- служебное


@router.get("/api/health")
def health() -> dict[str, Any]:
    s = get_settings()
    return {
        "status": "ok",
        "app": "pulse",
        "version": "0.2",
        "phase": 0,
        "db": ping(),
        "view": s.app_view,
        "team": s.team_name,
        "demo_mode": s.demo_mode,
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
    """
    conn = connect()
    try:
        if delivery_id:
            row = conn.execute(
                "SELECT id FROM ingest_log WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if row:
                return int(row["id"]), True

        cur = conn.execute(
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
        return int(cur.lastrowid or 0), False
    finally:
        conn.close()


@router.post("/api/ingest")
async def ingest(request: Request) -> JSONResponse:
    """Приём ответа формы.

    Всегда отвечает 200: по документации Яндекса 404 и 5XX заставляют форму повторять
    запрос до семи раз, а ошибку всё равно надо разбирать вручную. Разбор, LLM и
    пересчёт идут после ответа — у формы таймаут 5 секунд.
    """
    s = get_settings()
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    raw = body.decode("utf-8", errors="replace")

    key_ok = not s.ingest_key or headers.get("x-ingest-key") == s.ingest_key
    status = "accepted" if key_ok else "rejected"
    error = None if key_ok else "bad ingest key"

    parsed: Any = None
    is_rpc = False
    try:
        parsed = json.loads(raw) if raw.strip() else None
        is_rpc = isinstance(parsed, dict) and parsed.get("jsonrpc") == "2.0"
    except json.JSONDecodeError as exc:
        status = "unparsed"
        error = f"json: {exc.msg}"

    row_id, duplicate = _log_ingest(
        source="webhook",
        delivery_id=headers.get("x-delivery-id"),
        status=status,
        raw=raw,
        headers=headers,
        error=error,
    )

    result = {
        "ok": True,
        "id": row_id,
        "duplicate": duplicate,
        "status": "duplicate" if duplicate else status,
    }
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
# До появления расчёта отдаём фикстуры, чтобы фронт мог строиться параллельно.


def _fixture(name: str) -> Any:
    path = FIXTURES / name
    if not path.exists():
        return {"error": "fixture missing", "name": name}
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_response(name: str) -> JSONResponse:
    return JSONResponse(_fixture(name), headers={"X-Data-Source": "fixture"})


@router.get("/api/program")
def program(month: str | None = None) -> JSONResponse:
    return _fixture_response("program.json")


@router.get("/api/pairs")
def pairs() -> JSONResponse:
    return _fixture_response("pairs.json")


@router.get("/api/pairs/{pair_id}")
def pair(pair_id: int, month: str | None = None) -> JSONResponse:
    return _fixture_response("pair.json")


@router.get("/api/pairs/{pair_id}/submissions")
def submissions(pair_id: int) -> JSONResponse:
    return _fixture_response("submissions.json")


# ---------------------------------------------------------------- события


@router.get("/api/events")
async def events(request: Request) -> StreamingResponse:
    """SSE. Фаза 0 — только heartbeat, чтобы фронт мог подключиться заранее."""

    async def stream():
        yield "event: hello\ndata: {}\n\n"
        while True:
            if await request.is_disconnected():
                break
            yield "event: ping\ndata: {}\n\n"
            await asyncio.sleep(15)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
