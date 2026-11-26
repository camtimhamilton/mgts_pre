"""Adapt the supplied expert pack to the existing pair analytics and survey wire keys."""
from functools import lru_cache
from pathlib import Path

WIRE = {
    "mentor": ("clarity", "quality", "autonomy", "deadlines", "initiative", "team"),
    "intern": ("clarity", "support", "workload", "interest", "growth", "team", "satisfaction"),
}


@lru_cache
def expert_policy():
    import yaml

    root = Path(__file__).resolve().parents[1] / "knowledge_base/pulse_kb_v1"

    def read(name):
        return yaml.safe_load((root / f"{name}.yaml").read_text(encoding="utf-8"))

    scoring = read("scoring_policy")
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
    actions = {a["id"]: a for a in action_config["actions"]}
    if any(rule_id not in rules for a in actions.values() for rule_id in a["target_rules"]):
        raise ValueError("unknown expert action rule")
    ontology = read("shared_ontology")
    critical = read("critical_flags")
    sources = {s["id"]: s["title"] for s in read("sources")["sources"]}
    zones = {zone["id"]: zone["min_inclusive"] for zone in scoring["zones"] if "min_inclusive" in zone}
    return {
        "expert": True, "version": scoring["version"], "status": scoring["status"],
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


def action_matches(cell, side, factor, action):
    if factor["id"] not in action["target_factors"] or not factor["contribution"]:
        return False
    if action.get("rating_resolution"):
        return factor["rating"] in (1, 2) and not factor["active_rules"]
    if action["scope"] != "PAIR":
        return action["scope"] == ("MENTOR" if side == "x" else "INTERN") and bool(
            set(factor["active_rules"]) & set(action["target_rules"]))
    return all(any(f["contribution"] and f["id"] in action["target_factors"]
                   and set(f["active_rules"]) & set(action["target_rules"])
                   for f in cell[key]["factors"]) for key in ("x", "y")) and bool(
        set(factor["active_rules"]) & set(action["target_rules"]))
