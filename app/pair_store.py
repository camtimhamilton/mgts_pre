"""Append-only survey revisions, idempotent delivery and persisted scenarios."""
import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from .db import connect as open_connection, write_lock
from .pair_models import AnalyticsScope, SurveyInput


@contextmanager
def connect():
    connection = open_connection()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_store() -> None:
    with write_lock, connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS pair_v2_registry (
          pair_id TEXT PRIMARY KEY, intern_id TEXT UNIQUE NOT NULL,
          mentor_id TEXT NOT NULL, department TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pair_v2_surveys (
          source_id TEXT PRIMARY KEY, pair_id TEXT NOT NULL REFERENCES pair_v2_registry,
          month INTEGER NOT NULL, role TEXT NOT NULL, version INTEGER NOT NULL,
          content_hash TEXT NOT NULL, record_json TEXT NOT NULL, raw_json TEXT NOT NULL,
          received_at TEXT NOT NULL, UNIQUE(pair_id, month, role, version));
        CREATE TABLE IF NOT EXISTS pair_v2_deliveries (
          event_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
          source_id TEXT NOT NULL REFERENCES pair_v2_surveys);
        CREATE TABLE IF NOT EXISTS pair_v2_cache (
          cache_key TEXT PRIMARY KEY, result_json TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pair_v2_audit (
          id TEXT PRIMARY KEY, cache_key TEXT NOT NULL, result_json TEXT NOT NULL,
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pair_v2_analysis_times (
          source_id TEXT NOT NULL, methodology TEXT NOT NULL, analyzed_at TEXT NOT NULL,
          PRIMARY KEY(source_id,methodology));
        CREATE TABLE IF NOT EXISTS pair_v2_scenarios (
          scenario_id TEXT PRIMARY KEY, name TEXT NOT NULL, state_json TEXT NOT NULL,
          data_version TEXT NOT NULL, result_json TEXT NOT NULL,
          stale INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pair_v2_scenario_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_id TEXT NOT NULL,
          kind TEXT NOT NULL, state_json TEXT NOT NULL, result_json TEXT NOT NULL,
          created_at TEXT NOT NULL);
        """)
        reconcile_scenario_staleness(db)


def scenario_scopes(state: dict) -> list[dict]:
    """A saved view and every selected action contribute data dependencies."""
    scopes = [state["scope"], *(selection["scope"] for selection in state.get("selected", []))]
    return [AnalyticsScope.model_validate(scope).model_dump(mode="json") for scope in scopes]


def scope_contains(scope: dict, pair_id: str, month: int | None = None) -> bool:
    return scope["type"] == "GLOBAL" or (scope["pair_id"] == pair_id and (
        month is None or scope["start"] <= month <= scope["end"]))


def invalidate_scenarios(db, pair_id: str, month: int | None = None) -> None:
    for row in db.execute("SELECT scenario_id,state_json FROM pair_v2_scenarios WHERE stale=0").fetchall():
        try:
            affected = any(scope_contains(scope, pair_id, month) for scope in scenario_scopes(json.loads(row["state_json"])))
        except (ValueError, KeyError, TypeError):
            affected = True
        if affected:
            db.execute("UPDATE pair_v2_scenarios SET stale=1 WHERE scenario_id=?", (row["scenario_id"],))


def scenario_dependencies_changed(state: dict, snapshot: dict, records: list[dict], pairs: list[dict]) -> bool:
    """Compare immutable dependency sources, including previously missing surveys."""
    try:
        scopes = scenario_scopes(state)
        old_pairs = {row["pair_id"]: row for row in snapshot["matrix"]}
        new_pairs = {pair["pair_id"]: pair for pair in pairs}
        for scope in scopes:
            if scope["type"] != "GLOBAL" and (scope["pair_id"] not in old_pairs or scope["pair_id"] not in new_pairs):
                return True
        for pair_id in old_pairs.keys() | new_pairs.keys():
            if not any(scope_contains(scope, pair_id) for scope in scopes):
                continue
            if pair_id not in old_pairs or pair_id not in new_pairs:
                return True
            if any(old_pairs[pair_id][key] != new_pairs[pair_id][key] for key in ("mentor_id", "intern_id", "department")):
                return True
        old_sources = {(row["pair_id"], cell["month"], role): cell[side].get("source_id")
            for row in snapshot["matrix"] for cell in row["months"] for side, role in (("x", "mentor"), ("y", "intern"))}
        new_sources = {(record["pair_id"], record["month"], record["role"]): record["source_id"] for record in records}
        for identity in old_sources.keys() | new_sources.keys():
            pair_id, month, _ = identity
            if any(scope_contains(scope, pair_id, month) for scope in scopes) and old_sources.get(identity) != new_sources.get(identity):
                return True
        return False
    except (ValueError, KeyError, TypeError):
        return True


def reconcile_scenario_staleness(db) -> None:
    """Upgrade earlier blanket invalidations using each preserved base snapshot."""
    records, pairs = current_records(db), registered_pairs(db)
    for row in db.execute("SELECT scenario_id,state_json,result_json,stale FROM pair_v2_scenarios").fetchall():
        try:
            changed = scenario_dependencies_changed(json.loads(row["state_json"]), json.loads(row["result_json"]), records, pairs)
        except (ValueError, TypeError):
            changed = True
        if bool(row["stale"]) != changed:
            db.execute("UPDATE pair_v2_scenarios SET stale=? WHERE scenario_id=?", (int(changed), row["scenario_id"]))


def register_pair(db, intern_id: str, mentor_id: str, department: str) -> str:
    row = db.execute("SELECT * FROM pair_v2_registry WHERE intern_id=?", (intern_id,)).fetchone()
    if row:
        if row["mentor_id"] != mentor_id or row["department"] != department:
            raise ValueError("pair_registry_conflict")
        return row["pair_id"]
    pair_id = "pair-" + digest([mentor_id, intern_id])[:16]
    db.execute("INSERT INTO pair_v2_registry VALUES (?,?,?,?)", (pair_id, intern_id, mentor_id, department))
    db.execute("DELETE FROM pair_v2_cache")
    invalidate_scenarios(db, pair_id)
    return pair_id


def accept(db, survey: SurveyInput, event_id: str, raw: dict) -> dict:
    body = survey.model_dump(mode="json")
    content_hash = digest(body)
    previous = db.execute("SELECT * FROM pair_v2_deliveries WHERE event_id=?", (event_id,)).fetchone()
    if previous:
        if previous["content_hash"] != content_hash:
            raise ValueError("delivery_id_content_conflict")
        return {"status": "duplicate", "source_id": previous["source_id"]}
    registry = db.execute("SELECT * FROM pair_v2_registry WHERE intern_id=?", (survey.intern_id,)).fetchone()
    mentor_id = survey.mentor_id or (registry["mentor_id"] if registry else None)
    if not mentor_id:
        raise ValueError("intern_pair_not_registered")
    pair_id = register_pair(db, survey.intern_id, mentor_id, survey.department)
    latest = db.execute("SELECT * FROM pair_v2_surveys WHERE pair_id=? AND month=? AND role=? ORDER BY version DESC LIMIT 1",
                        (pair_id, survey.month, survey.role)).fetchone()
    # A new authenticated event may be an explicit new edition with equal answers.
    # Delivery identity, rather than answer equality, controls replay suppression.
    source_id = "survey-" + uuid.uuid4().hex
    version = latest["version"] + 1 if latest else 1
    record = {**body, "mentor_id": mentor_id, "pair_id": pair_id,
              "source_id": source_id, "version": version}
    db.execute("INSERT INTO pair_v2_surveys VALUES (?,?,?,?,?,?,?,?,?)",
               (source_id, pair_id, survey.month, survey.role, version,
                content_hash, encode(record), encode(raw), now()))
    db.execute("DELETE FROM pair_v2_cache")
    invalidate_scenarios(db, pair_id, survey.month)
    status = "accepted"
    db.execute("INSERT INTO pair_v2_deliveries VALUES (?,?,?)", (event_id, content_hash, source_id))
    return {"status": status, "source_id": source_id, "pair_id": pair_id}


def ingest(survey: SurveyInput, event_id: str, raw: dict | None = None) -> dict:
    with write_lock, connect() as db:
        return accept(db, survey, event_id, raw or survey.model_dump(mode="json"))


def current_records(db=None) -> list[dict]:
    if db is None:
        with connect() as connection:
            return current_records(connection)
    rows = db.execute("""SELECT s.record_json,s.received_at FROM pair_v2_surveys s WHERE version=(
      SELECT MAX(t.version) FROM pair_v2_surveys t
      WHERE t.pair_id=s.pair_id AND t.month=s.month AND t.role=s.role)
      ORDER BY s.pair_id,s.month,s.role""").fetchall()
    return [{**json.loads(row[0]), "received_at": row[1]} for row in rows]


def registered_pairs(db=None) -> list[dict]:
    if db is None:
        with connect() as connection:
            return registered_pairs(connection)
    return [dict(row) for row in db.execute("SELECT * FROM pair_v2_registry ORDER BY pair_id")]


def data_version(records: list[dict], pairs: list[dict] | None = None) -> str:
    return digest({"sources": [(r["source_id"], r["version"]) for r in records],
                   "pairs": registered_pairs() if pairs is None else pairs})


def source(source_id: str) -> dict | None:
    with connect() as db:
        row = db.execute("SELECT * FROM pair_v2_surveys WHERE source_id=?", (source_id,)).fetchone()
        if not row:
            return None
        versions = db.execute("SELECT source_id,version,received_at FROM pair_v2_surveys WHERE pair_id=? AND month=? AND role=? ORDER BY version",
                              (row["pair_id"], row["month"], row["role"])).fetchall()
        return {"record": json.loads(row["record_json"]), "raw": json.loads(row["raw_json"]),
                "received_at": row["received_at"], "versions": [dict(v) for v in versions]}
