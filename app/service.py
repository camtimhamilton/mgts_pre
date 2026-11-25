"""Снапшоты и выборки для API.

Здесь живёт всё, что требует истории: дельта к прошлому месяцу, сценарии, теплокарта, KPI.
Числа не пересчитываются на лету для каждого запроса — читаем готовый снапшот.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import analysis, scoring
from .db import write_lock
from .normalize import FACTOR_LABELS, MONTHS

MONTH_INDEX = {month: index + 1 for index, month in enumerate(MONTHS)}

CAUSE_LABELS = {
    "clarity": "неясные задачи",
    "future": "нет ясности по офферу",
    "mentor": "мало обратной связи",
    "workload": "высокая нагрузка",
    "motivation": "низкая мотивация",
    "inclusion": "слабая включённость",
}


def _idx(month: str) -> int:
    return MONTH_INDEX.get(month, 0)


def _trend(delta: int) -> dict[str, str]:
    if delta <= -5:
        return {"key": "falling", "label": "снижается"}
    if delta < 5:
        return {"key": "stable", "label": "стабильно"}
    if delta < 10:
        return {"key": "growing", "label": "растёт"}
    return {"key": "fast_growth", "label": "быстро растёт"}


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def available_months(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT month FROM snapshots").fetchall()
    return sorted((row["month"] for row in rows), key=_idx)


def latest_month(conn: sqlite3.Connection) -> str | None:
    months = available_months(conn)
    return months[-1] if months else None


def has_snapshots(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None


def _pair_meta(conn: sqlite3.Connection, pair_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM pairs WHERE id = ?", (pair_id,)).fetchone()


def recompute_pair_month(
    conn: sqlite3.Connection, pair_id: int, month: str
) -> dict[str, Any] | None:
    """Считает снапшот пары за месяц. Возвращает data_json или None, если анкет нет."""
    core = analysis.build_core(conn, pair_id, month)
    if core is None:
        return None

    # Всё, что может тормозить (модель), уже посчитано. Под блокировкой — только БД.
    with write_lock:
        return _persist_snapshot(conn, pair_id, month, core)


def _persist_snapshot(
    conn: sqlite3.Connection, pair_id: int, month: str, core: dict[str, Any]
) -> dict[str, Any] | None:
    conn.execute("BEGIN IMMEDIATE")
    pair = _pair_meta(conn, pair_id)
    if pair is None:
        conn.rollback()
        return None

    previous = conn.execute(
        "SELECT month, risk FROM snapshots WHERE pair_id = ?", (pair_id,)
    ).fetchall()
    history = sorted(
        ((row["month"], int(row["risk"])) for row in previous if _idx(row["month"]) < _idx(month)),
        key=lambda item: _idx(item[0]),
    )
    delta = core["risk"] - history[-1][1] if history else 0
    history = history + [(month, core["risk"])]

    scenarios = scoring.scenario(history, float(core["steps_reduction"]), horizon=1)
    # Если человек уже в красной зоне, «красная зона к M6» звучит странно:
    # событие должно говорить о том, что будет, а не о том, что уже случилось.
    if history[-1][1] >= scoring.BAND_HIGH:
        scenarios["event"] = "остаётся в красной зоне"

    observed = conn.execute(
        """
        SELECT month, COUNT(DISTINCT role) AS roles
          FROM submissions
         WHERE pair_id = ? AND month <= ?
         GROUP BY month
        """,
        (pair_id, month),
    ).fetchall()
    months_seen = len(observed)
    with_both = sum(1 for row in observed if row["roles"] >= 2)

    received = conn.execute(
        "SELECT MAX(received_at) FROM submissions WHERE pair_id = ? AND month = ?",
        (pair_id, month),
    ).fetchone()[0]
    received_at = _parse_dt(received)
    signal_seconds = None
    if received_at is not None:
        seconds = max(0.0, (datetime.now(timezone.utc) - received_at).total_seconds())
        # Для исторических данных «время сигнала» смысла не имеет — это метрика
        # живого приёма. Больше часа — значит, данные пришли пачкой, показывать нечего.
        signal_seconds = seconds if seconds <= 3600 else None

    summary = core["summary"]
    if not summary:
        top = max(core["factors"], key=lambda item: item["points"])
        summary = (
            f"Главный вклад в риск — {top['label'].lower()}: "
            f"{top['points']:.0f} из {int(scoring.WEIGHTS.get(top['key'], 0))} возможных."
        )

    data: dict[str, Any] = {
        "id": pair_id,
        "month": month,
        "intern_name": pair["intern_name"],
        "mentor_name": pair["mentor_name"],
        "direction": pair["direction"],
        "photo_url": pair["photo_url"],
        "program": {"month_current": _idx(month), "months_total": len(MONTHS)},
        "risk": core["risk"],
        "delta": delta,
        "band": core["band"],
        "trend": _trend(delta),
        "score_origin": core["factors"][0]["origin"] if core["factors"] else "answer",
        "summary": summary,
        "gap_comment": core["gap_comment"],
        "factors": core["factors"],
        "reasons": core.get("reasons", []),
        "gap_penalty": core["gap_penalty"],
        "text_penalty": core["text_penalty"],
        "signals": core["signals"],
        "steps": core["steps"],
        "history": [{"month": item[0], "risk": item[1]} for item in history],
        "scenarios": scenarios,
        "observation": {"months": months_seen, "with_both_forms": with_both},
        "originals": core["originals"],
        "signal_seconds": round(signal_seconds, 1) if signal_seconds is not None else None,
        "degraded": core["degraded"],
    }

    payload = json.dumps(data, ensure_ascii=False)
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = conn.execute(
        "SELECT id FROM snapshots WHERE pair_id = ? AND month = ?", (pair_id, month)
    ).fetchone()

    if existing:
        snapshot_id = int(existing["id"])
        conn.execute(
            """
            UPDATE snapshots
               SET risk = ?, base = ?, gap_penalty = ?, text_penalty = ?, delta = ?,
                   band = ?, data_json = ?, created_at = ?
             WHERE id = ?
            """,
            (
                core["risk"],
                core["base"],
                core["gap_penalty"],
                core["text_penalty"],
                delta,
                core["band"],
                payload,
                created,
                snapshot_id,
            ),
        )
        conn.execute("DELETE FROM factors WHERE snapshot_id = ?", (snapshot_id,))
        conn.execute("DELETE FROM steps WHERE snapshot_id = ?", (snapshot_id,))
    else:
        cursor = conn.execute(
            """
            INSERT INTO snapshots
                (pair_id, month, risk, base, gap_penalty, text_penalty, delta, band,
                 data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pair_id,
                month,
                core["risk"],
                core["base"],
                core["gap_penalty"],
                core["text_penalty"],
                delta,
                core["band"],
                payload,
                created,
            ),
        )
        snapshot_id = int(cursor.lastrowid or 0)

    for factor in core["factors"]:
        conn.execute(
            """
            INSERT INTO factors
                (snapshot_id, key, points, score_intern, score_mentor, score_origin, quote)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                factor["key"],
                factor["points"],
                factor["score_intern"],
                factor["score_mentor"],
                factor["origin"],
                factor["quote"],
            ),
        )

    for step in core["steps"]:
        conn.execute(
            """
            INSERT INTO steps (snapshot_id, key, text, reduction, owner, due)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                step["key"],
                step["text"],
                step["reduction"],
                step["owner"],
                step["due"],
            ),
        )

    conn.commit()
    return data


