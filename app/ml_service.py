"""Stored ML results for the dashboard. No weights, score formulas or forecasts."""
import hashlib
import json
import threading
from collections import Counter

from .config import get_settings
from .db import write_lock
from .ml_client import MLClient, MLUnavailable
from .ml_mapping import survey_payload
from .normalize import MONTHS

BANDS = {"routine": "low", "elevated": "watch", "high": "critical"}
_locks, _lock_guard = {}, threading.Lock()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _request_hash(payload):
    return hashlib.sha256(_json([payload, get_settings().ml_api_url, "dashboard-adapter-1"]).encode()).hexdigest()


def available_months(conn):
    return [r[0] for r in conn.execute("SELECT DISTINCT month FROM submissions ORDER BY month")]


def latest_month(conn):
    return (available_months(conn) or [None])[-1]


def has_snapshots(conn):
    return conn.execute("SELECT 1 FROM ml_results LIMIT 1").fetchone() is not None


def _input(conn, pair_id, month):
    rows = conn.execute("SELECT payload_json FROM submissions WHERE pair_id=? AND month=? ORDER BY role", (pair_id, month))
    return survey_payload(pair_id, month, [json.loads(r[0]) for r in rows])


def _stored(conn, pair_id, month):
    return conn.execute("SELECT * FROM ml_results WHERE pair_id=? AND month=?", (pair_id, month)).fetchone()


def _analysis(conn, row):
    if not row or not row["analysis_json"] or row["state"] == "pending":
        return None
    if row["request_hash"] != _request_hash(_input(conn, row["pair_id"], row["month"])):
        return None
    return json.loads(row["analysis_json"])


def recompute_pair_month(conn, pair_id, month):
    # Key lock coalesces manual/background calls; global DB lock never covers HTTP.
    with _lock_guard:
        lock = _locks.setdefault((get_settings().db_path, pair_id, month), threading.Lock())
    with lock:
        return _recompute(conn, pair_id, month)


def _recompute(conn, pair_id, month):
    payload = _input(conn, pair_id, month)
    if not payload["responses"]:
        return None
    request_hash = _request_hash(payload)
    old = _stored(conn, pair_id, month)
    if old and old["request_hash"] == request_hash and old["state"] not in {"error", "pending"}:
        return pair_card(conn, pair_id, month)
    with write_lock:
        conn.execute("""INSERT INTO ml_results(pair_id,month,request_hash,state) VALUES(?,?,?,'pending')
            ON CONFLICT(pair_id,month) DO UPDATE SET request_hash=excluded.request_hash,state='pending',
            analysis_json=NULL,scenario_json=NULL,error_code=NULL,updated_at=datetime('now')""", (pair_id, month, request_hash))
        conn.commit()
    result = scenario = None
    error = None
    try:
        client = MLClient()
        result = client.analyze(payload)
        if result["risk"]["score"] is not None:
            try:
                scenario = client.scenario(result["analysis_id"], [a["id"] for a in result["recommendations"]])
            except MLUnavailable:
                error = "ml_scenario_unavailable"
    except MLUnavailable as failure:
        error = failure.code
    with write_lock:
        if _request_hash(_input(conn, pair_id, month)) != request_hash:
            return pair_card(conn, pair_id, month)  # updated input remains pending
        state = "error" if result is None else result["status"]
        state = "ready" if state == "completed" else state
        conn.execute("""UPDATE ml_results SET analysis_json=?,scenario_json=?,state=?,error_code=?,updated_at=datetime('now')
            WHERE pair_id=? AND month=? AND request_hash=?""", (_json(result) if result else None,
            _json(scenario) if scenario else None, state, error, pair_id, month, request_hash))
        if result:
            conn.execute("INSERT OR IGNORE INTO ml_runs(analysis_id,pair_id,month,request_hash,result_json) VALUES(?,?,?,?,?)",
                         (result["analysis_id"], pair_id, month, request_hash, _json(result)))
        conn.commit()
    return pair_card(conn, pair_id, month)


def recompute_pair(conn, pair_id):
    months = [r[0] for r in conn.execute("SELECT DISTINCT month FROM submissions WHERE pair_id=? ORDER BY month", (pair_id,))]
    return [result for month in months if (result := recompute_pair_month(conn, pair_id, month)) is not None]


