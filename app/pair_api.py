"""Local dashboard persistence and transport for ML-owned v2 analytics."""
import hmac
import json
import uuid
from threading import Lock
from time import perf_counter

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from pydantic import ValidationError

from .broker import broker
from .config import get_settings
from .db import write_lock
from .ml_client import MLClient, MLUnavailable
from .pair_models import AnalyticsRequest, AnalyticsResponse, DashboardAnalysis, PairRegistration, ScenarioSave, SurveyInput
from .pair_wire import validate_analysis
from .pair_store import accept, connect, current_records, data_version, digest, encode, ingest, now, register_pair, registered_pairs, scenario_dependencies_changed, source

router = APIRouter(prefix="/api/v2", tags=["pair analytics v2"])
refresh_lock = Lock()


def require_key(value: str | None) -> None:
    expected = get_settings().ingest_key
    if expected and not hmac.compare_digest((value or "").encode(), expected.encode()):
        raise HTTPException(401, "invalid_ingest_key")


def analytics(payload: AnalyticsRequest, *, generation: str | None = None, preview: bool = False) -> dict:
    started = perf_counter()
    recalculate = generation is not None
    with connect() as db:
        records = current_records(db)
        registry = registered_pairs(db)
        if not recalculate:
            row = db.execute("SELECT generation FROM pair_v2_refresh_state WHERE id=1").fetchone()
            generation = row["generation"] if row else None
    registered_ids = {pair["pair_id"] for pair in registry}
    scopes = [payload.scope, *(selection.scope for selection in payload.selected)]
    if any(scope.type != "GLOBAL" and scope.pair_id not in registered_ids for scope in scopes):
        raise HTTPException(404, "pair_not_found")
    version = data_version(records, registry)
    if payload.expected_data_version is not None and payload.expected_data_version != version:
        raise HTTPException(409, "analysis_base_stale")
    state = payload.model_dump(mode="json")
    state.pop("expected_data_version", None)
    state.pop("expected_methodology_version", None)
    try:
        methodology = MLClient()._request("GET", "/v2/methodology")
    except MLUnavailable as exc:
        raise HTTPException(503, exc.code) from None
    cache_version = methodology.get("cache_version")
    if not isinstance(cache_version, str) or not cache_version:
        raise HTTPException(502, "ml_invalid_methodology")
    if payload.expected_methodology_version is not None and payload.expected_methodology_version != cache_version:
        raise HTTPException(409, "methodology_base_stale")
    cache_key = digest({"records": records, "pairs": registry, **state, "pipeline": "pair-dashboard-2.3",
                        "methodology": cache_version, "generation": generation})
    if not preview:
        with connect() as db:
            cached = db.execute("SELECT result_json FROM pair_v2_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if cached:
            return json.loads(cached[0])
    try:
        ml_payload = {"records": records, "pairs": registry, **state}
        if recalculate:
            ml_payload["retry_errors"] = True
        result = MLClient()._request("POST", "/v2/analytics", ml_payload)
    except MLUnavailable as exc:
        if exc.code in ("ml_http_400", "ml_http_404", "ml_http_422"):
            raise HTTPException(int(exc.code.rsplit("_", 1)[1]), "invalid_analytics_scope_or_action") from None
        raise HTTPException(503, exc.code) from None
    try:
        result = AnalyticsResponse.model_validate(result).model_dump(mode="json", by_alias=True)
        validate_analysis(result, records, state["scope"], registry)
    except (ValueError, KeyError, TypeError, OverflowError):
        raise HTTPException(502, "ml_invalid_pair_analysis") from None
    result["data_version"] = version
    result["ml_analysis_version"] = result["analysis_version"]
    result["analysis_version"] = cache_key
    result["analysis_id"] = "analysis-" + uuid.uuid4().hex
    result["trace_id"] = "trace-" + uuid.uuid4().hex
    result["processing_ms"] = round(1000 * (perf_counter()-started), 2)
    with write_lock, connect() as db:
        # A concurrent delivery must never leave an obsolete cache entry.
        if data_version(current_records(db), registered_pairs(db)) != version:
            raise HTTPException(409, "analysis_inputs_changed_retry")
        timestamp = now()
        result["methodology_cache_version"] = cache_version
        if preview:
            result["updated_at"] = timestamp if records else None
            return result
        for record in records:
            db.execute("INSERT OR IGNORE INTO pair_v2_analysis_times VALUES (?,?,?)",
                       (record["source_id"], cache_version, timestamp))
        chosen = records if payload.scope.type == "GLOBAL" else [r for r in records
                  if r["pair_id"] == payload.scope.pair_id and payload.scope.start <= r["month"] <= payload.scope.end]
        analysis_times = [db.execute("SELECT analyzed_at FROM pair_v2_analysis_times WHERE source_id=? AND methodology=?",
                                    (r["source_id"], cache_version)).fetchone()[0] for r in chosen]
        if recalculate:
            db.execute("INSERT OR REPLACE INTO pair_v2_refresh_state VALUES (1,?)", (generation,))
            db.executemany("UPDATE pair_v2_analysis_times SET analyzed_at=? WHERE source_id=? AND methodology=?",
                           ((timestamp, r["source_id"], cache_version) for r in records))
        result["updated_at"] = timestamp if recalculate and records else max(analysis_times, default=None)
        db.execute("INSERT OR REPLACE INTO pair_v2_cache VALUES (?,?,?)", (cache_key, encode(result), now()))
        db.execute("INSERT INTO pair_v2_audit VALUES (?,?,?,?)",
                   (result["analysis_id"], cache_key, encode(result), now()))
    return result


@router.post("/analytics", response_model=DashboardAnalysis)
def analyze(payload: AnalyticsRequest, preview: bool = False) -> dict:
    return analytics(payload, preview=preview)


@router.post("/recalculate", response_model=DashboardAnalysis)
def recalculate() -> dict:
    if not refresh_lock.acquire(blocking=False):
        raise HTTPException(409, "recalculation_in_progress")
    try:
        return analytics(AnalyticsRequest(), generation=uuid.uuid4().hex)
    finally:
        refresh_lock.release()


@router.get("/analysis/{analysis_id}")
def replay(analysis_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT result_json FROM pair_v2_audit WHERE id=?", (analysis_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "analysis_not_found")
    return json.loads(row[0])


@router.get("/records")
def records() -> dict:
    values = current_records()
    return {"records": values, "data_version": data_version(values)}


@router.get("/revision")
def revision() -> dict:
    with connect() as db:
        row = db.execute("SELECT COUNT(*), MAX(received_at) FROM pair_v2_surveys").fetchone()
        registry_count = db.execute("SELECT COUNT(*) FROM pair_v2_registry").fetchone()[0]
        generation = db.execute("SELECT generation FROM pair_v2_refresh_state WHERE id=1").fetchone()
    return {"revision": digest([*row, registry_count, generation[0] if generation else None])}


@router.get("/pairs")
def pairs() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM pair_v2_registry ORDER BY intern_id")]


@router.post("/pairs")
def add_pair(payload: PairRegistration, x_ingest_key: str | None = Header(default=None)) -> dict:
    require_key(x_ingest_key)
    try:
        with write_lock, connect() as db:
            pair_id = register_pair(db, payload.intern_id, payload.mentor_id, payload.department)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"pair_id": pair_id}


@router.get("/source/{source_id}")
def get_source(source_id: str) -> dict:
    value = source(source_id)
    if value is None:
        raise HTTPException(404, "source_not_found")
    return value


def refresh() -> None:
    try:
        analytics(AnalyticsRequest())
        broker.publish({"type": "pair_analytics_updated"})
    except HTTPException:
        broker.publish({"type": "pair_analytics_unavailable"})


@router.post("/ingest")
def receive(payload: SurveyInput, background: BackgroundTasks,
            x_delivery_id: str = Header(min_length=1, max_length=200),
            x_ingest_key: str | None = Header(default=None)) -> dict:
    require_key(x_ingest_key)
    try:
        result = ingest(payload, "normalized:" + x_delivery_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if result["status"] == "accepted":
        broker.publish({"type": "pair_survey_received", "source_id": result["source_id"]})
        background.add_task(refresh)
    return result


def normalize_form(payload: dict) -> SurveyInput:
    """Configured JSON-RPC params use explicit canonical question identifiers."""
    params = payload.get("params", payload)
    if not isinstance(params, dict):
        raise ValueError("params_object_required")
    role = params.get("role")
    ratings = params.get("ratings")
    texts = params.get("texts")
    def form_integer(value):
        if type(value) is int:
            return value
        if isinstance(value, str) and value.isdecimal():
            return int(value)
        raise ValueError("integer_answer_required")

    if ratings is None:
        from .pair_import import RATINGS, TEXTS
        ratings = {key: form_integer(params[key]) for key in RATINGS.get(role, []) if params.get(key) not in (None, "")}
        texts = {key: params[key] for key in TEXTS.get(role, []) if params.get(key) not in (None, "")}
    month = params.get("month")
    if isinstance(month, str) and month.startswith("Месяц "):
        month = month.removeprefix("Месяц ")
    elif isinstance(month, str) and month.startswith(("M", "М")):
        month = month[1:]
    response_id = params.get("response_id")
    if type(response_id) is int:
        response_id = str(response_id)
    return SurveyInput(
        intern_id=params.get("intern_id"), mentor_id=params.get("mentor_id") or None,
        department=params.get("department"), role=role, month=form_integer(month),
        ratings=ratings, texts=texts or {}, external_id=response_id,
    )


def yandex_form_ids():
    cfg = get_settings()
    return {"mentor": cfg.yandex_mentor_form_id, "intern": cfg.yandex_intern_form_id}


def note_yandex(db, role, form_id, *, accepted=0, error=None):
    timestamp = now()
    db.execute("""
        INSERT INTO pair_v2_yandex_status VALUES (?,?,?,?,?,?)
        ON CONFLICT(role,form_id) DO UPDATE SET accepted=accepted+excluded.accepted,
        last_success=coalesce(excluded.last_success,last_success),
        last_error=excluded.last_error,updated_at=excluded.updated_at
        """, (role, form_id, accepted, None if error else timestamp, error, timestamp))


@router.get("/forms/status")
def yandex_status() -> dict:
    forms = []
    with connect() as db:
        for role, expected in yandex_form_ids().items():
            row = db.execute("""
                SELECT accepted,last_success,last_error FROM pair_v2_yandex_status
                WHERE role=? AND (?='' OR form_id=?) ORDER BY updated_at DESC LIMIT 1
                """, (role, expected, expected)).fetchone()
            forms.append({"role": role, "configured": bool(expected), **(dict(row) if row else
                         {"accepted": 0, "last_success": None, "last_error": None})})
    return {"mode": "webhook", "forms": forms}


@router.post("/forms/yandex")
async def yandex(request: Request, background: BackgroundTasks,
                 x_ingest_key: str | None = Header(default=None)) -> dict:
    body = await request.body()
    if len(body) > 200000:
        raise HTTPException(413, "event_too_large")
    payload = None
    role = form_id = None
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("object_required")
        params = payload.get("params", payload)
        if not isinstance(params, dict):
            raise ValueError("params_object_required")
        supplied = params.pop("key", None)
        require_key(x_ingest_key or (supplied if isinstance(supplied, str) else None))
        payload.pop("key", None)
        role, form_id = params.get("role"), params.get("form_id")
        if type(form_id) is int:
            form_id = str(form_id)
        allowed = yandex_form_ids()
        if not isinstance(role, str) or role not in allowed:
            raise ValueError("unknown_form_role")
        if form_id is None and not allowed[role]:
            form_id = role
        if not isinstance(form_id, str) or not 0 < len(form_id) <= 200 or (allowed[role] and form_id != allowed[role]):
            raise ValueError("wrong_form_id")
        if "jsonrpc" in payload and (payload["jsonrpc"] != "2.0" or payload.get("method") != "submit"):
            raise ValueError("invalid_rpc_method")
        survey = normalize_form(payload)
        response_id = survey.external_id
        revision_id = params.get("revision_id")
        if not response_id:
            raise ValueError("response_id_required")
        # Response ID identifies the submission, content/revision identifies its edition.
        event_id = "yandex:" + digest([form_id, response_id,
                                       revision_id or digest(survey.model_dump(mode="json"))])
        with write_lock, connect() as db:
            result = accept(db, survey, event_id, payload)
            note_yandex(db, role, form_id, accepted=int(result["status"] == "accepted"))
        if result["status"] == "accepted":
            broker.publish({"type": "pair_survey_received", "source_id": result["source_id"]})
            background.add_task(refresh)
        if payload.get("jsonrpc") == "2.0":
            return {"jsonrpc": "2.0", "id": payload.get("id"), "result": result}
        return result
    except (ValueError, TypeError, ValidationError) as exc:
        if isinstance(role, str) and role in yandex_form_ids():
            expected = yandex_form_ids()[role]
            logged_id = expected or (form_id if isinstance(form_id, str) and 0 < len(form_id) <= 200 else None)
            if logged_id:
                reason = str(exc) if type(exc) is ValueError else ""
                error = reason if reason in ("wrong_form_id", "intern_pair_not_registered",
                                             "delivery_id_content_conflict", "response_id_required",
                                             "invalid_rpc_method") else "invalid_or_conflicting_survey"
                with write_lock, connect() as db:
                    note_yandex(db, role, logged_id, error=error)
        if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0":
            return {"jsonrpc": "2.0", "id": payload.get("id"),
                    "error": {"code": -32602, "message": "invalid_or_conflicting_survey"}}
        raise HTTPException(422, "invalid_or_conflicting_survey") from None


@router.get("/scenarios")
def scenarios() -> list[dict]:
    with connect() as db:
        return [dict(row) for row in db.execute(
            """SELECT scenario_id,name,stale,updated_at,
               json_extract(result_json,'$.methodology_cache_version') AS methodology_cache_version
               FROM pair_v2_scenarios ORDER BY updated_at DESC""")]


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT * FROM pair_v2_scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
        if not row:
            raise HTTPException(404, "scenario_not_found")
        history = db.execute("SELECT * FROM pair_v2_scenario_events WHERE scenario_id=? ORDER BY id", (scenario_id,)).fetchall()
    return {"scenario_id": scenario_id, "name": row["name"], "stale": bool(row["stale"]),
            "state": json.loads(row["state_json"]), "result": json.loads(row["result_json"]),
            "data_version": row["data_version"], "updated_at": row["updated_at"],
            "history": [{"id": h["id"], "kind": h["kind"], "state": json.loads(h["state_json"]),
                         "created_at": h["created_at"]} for h in history]}


@router.post("/scenarios")
def save_scenario(payload: ScenarioSave) -> dict:
    state = {"scope": payload.scope.model_dump(mode="json"),
             "selected": [item.model_dump(mode="json") for item in payload.selected],
             "current_month": payload.current_month}
    result = analytics(AnalyticsRequest.model_validate({**state,
        "expected_data_version": payload.expected_data_version,
        "expected_methodology_version": payload.expected_methodology_version}))
    scenario_id = payload.scenario_id or "scenario-" + uuid.uuid4().hex
    with write_lock, connect() as db:
        if data_version(current_records(db), registered_pairs(db)) != result["data_version"]:
            raise HTTPException(409, "scenario_inputs_changed_retry")
        previous = db.execute("SELECT * FROM pair_v2_scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
        if payload.scenario_id and previous is None:
            raise HTTPException(404, "scenario_not_found")
        if previous and (previous["stale"] or json.loads(previous["result_json"]).get("methodology_cache_version") != result["methodology_cache_version"]):
            raise HTTPException(409, "scenario_base_stale")
        db.execute("INSERT OR REPLACE INTO pair_v2_scenarios VALUES (?,?,?,?,?,?,?)",
                   (scenario_id, payload.name, encode(state), result["data_version"], encode(result), 0, now()))
        kind = "updated" if previous else "created"
        db.execute("INSERT INTO pair_v2_scenario_events (scenario_id,kind,state_json,result_json,created_at) VALUES (?,?,?,?,?)",
                   (scenario_id, kind, encode(state), encode(result), now()))
        previous_choices = json.loads(previous["state_json"])["selected"] if previous else []
        old = {encode(item) for item in previous_choices}
        new = {encode(item) for item in state["selected"]}
        for event_kind, choices in (("selected", new - old), ("removed", old - new)):
            for choice in sorted(choices):
                db.execute("INSERT INTO pair_v2_scenario_events (scenario_id,kind,state_json,result_json,created_at) VALUES (?,?,?,?,?)",
                           (scenario_id, event_kind, encode({"selection": json.loads(choice)}), encode(result), now()))
    return get_scenario(scenario_id)


@router.post("/scenarios/{scenario_id}/restore")
def restore_scenario(scenario_id: str) -> dict:
    value = get_scenario(scenario_id)
    with connect() as db:
        changed = scenario_dependencies_changed(value["state"], value["result"], current_records(db), registered_pairs(db))
    if changed:
        raise HTTPException(409, "scenario_base_stale")
    fresh = analytics(AnalyticsRequest.model_validate({**value["state"],
        "expected_methodology_version": value["result"].get("methodology_cache_version")}))
    with write_lock, connect() as db:
        current = db.execute("SELECT stale,updated_at FROM pair_v2_scenarios WHERE scenario_id=?", (scenario_id,)).fetchone()
        records, pairs = current_records(db), registered_pairs(db)
        if (not current or current["updated_at"] != value["updated_at"]
                or data_version(records, pairs) != fresh["data_version"]
                or scenario_dependencies_changed(value["state"], value["result"], records, pairs)):
            raise HTTPException(409, "scenario_base_stale")
        db.execute("UPDATE pair_v2_scenarios SET stale=0 WHERE scenario_id=?", (scenario_id,))
        db.execute("INSERT INTO pair_v2_scenario_events (scenario_id,kind,state_json,result_json,created_at) VALUES (?,?,?,?,?)",
                   (scenario_id, "restored", encode(value["state"]), encode(fresh), now()))
    restored = get_scenario(scenario_id)
    return {**restored, "snapshot_result": value["result"], "snapshot_data_version": value["data_version"],
            "result": fresh, "data_version": fresh["data_version"]}
