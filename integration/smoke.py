"""Two real localhost processes, synthetic ingest, persistence and dashboard API.

All temporary processes/DBs are closed on exit. No LLM credentials are used.
"""
import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(check, timeout=20):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except (httpx.HTTPError, KeyError, IndexError) as error:
            last = type(error).__name__
        time.sleep(0.1)
    raise AssertionError("service/data not ready: " + str(last))


@contextmanager
def running_stack():
    ml_python = ROOT / "ml_service/.venv/Scripts/python.exe"
    if not ml_python.exists():
        ml_python = ROOT / "ml_service/.venv/bin/python"
    if not ml_python.exists():
        raise RuntimeError("Create ml_service/.venv first, see integration/README.md")
    processes = []
    with tempfile.TemporaryDirectory(prefix="pulse-integration-") as directory:
        temp = Path(directory)
        backend_port, ml_port = port(), port()
        base = f"http://127.0.0.1:{backend_port}"
        ml_base = f"http://127.0.0.1:{ml_port}"
        env = {k: v for k, v in os.environ.items() if k not in {"DEEPSEEK_API_KEY", "LLM_API_KEY"}}
        env.update(MGTS_PROVIDER="mock", MGTS_ALLOW_SYNTHETIC_LLM="0", MGTS_RUN_DEEPSEEK_SMOKE="0",
            MGTS_DATABASE=str(temp / "ml.sqlite3"),
            MGTS_MAPPING_PATH=str(ROOT / "ml_service/mappings/dashboard-demo.yaml"),
            MGTS_FIXTURE_CATALOG=str(ROOT / "ml_service/tests/fixtures/dashboard_mock_extractions.json"),
            ANALYSIS_MODE="ml", DEMO_MODE="false", LLM_MODE="fake", DB_PATH=str(temp / "backend.sqlite3"),
            ML_API_URL=ml_base, ML_API_TIMEOUT="1", ML_MAPPING_VERSION="dashboard-demo-1", ML_KNOWLEDGE_VERSION="0.1.0",
            ML_PROGRAM_ID="synthetic_dashboard", INGEST_KEY="")
        handles = []
        try:
            for name, executable, cwd, target, service_port in (
                ("ml", ml_python, ROOT / "ml_service", "app.main:create_app", ml_port),
                ("dashboard", Path(sys.executable), ROOT, "app.main:app", backend_port)):
                log = (temp / f"{name}.log").open("w", encoding="utf-8")
                handles.append(log)
                args = [str(executable), "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", str(service_port), "--log-level", "warning"]
                if name == "ml":
                    args.append("--factory")
                processes.append(subprocess.Popen(args, cwd=cwd, env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            with httpx.Client(timeout=3, trust_env=False) as client:
                wait_until(lambda: client.get(ml_base + "/health").status_code == 200)
                wait_until(lambda: client.get(base + "/api/health").status_code == 200)
                yield client, base, ml_base, temp, processes
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for handle in handles:
                handle.close()


def exercise(client, base, ml_base, temp, processes, export=False, outage=True):
    def get(path):
        response = client.get(base + path)
        response.raise_for_status()
        return response.json()
    def post(path, body):
        response = client.post(base + path, json=body)
        response.raise_for_status()
        return response.json()
    payloads = json.loads((ROOT / "integration/synthetic_ingest.json").read_text(encoding="utf-8"))
    assert get("/api/health")["analysis_mode"] == "ml"
    assert get("/api/program")["kpi"]["avg_risk"] is None
    for payload in payloads:
        response = post("/api/ingest", payload)
        assert response["submission"]["status"] == "created", response
    def ready():
        pairs = get("/api/pairs?month=M3")["items"]
        if not pairs:
            return None
        card = get(f"/api/pairs/{pairs[0]['id']}?month=M3")
        return card if card["analysis_state"] == "ready" and not card["missing_required_roles"] else None
    card = wait_until(ready)
    pair_id = card["id"]
    assert card["risk"] == 17 and sum(f["points"] for f in card["factors"]) == 17
    assert card["data_coverage"] == 0.22 and card["priority"] == "insufficient_data"
    assert len(card["factors"]) == 10 and card["has_gap"]
    assert card["delta"] is None and card["temporal_status"] == "not_implemented"
    assert card["history"][1]["risk"] is None
    assert card["scenario"]["scenario_score"] == 0  # all actions include resolution of R03 too
    ml_result = client.get(ml_base + "/v1/analysis/" + card["ml_analysis_id"]).json()
    assert ml_result["risk"]["score"] == card["risk"]
    for actions in (["A01"], ["A01", "A11"], ["A01", "A01"]):
        result = post(f"/api/pairs/{pair_id}/scenario?month=M3", {"action_ids": actions})
        assert result["scenario_score"] == 5 and result["potential_reduction"] == 12
    assert get(f"/api/pairs/{pair_id}?month=M3")["risk"] == 17
    def run_count():
        with sqlite3.connect(temp / "ml.sqlite3") as conn:
            return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    before = run_count()
    for _ in range(3):
        get(f"/api/pairs/{pair_id}?month=M3")
    post(f"/api/pairs/{pair_id}/recompute?month=M3", {})
    assert before == run_count()
    m1 = dict(payloads[0], s_month="M1", q_barrier="", f_clarity=10)
    post("/api/ingest", m1)
    wait_until(lambda: get(f"/api/pairs/{pair_id}?month=M1")["risk"] == 0)
    missing = get(f"/api/pairs/{pair_id}?month=M2")
    assert missing["risk"] is None and missing["analysis_state"] == "missing"
    assert get(f"/api/pairs/{pair_id}?month=M3")["history"] == [
        {"month": "M1", "risk": 0.0, "band": "low"}, {"month": "M2", "risk": None, "band": "unknown"},
        {"month": "M3", "risk": 17.0, "band": "low"}]
    if export:
        destination = ROOT / "integration/examples"
        destination.mkdir(exist_ok=True)
        from app.config import get_settings
        from app.ml_mapping import survey_payload
        from app.normalize import from_payload
        request = survey_payload(pair_id, "M3", [from_payload(p).as_dict() for p in payloads])
        artifacts = {"ml_request.json": request, "ml_response.json": ml_result,
            "dashboard_card.json": card, "scenario_response.json": result, "ingest_requests.json": payloads}
        for filename, data in artifacts.items():
            (destination / filename).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (ROOT / "integration/openapi.json").write_text(json.dumps(client.get(ml_base + "/openapi.json").json(),
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (ROOT / "integration/dashboard_openapi.json").write_text(json.dumps(get("/openapi.json"),
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if outage:
        processes[0].terminate()
        processes[0].wait(timeout=5)
        failure = dict(payloads[0], s_month="M4")
        post("/api/ingest", failure)
        failed = wait_until(lambda: (c if (c := get(f"/api/pairs/{pair_id}?month=M4"))["analysis_state"] == "error" else None))
        assert failed["risk"] is None and failed["error_code"] in {"ml_unavailable", "ml_timeout"}, failed
        assert get(f"/api/pairs/{pair_id}?month=M3")["risk"] == 17
    return {"status": "passed", "synthetic": True, "provider": "mock", "score": 17, "coverage": 0.22,
        "scenario_score": 5, "gap": next(f["gap"] for f in card["factors"] if f["key"] == "R03"),
        "checks": ["ingest", "HTTP ML", "persistence", "dashboard parity", "factor sum", "scenario union",
                   "GET/recompute cache", "missing month", "separate months", "ML outage"]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", action="store_true")
    args = parser.parse_args()
    with running_stack() as stack:
        report = exercise(*stack, export=args.export)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