def recompute_all(conn):
    pairs = [r[0] for r in conn.execute("SELECT id FROM pairs ORDER BY id")]
    return {"pairs": len(pairs), "snapshots": sum(len(recompute_pair(conn, p)) for p in pairs)}


def _history(conn, pair_id, target):
    result = []
    for month in MONTHS[:int(target[1:])]:
        analysis = _analysis(conn, _stored(conn, pair_id, month))
        result.append({"month": month, "risk": analysis["risk"]["score"] if analysis else None,
                       "band": BANDS.get(analysis["risk"]["score_band"], "unknown") if analysis else "unknown"})
    return result


def pair_card(conn, pair_id, month):
    pair = conn.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
    if pair is None:
        return None
    row = _stored(conn, pair_id, month)
    roles = {r[0] for r in conn.execute("SELECT role FROM submissions WHERE pair_id=? AND month=?", (pair_id, month))}
    analysis = _analysis(conn, row)
    risk = analysis["risk"] if analysis else {}
    factors = []
    for f in analysis["factors"] if analysis else []:
        numeric = {s["role"]: s for s in f["evidence"] if s["source_kind"] == "rating" and s["validation_status"] == "accepted"}
        gap = next((d for d in analysis["discrepancies"] if d["factor_id"] == f["id"]), None)
        text = [s for s in f["evidence"] if s["source_kind"] == "text" and s["validation_status"] == "accepted"]
        factors.append({"key": f["id"], "label": f["name"], "points": f["contribution"], "weight": f["weight"],
            "state": f["state"], "evidence": f["evidence"], "quote": text[0]["quote"] if text else "",
            "origin": "ml", "score_intern": numeric.get("intern", {}).get("value"),
            "score_mentor": numeric.get("mentor", {}).get("value"),
            "scale_min": next(iter(numeric.values()), {}).get("scale_min"),
            "scale_max": next(iter(numeric.values()), {}).get("scale_max"),
            "gap": gap["normalized_gap"] if gap else None,
            "comparison_threshold_exceeded": gap["threshold_exceeded"] if gap else False})
    steps = [{"key": a["id"], "text": a["name"], "reduction": a["potential_reduction"],
              "owner": ", ".join(a["responsible"]), "due": None, "target_factors": a["target_factors"],
              "completion_criteria": a["completion_criteria"], "resolution_criteria": a["resolution_criteria"]}
             for a in analysis["recommendations"]] if analysis else []
    state = row["state"] if row else "pending" if roles else "missing"
    if not analysis and state not in {"pending", "missing", "error"}:
        state = "pending"
    score = risk.get("score")
    scenario = json.loads(row["scenario_json"]) if analysis and row["scenario_json"] else None
    observed = conn.execute("SELECT month,COUNT(DISTINCT role) AS roles FROM submissions WHERE pair_id=? AND month<=? GROUP BY month",
                            (pair_id, month)).fetchall()
    return {"id": pair_id, "month": month, "intern_name": pair["intern_name"], "mentor_name": pair["mentor_name"],
        "direction": pair["direction"], "photo_url": pair["photo_url"], "analysis_mode": "ml", "analysis_state": state,
        "error_code": row["error_code"] if row else None, "program": {"month_current": int(month[1:]), "months_total": 6},
        "risk": score, "base": score, "gap_penalty": 0, "text_penalty": 0,
        "band": BANDS.get(risk.get("score_band"), "unknown"), "priority": risk.get("priority", "insufficient_data"),
        "data_coverage": risk.get("data_coverage"), "missing_required_roles": risk.get("missing_required_roles", [r for r in ("intern", "mentor") if r not in roles]),
        "critical_flags": risk.get("critical_flags", []), "warnings": analysis["warnings"] if analysis else [],
        "delta": None, "trend": {"key": "not_available", "label": "сравнение не реализовано"}, "score_origin": "ml",
        "summary": "Наблюдаемый индекс из ML API; текстовые сигналы предварительны." if analysis else "Анализ пока недоступен.",
        "gap_comment": "Расхождение оценок — отдельный сигнал для обсуждения, без надбавки к индексу.",
        "factors": factors, "reasons": [dict(f, text=f["state"]) for f in factors if f["points"] > 0],
        "signals": [dict(s, label=f"{next((f['name'] for f in analysis['factors'] if f['id'] == s['factor_id']), s['factor_id'])} · {s['validation_status']}")
                    for s in analysis["signals"]] if analysis else [], "steps": steps, "scenario": scenario,
        "steps_reduction": scenario["potential_reduction"] if scenario else None,
        "history": _history(conn, pair_id, month), "temporal_status": "not_implemented",
        "scenarios": {"base": [], "with_actions": [], "status": "not_implemented"},
        "originals": {r: {} for r in roles}, "observation": {"months": len(observed), "with_both_forms": sum(r["roles"] == 2 for r in observed)},
        "signal_seconds": None, "degraded": state in {"error", "partial"},
        "has_gap": any(f["comparison_threshold_exceeded"] for f in factors),
        "ml_analysis_id": analysis["analysis_id"] if analysis else None,
        "ml_metadata": analysis["metadata"] if analysis else None,
        "capabilities": analysis["capabilities"] if analysis else {}}