def recompute_pair(conn: sqlite3.Connection, pair_id: int) -> list[dict[str, Any]]:
    """Пересчитывает все месяцы пары по возрастанию, чтобы дельта была честной."""
    months = [
        row["month"]
        for row in conn.execute(
            "SELECT DISTINCT month FROM submissions WHERE pair_id = ?", (pair_id,)
        ).fetchall()
    ]
    result = []
    for month in sorted(months, key=_idx):
        data = recompute_pair_month(conn, pair_id, month)
        if data:
            result.append(data)
    return result


def recompute_all(conn: sqlite3.Connection) -> dict[str, Any]:
    pairs = [row["id"] for row in conn.execute("SELECT id FROM pairs ORDER BY id")]
    snapshots = 0
    for pair_id in pairs:
        snapshots += len(recompute_pair(conn, int(pair_id)))
    return {"pairs": len(pairs), "snapshots": snapshots}


# ---------------------------------------------------------------- выборки


def _sparkline(conn: sqlite3.Connection, pair_id: int, month: str) -> list[int]:
    rows = conn.execute(
        "SELECT month, risk FROM snapshots WHERE pair_id = ?", (pair_id,)
    ).fetchall()
    return [
        int(row["risk"])
        for row in sorted(rows, key=lambda item: _idx(item["month"]))
        if _idx(row["month"]) <= _idx(month)
    ]


