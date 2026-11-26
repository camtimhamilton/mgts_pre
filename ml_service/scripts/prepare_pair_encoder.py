"""Prepare pinned public ONNX assets and optionally exercise local inference."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import ssl
import sys
import time

import httpx

ML_ROOT = Path(__file__).resolve().parents[1]
ROOT = ML_ROOT.parent
sys.path.insert(0, str(ML_ROOT))
from app.local_encoder import ARTIFACTS, MODEL_ID, REVISION, check_artifacts, embed, encoder_version, model_directory, rank


def prepare(offline: bool) -> None:
    destination = model_directory()
    if not destination.is_relative_to((ROOT / "data").resolve()):
        raise ValueError("download assets must stay under ignored project data/")
    destination.mkdir(parents=True, exist_ok=True)
    if offline:
        check_artifacts(destination)
        return
    with httpx.Client(timeout=httpx.Timeout(90, connect=20), follow_redirects=True,
                      verify=ssl.create_default_context(), trust_env=False) as client:
        for name, metadata in ARTIFACTS.items():
            target = destination / name
            if target.is_file() and target.stat().st_size == metadata["size"]:
                if hashlib.sha256(target.read_bytes()).hexdigest() == metadata["sha256"]:
                    continue
            url = f'https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{metadata["remote"]}?download=true'
            partial = target.with_suffix(target.suffix + ".part")
            checksum = hashlib.sha256()
            received = 0
            try:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with partial.open("wb") as stream:
                        for chunk in response.iter_bytes(1024 * 1024):
                            received += len(chunk)
                            if received > metadata["size"]:
                                raise ValueError("download_exceeds_expected_size")
                            checksum.update(chunk)
                            stream.write(chunk)
                if received != metadata["size"] or checksum.hexdigest() != metadata["sha256"]:
                    raise ValueError("download_hash_or_size_mismatch")
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
    check_artifacts(destination)


def smoke() -> dict:
    import numpy as np

    policy = json.loads((ML_ROOT / "knowledge_base/v2/policy.json").read_text(encoding="utf-8"))
    examples = [
        ("Нагрузка слишком высокая, одновременно выполняю много задач.", "workload"),
        ("Мне нужна регулярная обратная связь наставника по моей работе.", "support"),
        ("Понимаю задачи и ожидаемый результат работы.", "clarity"),
    ]
    matrix = embed([text for text, _ in examples])
    assert matrix.shape == (3, 384)
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1, atol=1e-5)
    assert np.allclose(matrix, embed([text for text, _ in examples]), atol=1e-6)
    results = []
    for text, expected_topic in examples:
        matches = rank({"texts": {"synthetic": text}}, policy["roles"]["intern"])
        assert all(m["quote"] in text and m["severity"] == "none" and m["validation_status"] == "review" for m in matches)
        results.append({"expected_topic": expected_topic, "matched_topics": [m["factor_id"] for m in matches],
                        "similarities": [m["similarity"] for m in matches], "abstained": not matches})
    return {"synthetic_examples": results, "unit_norms": True, "deterministic_replay": True,
            "calibration": "provisional_min_similarity_0.35_margin_0.03"}


def history(database: Path, report_path: Path) -> dict:
    report_path = report_path.resolve()
    if not report_path.is_relative_to((ROOT / "data").resolve()):
        raise ValueError("private report must stay under ignored project data/")
    policy = json.loads((ML_ROOT / "knowledge_base/v2/policy.json").read_text(encoding="utf-8"))
    with sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True) as conn:
        rows = conn.execute("""SELECT s.record_json FROM pair_v2_surveys s WHERE version=(
          SELECT MAX(t.version) FROM pair_v2_surveys t
          WHERE t.pair_id=s.pair_id AND t.month=s.month AND t.role=s.role)
          ORDER BY s.pair_id,s.month,s.role""").fetchall()
    records = [json.loads(row[0]) for row in rows]
    started = time.perf_counter()
    samples = []
    counts = Counter()
    matched_records = 0
    for record in records:
        matches = rank(record, policy["roles"][record["role"]])
        matched_records += bool(matches)
        for match in matches:
            assert match["quote"] in record["texts"][match["question_id"]]
            assert match["validation_status"] == "review" and match["severity"] == "none"
            counts[f'{record["role"]}.{match["factor_id"]}'] += 1
        samples.append({"source_id": record["source_id"], "role": record["role"], "month": record["month"], "matches": matches})
    elapsed = time.perf_counter() - started
    summary = {"status": "passed", "encoder_version": encoder_version(), "records": len(records),
               "matched_records": matched_records, "abstained_records": len(records) - matched_records,
               "topic_matches": sum(counts.values()), "factor_counts": dict(counts), "elapsed_seconds": round(elapsed, 3),
               "outbound_survey_requests": 0, "all_matches_require_review": True, "risk_points_added": 0,
               "semantic_accuracy_evaluated": False}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({**summary, "records_detail": samples}, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--history-db", type=Path)
    parser.add_argument("--report", type=Path, default=ROOT / "data/review/encoder-history.json")
    args = parser.parse_args()
    prepare(args.offline)
    result = {"status": "prepared", "model": MODEL_ID, "revision": REVISION, "encoder_version": encoder_version()}
    if args.smoke:
        result["smoke"] = smoke()
    if args.history_db:
        result["history"] = history(args.history_db, args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
