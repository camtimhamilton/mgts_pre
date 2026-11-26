"""Conservative, source-linked comparisons of explicitly phrased observations."""
import hashlib
import json
from pathlib import Path
import re


def load_semantic_rules() -> dict:
    path = Path(__file__).resolve().parents[1] / "knowledge_base/v2/semantic_rules.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _observations(record: dict | None, aspect: str, rules: dict) -> list[dict]:
    if record is None:
        return []
    result = []
    for question_id, answer in record["texts"].items():
        for match in re.finditer(r"[^.!?;\n]+[.!?]?", answer or ""):
            quote = match.group().strip()
            if not quote or re.search(rules["caution"], quote, re.I):
                continue
            states = [state for state, patterns in rules["aspects"][aspect].items()
                      if any(re.search(pattern, quote, re.I) for pattern in patterns)]
            if len(states) != 1:
                continue
            identity = f'{record["source_id"]}:{record["version"]}:{question_id}:semantic:{aspect}:{match.start()}'
            evidence = {"id": hashlib.sha256(identity.encode()).hexdigest()[:20],
                        **{key: record[key] for key in ("source_id", "version", "pair_id", "month", "role")},
                        "question_id": question_id, "quote": quote, "kind": "text", "value": None,
                        "validation_status": "project_rule_provisional", "rule_id": f"comparison.{aspect}.{states[0]}"}
            result.append({"state": states[0], "evidence": evidence})
    return result


def compare_texts(mentor: dict | None, intern: dict | None, pair_id: str, month: int,
                  comparable_aspects: list[str], rules: dict) -> list[dict]:
    result = []
    for aspect in comparable_aspects:
        mentor_observations = _observations(mentor, aspect, rules)
        intern_observations = _observations(intern, aspect, rules)
        mentor_states = {item["state"] for item in mentor_observations}
        intern_states = {item["state"] for item in intern_observations}
        evidence = [item["evidence"] for item in mentor_observations + intern_observations]
        kind, status = "NOT_COMPARABLE", "abstained"
        explanation = "Недостаточно явных сопоставимых текстовых утверждений по этому аспекту."
        if "awareness_gap" in intern_states:
            kind, status = "AWARENESS_GAP", "requires_clarification"
            explanation = "Стажёр прямо сообщает, что наставнику неизвестна проблема. Это сообщение требует конфиденциального уточнения у обеих сторон."
        elif len(mentor_states) == 1 and len(intern_states) == 1:
            mentor_state, intern_state = next(iter(mentor_states)), next(iter(intern_states))
            if mentor_state == intern_state and mentor_state in ("positive", "negative"):
                kind, status = "AGREEMENT", "project_rule_provisional"
                explanation = "Обе стороны явно описывают сопоставимый аспект с одинаковой направленностью."
            elif {mentor_state, intern_state} == {"positive", "negative"}:
                kind, status = "CONTRADICTION", "requires_clarification"
                explanation = "Одна сторона явно описывает благополучие по аспекту, другая — текущую сложность. Различие перспектив требует уточнения."
            elif "proposal" in (mentor_state, intern_state) and "negative" in (mentor_state, intern_state):
                kind, status = "COMPLEMENTARY", "project_rule_provisional"
                explanation = "Сообщение о сложности дополнено предложением по тому же аспекту. Предложение сохраняется как контекст."
        if len(mentor_states) > 1 or len(intern_states) > 1:
            kind, status = "NOT_COMPARABLE", "requires_clarification"
            explanation = "Внутри одной перспективы есть несколько разных утверждений. Автоматическое семантическое сопоставление приостановлено."
        result.append({"pair_id": pair_id, "month": month, "aspect": aspect, "type": kind,
                       "status": status, "mentor_states": sorted(mentor_states), "intern_states": sorted(intern_states),
                       "explanation": explanation, "evidence": evidence,
                       "evidence_refs": [item["id"] for item in evidence], "rule_version": rules["version"],
                       "score_effect": 0})
    return result
