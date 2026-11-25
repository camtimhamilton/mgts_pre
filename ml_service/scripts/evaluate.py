"""Measure fixture integration regression only, never infer real NLP accuracy."""
import json
import math
import statistics
import tempfile
from pathlib import Path
from time import perf_counter

from fastapi.testclient import TestClient

from app.knowledge import ROOT
from app.main import create_app
from app.llm import FixtureProvider


def main():
    cases = json.loads((ROOT / "tests/fixtures/mock_extractions.json").read_text(encoding="utf-8"))["cases"]
    totals = {f"R{i:02}": {"tp": 0, "fp": 0, "fn": 0} for i in range(1, 11)}
    elapsed, raw_count, accepted, rejected, review, abstentions, errors = [], 0, 0, 0, 0, 0, 0
    with tempfile.TemporaryDirectory() as directory:
        with TestClient(create_app(Path(directory) / "evaluation.sqlite3", FixtureProvider())) as client:
            for case in cases:
                payload = {"program_id": "synthetic", "intern_id": "synthetic", "period": 3, "responses": [{
                    "response_id": "fixture_response", "respondent_role": case["input"]["respondent_role"],
                    "answers": case["input"]["answers"]}]}
                start = perf_counter()
                response = client.post("/v1/analyze?mode=live", json=payload)
                elapsed.append((perf_counter() - start) * 1000)
                raw_count += len(case["output"]["signals"])
                if response.status_code != 200:
                    errors += 1
                    continue
                result = response.json()
                actual = {f["id"] for f in result["factors"] if f["contribution"] > 0}
                expected = set(case["expected_active_factors"])
                accepted += result["metadata"]["accepted_count"]
                rejected += result["metadata"]["rejected_count"]
                review += result["metadata"]["review_count"]
                abstentions += not bool(actual)
                for factor, counts in totals.items():
                    counts["tp"] += factor in actual and factor in expected
                    counts["fp"] += factor in actual and factor not in expected
                    counts["fn"] += factor not in actual and factor in expected
    per_factor = {}
    for factor, counts in totals.items():
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
        per_factor[factor] = {**counts, "precision": precision, "recall": recall, "f1": f1}
    supported = [v["f1"] for v in per_factor.values() if v["f1"] is not None]
    report = {"scope": "synthetic fixture contract regression; outputs scripted; NOT independent NLP quality",
        "cases": len(cases), "errors": errors, "raw_signals": raw_count, "accepted_unique_signals": accepted,
        "rejected_unique_signals": rejected, "review_unique_signals": review, "no_active_factor_cases": abstentions,
        "per_factor": per_factor, "macro_f1_supported_labels_only": statistics.mean(supported) if supported else None,
        "uncovered_labels": [f for f, value in per_factor.items() if value["f1"] is None],
        "semantic_correctness": "not_independently_evaluated", "provider": "mock", "cache": "none",
        "api_latency_ms_p50": round(statistics.median(elapsed), 3),
        "api_latency_ms_p95_nearest_rank": round(sorted(elapsed)[math.ceil(0.95 * len(elapsed)) - 1], 3)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors or any(v["fp"] or v["fn"] for v in totals.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
