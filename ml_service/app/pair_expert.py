"""Adapt the supplied expert pack to the existing pair analytics and survey wire keys."""
from functools import lru_cache
from pathlib import Path

WIRE = {
    "mentor": ("clarity", "quality", "autonomy", "deadlines", "initiative", "team"),
    "intern": ("clarity", "support", "workload", "interest", "growth", "team", "satisfaction"),
}

# General actions that can address an observed score without inferring its cause.
RATING_ACTIONS = {
    "M_A01": ("M01",), "M_A02": ("M02",), "M_A03": ("M03",), "M_A04": ("M04",),
    "M_A07": ("M05",), "M_A08": ("M06",), "S_A01": ("S01",), "S_A02": ("S02",),
    "S_A04": ("S03",), "S_A06": ("S04",), "S_A07": ("S05",), "S_A09": ("S06",),
    "S_A11": ("S07",), "P01": ("M01", "S01"), "P02": ("M06", "S02"), "P04": ("M03", "S02"),
}

DISCUSSION_QUESTIONS = {
    "M01": "Уточнить, какие задачи требуют дополнительных пояснений",
    "M02": "Уточнить, по каким результатам расходятся ожидания",
    "M03": "Обсудить, на каких этапах нужна помощь",
    "M04": "Уточнить, что затрудняет соблюдение сроков",
    "M05": "Уточнить, какие рабочие препятствия мешают участию",
    "M06": "Обсудить, где затруднена рабочая коммуникация",
    "S01": "Уточнить, какие задачи и критерии результата остаются неясными",
    "S02": "Обсудить регулярность обратной связи и доступность помощи",
    "S03": "Уточнить, какой объём работы был бы комфортным",
    "S04": "Обсудить, какие задачи вызывают интерес",
    "S05": "Уточнить, каких возможностей для развития не хватает",
    "S06": "Обсудить, в каких ситуациях затруднено взаимодействие с командой",
    "S07": "Уточнить, какой опыт стажировки требует обсуждения",
}


@lru_cache
def expert_policy(archive=False):
    import yaml

    root = Path(__file__).resolve().parents[1] / "knowledge_base/pulse_kb_v1"

    def read(name):
        return yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))

    scoring = read("scoring_policy_v1" if archive else "scoring_policy")
    mapping = read("question_mapping")["roles"]
    roles, rules = {}, {}
    for role, wire_keys in WIRE.items():
        questions = [q for q in mapping[role]["questions"] if q["type"] == "rating"]
        aliases = dict(zip((q["factor_id"] for q in questions), wire_keys, strict=True))
        roles[role] = {
            f["id"]: {"name": f["name"], "weight": scoring["role_weights"][role][f["id"]],
                      "wire_key": aliases[f["id"]], "source_ids": f["source_ids"]}
            for f in read(f"{role}_factors")["factors"]
        }
        rules.update({r["id"]: r for r in read(f"{role}_rules")["rules"]})
        if sum(f["weight"] for f in roles[role].values()) != 100:
            raise ValueError("expert role weights must sum to 100")
    action_config = read("actions")
    actions = {a["id"]: {**a, "rating_targets": RATING_ACTIONS.get(a["id"], ()) if not archive else ()}
               for a in action_config["actions"]}
    if any(rule_id not in rules for a in actions.values() for rule_id in a["target_rules"]):
        raise ValueError("unknown expert action rule")
    ontology = read("shared_ontology")
    critical = read("critical_flags")
    sources = {s["id"]: s["title"] for s in read("sources")["sources"]}
    zones = {zone["id"]: zone["min_inclusive"] for zone in scoring["zones"] if "min_inclusive" in zone}
    return {
        "expert": True, "archive": archive, "version": scoring["version"], "status": scoring["status"],
        "source": "Экспертная база ПУЛЬС", "roles": roles, "rules": rules, "actions": actions,
        "sources": sources,
        "unscored": {a["id"]: a for a in action_config["non_scoring_actions"]},
        "critical": critical["flags"], "additional": critical["additional_signals"],
        "rating_levels": {str(k): v for k, v in scoring["rating_severity"].items()},
        "min_coverage": scoring["coverage"]["minimum"],
        "rating_gap_threshold": scoring["flags"]["gap_threshold"],
        "thresholds": {key: zones[key] for key in ("yellow", "red")},
        "comparisons": [],
        "numeric_pairs": [(r["mentor_factor"], r["intern_factor"]) for r in ontology["relationships"]
                          if r["allow_numeric_gap"]],
        "ontology": ontology,
    }


def rating_matches(cell, factor, action):
    return factor["id"] in action.get("rating_targets", ()) and (
        factor["rating"] in (1, 2)
        or factor["rating"] == 3 and factor["instance_id"] in cell.get("repeated_ratings", ()))


def action_matches(cell, side, factor, action):
    if factor["id"] not in action["target_factors"] or not factor["contribution"]:
        return False
    if action.get("rating_resolution"):
        return factor["rating"] in (1, 2) and not factor["active_rules"]
    def supports(candidate):
        return candidate["contribution"] > 0 and candidate["id"] in action["target_factors"] and (
            rating_matches(cell, candidate, action) or bool(set(candidate["active_rules"]) & set(action["target_rules"])))

    if not supports(factor):
        return False
    if action["scope"] != "PAIR":
        return action["scope"] == ("MENTOR" if side == "x" else "INTERN")
    other = "y" if side == "x" else "x"
    return any(supports(candidate) for candidate in cell[other]["factors"])
