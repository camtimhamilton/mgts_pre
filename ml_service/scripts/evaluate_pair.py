"""Engineering check on synthetic sentences, with role-specific topic labels."""
import argparse
import json
from collections import Counter
from app.knowledge import ROOT
from app.pulse_nlp import local_signals, resources


def evaluate(with_encoder=False):
    cases = json.loads((ROOT / "tests/fixtures/pair_text_checks.json").read_text("utf-8"))["cases"]
    rules, _, _, policy = resources()
    counts = Counter()
    for case in cases:
        record = {"role": case["role"], "texts": {"sample": case["text"]}}
        expected = {case["topic"]} if case["current_problem"] else set()
        detected = {s["factor_id"] for s in local_signals(record, rules) if s["validation_status"] == "accepted"
                    and s["temporal_status"] == "current" and s["polarity"] == "negative"}
        counts["tp"] += len(expected & detected)
        counts["fp"] += len(detected - expected)
        counts["fn"] += len(expected - detected)
        if with_encoder:
            from app.local_encoder import rank
            suggestions = rank(record, policy["roles"][case["role"]])
            if suggestions:
                counts["suggested"] += 1
                counts["correct_topic"] += suggestions[0]["factor_id"] == case["topic"]
    tp, fp, fn = (counts[k] for k in ("tp", "fp", "fn"))
    def ratio(n, d):
        return round(n / d, 4) if d else None
    return {"synthetic": True, "cases": len(cases), "rules": {"tp":tp, "fp":fp, "fn":fn,
            "precision":ratio(tp, tp+fp), "recall":ratio(tp,tp+fn)},
            "encoder": {"suggested": counts["suggested"], "correct_topic":counts["correct_topic"],
                        "abstained":len(cases)-counts["suggested"],
                        "precision_among_suggestions":ratio(counts["correct_topic"],counts["suggested"])} if with_encoder else None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--encoder", action="store_true")
    print(json.dumps(evaluate(parser.parse_args().encoder), indent=2))
