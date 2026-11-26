"""Verify provided XLSX history against saved sources and data-driven situations.

Detailed evidence remains under ignored data/review. No corporate text is embedded
in this harness or written to tracked fixtures.
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean

from app.pair_import import read_workbook

ROOT = Path(__file__).resolve().parents[1]


def check_history(mentor_path, intern_path, client=None, base=None):
    originals = read_workbook(mentor_path, "mentor") + read_workbook(intern_path, "intern")
    assert len(originals) == 60
    by_key = {(s.intern_id, s.role, s.month): (s, raw) for s, raw in originals}
    assert len(by_key) == 60
    assert len({s.intern_id for s, _ in originals}) == 5
    assert {s.month for s, _ in originals} == set(range(1, 7))
    if client:
        records = client.get(base + "/api/v2/records").json()["records"]
        assert len(records) == 60
        for record in records:
            original, raw = by_key[record["intern_id"], record["role"], record["month"]]
            assert record["ratings"] == original.ratings
            assert record["texts"] == original.texts
            source = client.get(base + "/api/v2/source/" + record["source_id"]).json()
            assert source["raw"] == raw
    cases = defaultdict(list)
    intern_ids = sorted({s.intern_id for s, _ in originals})
    for intern_id in intern_ids:
        interns = [by_key[intern_id, "intern", month][0] for month in range(1, 7)]
        mentors = [by_key[intern_id, "mentor", month][0] for month in range(1, 7)]
        means = [mean(s.ratings.values()) for s in interns]
        if means[-1] > means[0] and all(a <= b for a, b in zip(means, means[1:])):
            cases["gradual_adaptation"].append({"intern_id": intern_id, "means": means})
        if all(value >= 4 for s in interns for value in s.ratings.values()):
            cases["stable_high_ratings"].append({"intern_id": intern_id})
        if min(means[1:-1]) < means[0] and means[-1] > min(means):
            cases["deterioration_then_improvement"].append({"intern_id": intern_id, "means": means})
        for index in range(1, 5):
            if interns[index].ratings["workload"] <= 2 and all(interns[i].ratings["workload"] >= 4 for i in (index - 1, index + 1)):
                cases["local_workload_problem"].append({"intern_id": intern_id, "month": index + 1})
        for question in ("support", "team", "clarity"):
            months = [s.month for s in interns if s.ratings[question] <= 3]
            if len(months) >= 2:
                cases["recurring_problem"].append({"intern_id": intern_id, "question_id": question, "months": months})
            if question in ("support", "team") and any(all(month + offset in months for offset in range(3)) for month in months):
                cases["prolonged_communication_difficulty"].append({"intern_id": intern_id, "question_id": question, "months": months})
        for mentor, intern in zip(mentors, interns):
            if abs(mentor.ratings["clarity"] - intern.ratings["clarity"]) >= 2:
                cases["task_perception_gap"].append({"intern_id": intern_id, "month": intern.month,
                    "mentor": mentor.ratings["clarity"], "intern": intern.ratings["clarity"]})
        for index, intern in enumerate(interns):
            for question, text in intern.texts.items():
                if index and interns[index - 1].ratings["workload"] <= 2 and intern.ratings["workload"] >= 4 and re.search(r"нагрузк\w* вернул\w* к комфортн", text, re.I):
                    cases["explicit_resolution"].append({"intern_id": intern_id, "month": intern.month,
                        "question_id": question, "quote": text, "status": "direct_self_report_with_rating_improvement"})
    expected = {"gradual_adaptation", "stable_high_ratings", "deterioration_then_improvement", "local_workload_problem",
                "prolonged_communication_difficulty", "task_perception_gap", "recurring_problem", "explicit_resolution"}
    assert expected <= cases.keys(), sorted(expected - cases.keys())
    reviewed = []
    for s, raw in originals:
        if s.intern_id.endswith(("003", "004", "005")):
            reviewed.append({"intern_id": s.intern_id, "role": s.role, "month": s.month,
                "ratings": s.ratings, "texts": s.texts, "source_row": raw["row"], "source_file": raw["file"]})
    return {"status": "passed", "rows": len(originals), "pairs": len(intern_ids), "months": 6,
            "cases": dict(cases), "reviewed_source_records": reviewed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mentor", required=True)
    parser.add_argument("--intern", required=True)
    parser.add_argument("--output", default=str(ROOT / "data/review/history-checks.json"))
    args = parser.parse_args()
    report = check_history(args.mentor, args.intern)
    destination = Path(args.output).resolve()
    if not destination.is_relative_to((ROOT / "data").resolve()):
        raise ValueError("confidential report must stay under ignored data/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "pairs": report["pairs"], "rows": report["rows"],
                      "case_types": sorted(report["cases"]), "output": str(destination)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
