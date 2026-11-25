"""Explicit synthetic integration mapping/catalogue; no general NLP simulation."""
import json
from pathlib import Path

from app.ml_mapping import QUESTIONS

ROOT = Path(__file__).resolve().parents[1]
TEXT = "Наставник регулярно не отвечает. Не совсем понимаю, какого результата от меня ждут."


def main():
    # Only clearly corresponding demo questions. Compound mentor and future/motivation stay unmapped.
    entries = []
    for role in ("intern", "mentor"):
        for key, factor in (("clarity", "R03"), ("workload", "R04"), ("inclusion", "R07")):
            entries.append({"role": role, "question_id": "f_" + key, "type": "rating", "factor_id": factor,
                "comparison_key": key if key in {"clarity", "inclusion"} else None,
                "direction": "higher_is_better", "scale_min": 1, "scale_max": 10,
                "levels": {str(n): "high" if n <= 2 else "moderate" if n <= 5 else "none" for n in range(1, 11)}})
        entries.extend({"role": role, "question_id": q, "type": "text"} for q in ("q_better", "q_barrier"))
    path = ROOT / "ml_service/mappings/dashboard-demo.yaml"
    path.write_text(json.dumps({"version": "dashboard-demo-1", "entries": entries}, ensure_ascii=False, indent=2), encoding="utf-8")
    baseline = json.loads((ROOT / "ml_service/tests/fixtures/mock_extractions.json").read_text(encoding="utf-8"))
    cases = []
    for period in range(1, 7):
        for role in ("intern", "mentor"):
            alternatives = [[("q_better", "Всё стабильно.")]]
            if role == "intern":
                alternatives += [[("q_barrier", TEXT)], [("q_better", "Всё стабильно."), ("q_barrier", TEXT)]]
            for n, answers in enumerate(alternatives):
                signals = []
                if any(key == "q_barrier" for key, value in answers):
                    signals = [dict(s, question_id="q_barrier") for s in baseline["cases"][0]["output"]["signals"]]
                cases.append({"id": f"dashboard_{period}_{role}_{n}", "input": {"respondent_role": role,
                    "period": period, "program_day": None, "context": {}, "answers": [{"type": "text", "question_id": q,
                        "question_text": QUESTIONS[role][q], "value": text} for q, text in answers]},
                    "output": {"signals": signals, "unclassified_fragments": [], "requires_review": False},
                    "expected_active_factors": ["R01", "R03"] if signals else []})
    catalogue = {"synthetic": True, "purpose": "explicit integration templates only",
                 "knowledge_hash": baseline["knowledge_hash"], "prompt_hash": baseline["prompt_hash"], "cases": cases}
    (ROOT / "ml_service/tests/fixtures/dashboard_mock_extractions.json").write_text(json.dumps(catalogue,
        ensure_ascii=False, indent=2), encoding="utf-8")
    payloads = [{"s_name": "Синтетический Стажёр", "s_mentor": "Синтетический Наставник", "s_role": role,
        "s_month": "M3", "f_clarity": 4 if role == "intern" else 10,
        "q_barrier": TEXT if role == "intern" else ""} for role in ("intern", "mentor")]
    (ROOT / "integration/synthetic_ingest.json").write_text(json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