def list_pairs(conn, month):
    items = []
    for row in conn.execute("SELECT id FROM pairs ORDER BY id").fetchall():
        card = pair_card(conn, row[0], month)
        rank = {"pending_review": 4, "high": 3, "elevated": 2, "insufficient_data": 1, "routine": 0}.get(card["priority"], 1)
        items.append({k: card[k] for k in ("id", "intern_name", "mentor_name", "direction", "risk", "delta", "band",
                      "priority", "data_coverage", "analysis_state", "has_gap", "photo_url")})
        items[-1].update(urgency=rank, gap=max((f["gap"] or 0 for f in card["factors"]), default=0),
                         sparkline=[p["risk"] for p in card["history"]],
                         missing=next(iter(card["missing_required_roles"]), None))
    return {"month": month, "items": sorted(items, key=lambda item: item["urgency"], reverse=True)}


def program(conn, month):
    pairs = list_pairs(conn, month)["items"]
    known = [p["risk"] for p in pairs if p["risk"] is not None]
    counts = Counter(p["band"] for p in pairs)
    causes, labels = Counter(), {}
    for p in pairs:
        for f in pair_card(conn, p["id"], month)["factors"]:
            if f["points"] > 0:
                causes[f["key"]] += 1
                labels[f["key"]] = f["label"]
    return {"month": month, "months": available_months(conn), "analysis_mode": "ml",
        "kpi": {"at_risk": sum(p["priority"] in {"high", "pending_review"} for p in pairs),
            "total_pairs": len(pairs), "avg_risk": round(sum(known) / len(known), 2) if known else None,
            "gap_pairs": sum(p["has_gap"] for p in pairs),
            "unknown": sum(p["risk"] is None or p["priority"] == "insufficient_data" for p in pairs),
            "submitted": {"done": conn.execute("SELECT COUNT(*) FROM submissions WHERE month=?", (month,)).fetchone()[0],
                          "expected": len(pairs) * 2}, "last_signal_sec": None},
        "bands": {key: counts[key] for key in ("low", "watch", "critical", "unknown")},
        "top_causes": [{"key": key, "label": labels[key], "count": n} for key, n in causes.most_common(3)],
        "heatmap": [{"pair_id": p["id"], "name": p["intern_name"], "cells": _history(conn, p["id"], "M6")} for p in pairs]}


def submissions_for(conn, pair_id):
    items = []
    for row in conn.execute("SELECT * FROM submissions WHERE pair_id=? ORDER BY month", (pair_id,)):
        payload = json.loads(row["payload_json"])
        items.append({"id": row["id"], "role": row["role"], "month": row["month"], "source": row["source"],
            "received_at": row["received_at"], "answers": payload.get("scores", {}),
            "open": {key: payload.get(key, "") for key in ("q_better", "q_barrier")}})
    return {"pair_id": pair_id, "items": items}


def calculate_scenario(conn, pair_id, month, action_ids):
    card = pair_card(conn, pair_id, month)
    if not card or not card["ml_analysis_id"] or card["risk"] is None:
        raise MLUnavailable("ml_scenario_unavailable")
    if not set(action_ids) <= {s["key"] for s in card["steps"]}:
        raise MLUnavailable("ml_action_not_available")
    return MLClient().scenario(card["ml_analysis_id"], action_ids)