def list_pairs(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT s.*, p.intern_name, p.mentor_name, p.direction, p.photo_url
          FROM snapshots s
          JOIN pairs p ON p.id = s.pair_id
         WHERE s.month = ?
        """,
        (month,),
    ).fetchall()

    items = []
    for row in rows:
        data = json.loads(row["data_json"])
        gap = max((factor.get("gap") or 0) for factor in data.get("factors", [])) if data.get("factors") else 0
        missing = None
        roles = {
            item["role"]
            for item in conn.execute(
                "SELECT role FROM submissions WHERE pair_id = ? AND month = ?",
                (row["pair_id"], month),
            ).fetchall()
        }
        if "intern" not in roles:
            missing = "intern"
        elif "mentor" not in roles:
            missing = "mentor"

        items.append(
            {
                "id": row["pair_id"],
                "intern_name": row["intern_name"],
                "mentor_name": row["mentor_name"],
                "direction": row["direction"],
                "risk": int(row["risk"]),
                "delta": int(row["delta"] or 0),
                "band": row["band"],
                "urgency": round(scoring.urgency(int(row["risk"]), int(row["delta"] or 0)), 1),
                "gap": gap,
                "sparkline": _sparkline(conn, row["pair_id"], month),
                "missing": missing,
                "photo_url": row["photo_url"],
            }
        )

    items.sort(key=lambda item: item["urgency"], reverse=True)
    return {"month": month, "items": items}


def program(conn: sqlite3.Connection, month: str) -> dict[str, Any]:
    pairs = list_pairs(conn, month)["items"]
    total = len(pairs)
    at_risk = sum(
        1 for item in pairs if item["risk"] >= scoring.BAND_HIGH or item["delta"] >= 10
    )
    avg_risk = round(sum(item["risk"] for item in pairs) / total, 1) if total else 0
    gap_pairs = sum(1 for item in pairs if (item["gap"] or 0) >= 4)

    submitted = conn.execute(
        """
        SELECT COUNT(*) FROM submissions WHERE month = ?
        """,
        (month,),
    ).fetchone()[0]

    factors = conn.execute(
        """
        SELECT f.key, COUNT(*) AS cnt
          FROM factors f
          JOIN snapshots s ON s.id = f.snapshot_id
         WHERE s.month = ? AND f.points >= 8
         GROUP BY f.key
         ORDER BY cnt DESC, f.key
         LIMIT 3
        """,
        (month,),
    ).fetchall()

    bands = {"low": 0, "watch": 0, "critical": 0}
    for item in pairs:
        bands[item["band"]] = bands.get(item["band"], 0) + 1

    signal_values = [
        json.loads(row["data_json"]).get("signal_seconds")
        for row in conn.execute(
            "SELECT data_json FROM snapshots WHERE month = ?", (month,)
        ).fetchall()
    ]
    fresh = [value for value in signal_values if value is not None and value <= 3600]

    heatmap = []
    for item in pairs:
        cells = [
            {"month": row["month"], "risk": int(row["risk"])}
            for row in conn.execute(
                "SELECT month, risk FROM snapshots WHERE pair_id = ?", (item["id"],)
            ).fetchall()
        ]
        cells.sort(key=lambda cell: _idx(cell["month"]))
        heatmap.append({"pair_id": item["id"], "name": item["intern_name"], "cells": cells})

    return {
        "month": month,
        "months": available_months(conn),
        "kpi": {
            "at_risk": at_risk,
            "total_pairs": total,
            "avg_risk": avg_risk,
            "gap_pairs": gap_pairs,
            "submitted": {"done": int(submitted), "expected": total * 2},
            "last_signal_sec": round(min(fresh), 1) if fresh else None,
        },
        "bands": bands,
        "top_causes": [
            {
                "key": row["key"],
                "label": CAUSE_LABELS.get(row["key"], FACTOR_LABELS.get(row["key"], row["key"])),
                "count": int(row["cnt"]),
            }
            for row in factors
        ],
        "heatmap": heatmap,
    }


def pair_card(conn: sqlite3.Connection, pair_id: int, month: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT data_json FROM snapshots WHERE pair_id = ? AND month = ?",
        (pair_id, month),
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["data_json"])


def submissions_for(conn: sqlite3.Connection, pair_id: int) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM submissions WHERE pair_id = ? ORDER BY month", (pair_id,)
    ).fetchall()
    items = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        items.append(
            {
                "id": row["id"],
                "role": row["role"],
                "month": row["month"],
                "source": row["source"],
                "received_at": row["received_at"],
                "answers": {key: value for key, value in payload.get("scores", {}).items()},
                "open": {
                    "q_better": payload.get("q_better", ""),
                    "q_barrier": payload.get("q_barrier", ""),
                },
            }
        )
    return {"pair_id": pair_id, "items": items}
