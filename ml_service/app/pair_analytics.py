"""Pure two-perspective scoring, aggregation and counterfactual modelling."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
from statistics import mean

from app.pair_schemas import AnalyticsScope
from app.pair_semantics import compare_texts, load_semantic_rules
from .pair_expert import action_matches, expert_policy

PIPELINE_VERSION = "pair-2.1.0"


def load_policy(mode: str | None = None) -> dict:
    if (mode or os.getenv("PULSE_PAIR_POLICY")) == "expert":
        return expert_policy()
    policy = json.loads((Path(__file__).resolve().parents[1] / "knowledge_base/v2/policy.json").read_text(encoding="utf-8"))
    try:
        return validate_policy(policy)
    except (KeyError, TypeError) as error:
        raise ValueError("invalid policy configuration structure") from error


def _finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def validate_policy(policy: dict) -> dict:
    if set(policy["roles"]) != {"mentor", "intern"}:
        raise ValueError("exact mentor and intern policy roles required")
    factors_by_role = policy["roles"]
    all_factors = set().union(*(set(factors) for factors in factors_by_role.values()))
    for factors in factors_by_role.values():
        if not factors or any(not re.fullmatch(r"[a-z][a-z0-9_]*", key) for key in factors):
            raise ValueError("invalid factor identifiers")
        if any(type(item["weight"]) is not int or item["weight"] < 0 or not isinstance(item["name"], str)
               or not item["name"].strip() for item in factors.values()):
            raise ValueError("invalid factor definition")
        if sum(item["weight"] for item in factors.values()) != 100:
            raise ValueError("each role must have total weight 100")
    if (set(policy["rating_levels"]) != {str(value) for value in range(1, 6)}
            or any(not _finite_number(value) or value not in (0, .5, 1) for value in policy["rating_levels"].values())):
        raise ValueError("rating mapping must specify 1..5 and finite supported severities")
    if (set(policy["thresholds"]) != {"yellow", "red"}
            or not all(_finite_number(value) for value in policy["thresholds"].values())
            or not 0 <= policy["thresholds"]["yellow"] < policy["thresholds"]["red"] <= 100):
        raise ValueError("invalid thresholds")
    if not _finite_number(policy["min_coverage"]) or not 0 <= policy["min_coverage"] <= 1:
        raise ValueError("invalid coverage")
    if not _finite_number(policy["rating_gap_threshold"]) or not 0 < policy["rating_gap_threshold"] <= 1:
        raise ValueError("invalid rating gap threshold")
    if set(policy["actions"]) != all_factors:
        raise ValueError("action references must cover exactly configured factors")
    for action in policy["actions"].values():
        if (action["responsible"] not in ("mentor", "intern", "hr", "team_lead")
                or any(not isinstance(action[field], str) or not action[field].strip()
                       for field in ("title", "completion", "resolution"))):
            raise ValueError("invalid action definition")
    expected_rules = {f"rule_{role}_{factor}" for role, factors in factors_by_role.items() for factor in factors}
    if set(policy["rules"]) != expected_rules:
        raise ValueError("rule references must cover configured role factors")
    for rule_id, rule in policy["rules"].items():
        if (rule["role"] not in factors_by_role or rule["factor_id"] not in factors_by_role[rule["role"]]
                or rule_id != f'rule_{rule["role"]}_{rule["factor_id"]}'
                or rule["action_factor"] != rule["factor_id"] or rule["action_factor"] not in policy["actions"]
                or rule["source_id"] not in policy["sources"]):
            raise ValueError("invalid rule factor, action or source reference")
    comparisons = policy["comparisons"]
    common_factors = set.intersection(*(set(factors) for factors in factors_by_role.values()))
    if (len(set(comparisons)) != len(comparisons) or not set(comparisons) <= common_factors
            or not set(comparisons) <= set(load_semantic_rules()["aspects"])):
        raise ValueError("invalid semantic comparison references")
    return policy


def band(score: float | None, coverage: float, policy: dict) -> str:
    if score is None or coverage < policy["min_coverage"]:
        return "gray"
    if score >= policy["thresholds"]["red"]:
        return "red"
    if score >= policy["thresholds"]["yellow"]:
        return "yellow"
    return "green"


def in_scope(pair_id: str, month: int, scope: dict) -> bool:
    return scope["type"] == "GLOBAL" or (
        pair_id == scope["pair_id"] and scope["start"] <= month <= scope["end"])


def _average(values):
    values = [value for value in values if value is not None]
    return mean(values) if values else None


def _quote(record: dict, question_id: str, quote: str, suffix: str = "") -> dict:
    identity = f'{record["source_id"]}:{record["version"]}:{question_id}:{suffix}'
    return {"id": hashlib.sha256(identity.encode()).hexdigest()[:20],
            "source_id": record["source_id"], "version": record["version"],
            "pair_id": record["pair_id"], "month": record["month"], "role": record["role"],
            "question_id": question_id, "quote": quote, "kind": "text", "value": None}


def _rating_evidence(record: dict, question_id: str, value: int) -> dict:
    evidence = _quote(record, question_id, "", "rating")
    evidence.update(quote=None, kind="rating", value=value, validation_status="deterministic_mapping",
                    rule_id=f'rule_{record["role"]}_{question_id}')
    return evidence


def _text_interpretation(record: dict) -> tuple[str, list[dict]]:
    status = record.get("text_analysis_status", "not_run")
    if status in ("error", "not_run", "missing", "empty"):
        return status, []
    texts = record["texts"]
    if not any(texts.values()):
        return "empty", []
    signals = record.get("signals", [])
    unmatched = []
    for question_id, answer in texts.items():
        for match in re.finditer(r"[^.!?;\n]+[.!?]?", answer or ""):
            quote = match.group().strip()
            if quote and not any(signal.get("question_id") == question_id and signal.get("quote")
                                 and (quote in signal["quote"] or signal["quote"] in quote) for signal in signals):
                unmatched.append(_quote(record, question_id, quote, f"unclassified-{match.start()}"))
    mode = record.get("nlp_audit", {}).get("mode")
    local = mode in ("local_rules", "local_encoder")
    if mode == "local_encoder" and signals and not any(signal.get("validation_status") == "accepted" for signal in signals):
        return "local_encoder_review", unmatched
    if not signals:
        return "local_abstain" if local else "abstained", unmatched
    if unmatched or any(signal.get("validation_status") != "accepted" for signal in signals):
        return "local_partial" if local else "partial", unmatched
    return "local_matched" if local else "matched", unmatched


def _history_summary(cells: list[dict], side: str, policy: dict) -> dict:
    timeline = []
    for month in range(1, 7):
        month_cells = [cell for cell in cells if cell["month"] == month]
        if side == "z":
            usable = [cell for cell in month_cells if cell["actual"]["z"] is not None]
            values = [cell["actual"]["z"] for cell in usable]
        else:
            usable = [cell for cell in month_cells if cell[side]["actual_score"] is not None
                      and cell[side]["coverage"] >= policy["min_coverage"]]
            values = [cell[side]["actual_score"] for cell in usable]
        relevant_sides = ("x", "y") if side == "z" else (side,)
        timeline.append({"month": month, "score": _average(values), "observation_count": len(values),
                         "pair_ids": sorted({cell["pair_id"] for cell in usable}),
                         "source_ids": sorted({cell[perspective]["source_id"] for cell in usable for perspective in relevant_sides}),
                         "evidence_refs": sorted({reference for cell in usable for perspective in relevant_sides
                                                  for factor in cell[perspective]["factors"] for reference in factor["score_evidence_refs"]})})
    available = [point for point in timeline if point["score"] is not None]
    first = available[0] if available else None
    last = available[-1] if available else None
    maximum = max(available, key=lambda point: point["score"]) if available else None
    change = last["score"] - first["score"] if len(available) >= 2 else None
    direction = "insufficient_data" if not available else "not_applicable" if len(available) == 1 else (
        "improvement" if change < 0 else "deterioration" if change > 0 else "stable")
    populations = {tuple(point["pair_ids"]) for point in available}
    return {"first": first, "last": last, "max": maximum, "timeline": timeline,
            "periods_available": len(available), "trend": {"direction": direction, "change": change,
            "population": "changing" if len(populations) > 1 else "fixed",
            "warning": "Состав пар в сравниваемых месяцах различается." if len(populations) > 1 else None},
            "population": "paired_observations" if side == "z" else "side_observations", "actual_only": True}


def analyze_side(record: dict | None, role: str, pair_id: str, month: int, policy: dict,
                 current_month: int = 6) -> dict:
    if record is None:
        return _analyze_side(record, role, pair_id, month, policy, current_month)
    return deepcopy(_cached_side(json.dumps(record, sort_keys=True, ensure_ascii=False), role, pair_id, month,
                                 json.dumps(policy, ensure_ascii=False), current_month))


@lru_cache(maxsize=1024)
def _cached_side(record_json: str, role: str, pair_id: str, month: int, policy_json: str, current_month: int) -> dict:
    return _analyze_side(json.loads(record_json), role, pair_id, month, json.loads(policy_json), current_month)


def _analyze_side(record: dict | None, role: str, pair_id: str, month: int, policy: dict,
                  current_month: int = 6) -> dict:
    if record is None:
        return {"score": None, "actual_score": None, "band": "gray", "coverage": 0,
                "status": "future" if month > current_month else "missing", "factors": [],
                "unknown_factors": list(policy["roles"][role]), "evidence": [],
                "critical_flags": [], "signals": [], "text_analysis_status": "missing",
                "text_interpretation_status": "missing", "unclassified_fragments": [],
                "text_mode": "missing", "nlp_identity": {}}
    factors, evidence = [], []
    signals = record.get("signals", [])
    for key, definition in policy["roles"][role].items():
        rating = record["ratings"].get(definition.get("wire_key", key))
        severity = policy["rating_levels"].get(str(rating))
        refs = [_rating_evidence(record, definition.get("wire_key", key), rating)] if rating is not None else []
        evidence_levels = {refs[0]["id"]: severity} if refs else {}
        conflicts, active_rules = [], set()
        for signal in signals:
            if signal.get("factor_id") != key:
                continue
            question_id = signal.get("question_id")
            quote = signal.get("quote", signal.get("evidence", ""))
            text = record["texts"].get(question_id) or ""
            if not quote or quote not in text:
                continue
            reference = _quote(record, question_id, quote, signal.get("signal_id", key))
            reference["validation_status"] = signal.get("validation_status", "review")
            reference["rule_id"] = signal.get("rule_id", f"rule_{role}_{key}")
            reference["temporal_status"] = signal.get("temporal_status")
            reference["polarity"] = signal.get("polarity")
            refs.append(reference)
            rule_id = signal.get("rule_id")
            valid_rule = (policy["rules"].get(rule_id, {}).get("factor_id") == key
                          if policy.get("expert") else rule_id == f"text.{role}.{key}")
            if not valid_rule or signal.get("role", role) != role:
                if signal.get("rule_id") != "local_review":
                    reference["validation_status"] = "rejected"
                continue
            if (policy.get("expert") and signal.get("validation_status") == "accepted"
                    and signal.get("semantic_type") == "ABSENCE" and signal.get("temporal_status") == "current"):
                severity = max(severity or 0, 0)
                evidence_levels[reference["id"]] = 0
            if (signal.get("validation_status") == "accepted"
                    and signal.get("temporal_status") == "current"
                    and signal.get("polarity") == "negative"):
                level = signal.get("severity")
                level = {"none": 0, "moderate": .5, "high": 1}.get(level, level)
                if type(level) in (float, int) and level in (0, .5, 1):
                    if level and policy.get("expert"):
                        active_rules.add(rule_id)
                    evidence_levels[reference["id"]] = level
                    severity = max(severity or 0, level)
                    if rating is not None and policy["rating_levels"][str(rating)] == 0 and level > 0:
                        conflicts.append("rating_text_conflict_requires_review")
        instance = f'{pair_id}:M{month}:{role}:{key}:v{record["version"]}:{record["source_id"]}'
        contribution = definition["weight"] * (severity or 0)
        factors.append({"id": key, "instance_id": instance, "name": definition["name"],
                        "weight": definition["weight"], "severity": severity,
                        "state": "unknown" if severity is None else "active" if severity else "observed_no_signal",
                        "contribution": contribution, "scenario_contribution": contribution,
                        "rating": rating, "rule_id": ", ".join(sorted(active_rules)) or (key + "_Q" if policy.get("expert") else f"rule_{role}_{key}"),
                        "activation_rule": "Оценки переводятся в выраженность по схеме " + ", ".join(f"{value} → {policy['rating_levels'][str(value)]:g}" for value in range(1, 6))
                        + ". Максимум подтверждённых текущих сигналов." + (" " + " ".join(
                            f'{rule}: {policy["rules"][rule]["activation"]["moderate"]} {policy["rules"][rule]["activation"]["high"]}'
                            for rule in sorted(active_rules)) if active_rules else ""),
                        "source": ", ".join(policy["sources"][source] for source in definition["source_ids"])
                            if policy.get("expert") else policy["source"], "rule_status": policy["status"],
                        "validation_status": "deterministic_mapping" if rating is not None else "text_validated" if severity is not None else "unknown",
                        "review_flags": sorted(set(conflicts)), "evidence": refs, "active_rules": sorted(active_rules),
                        "score_evidence_refs": sorted({reference["id"] for reference in refs
                            if reference["id"] in evidence_levels and evidence_levels[reference["id"]] == severity})})
        evidence.extend(refs)
    for question_id, text in record["texts"].items():
        if text:
            evidence.append(_quote(record, question_id, text))
    coverage = sum(factor["weight"] for factor in factors if factor["severity"] is not None) / 100
    score = sum(factor["contribution"] for factor in factors) if coverage else None
    critical = []
    if role == "intern":
        from app.pulse_nlp import EXIT
        critical = [signal.get("signal_id", "direct_exit_requires_confidential_review") for signal in signals
                    if signal.get("critical") and signal.get("validation_status") in ("accepted", "review")
                    and signal.get("temporal_status") == "current"
                    and signal.get("quote") and signal["quote"] in (record["texts"].get(signal.get("question_id")) or "")
                    and EXIT.fullmatch(signal.get("quote", ""))]
    if policy.get("expert"):
        critical = sorted({s["factor_id"] for s in signals if s.get("critical")
                           and s.get("validation_status") in ("accepted", "review")
                           and s.get("temporal_status") == "current"
                           and s.get("quote") and s["quote"] in (record["texts"].get(s.get("question_id")) or "")})
    failed = policy.get("expert") and record.get("text_analysis_status") == "error"
    text_interpretation, unmatched = _text_interpretation(record)
    return {"score": score, "actual_score": score, "band": "gray" if failed else band(score, coverage, policy),
            "coverage": coverage, "status": "insufficient_data" if coverage < policy["min_coverage"] else
            "partial" if record.get("text_analysis_status") in ("error", "not_run", "review") or critical
            or text_interpretation in ("local_abstain", "abstained", "local_partial", "partial", "local_encoder_review")
            or any(factor["review_flags"] for factor in factors) else "completed",
            "factors": factors, "unknown_factors": [factor["id"] for factor in factors if factor["severity"] is None],
            "evidence": evidence, "critical_flags": critical, "signals": signals,
            "source_id": record["source_id"], "version": record["version"],
            "text_analysis_status": record.get("text_analysis_status", "not_run"),
            "text_interpretation_status": text_interpretation, "unclassified_fragments": unmatched,
            "text_mode": record.get("nlp_audit", {}).get("mode", "not_run"),
            "nlp_identity": {"version": record.get("nlp_version"), "mode": record.get("nlp_audit", {}).get("mode", "not_run"),
                             "pipeline": record.get("nlp_audit", {}).get("pipeline"),
                             "config_hash": record.get("nlp_audit", {}).get("config_hash"),
                             "encoder_versions": sorted({signal["encoder_version"] for signal in signals if signal.get("encoder_version")})},
            "nlp_audit": record.get("nlp_audit", {})}


def aggregate(cells: list[dict], policy: dict) -> dict:
    eligible = [cell for cell in cells if cell["z"] is not None]
    x_all = [cell["x"]["score"] for cell in cells if cell["x"]["coverage"] >= policy["min_coverage"]
             and not (policy.get("expert") and cell["x"]["text_analysis_status"] == "error")]
    y_all = [cell["y"]["score"] for cell in cells if cell["y"]["coverage"] >= policy["min_coverage"]
             and not (policy.get("expert") and cell["y"]["text_analysis_status"] == "error")]
    x = _average([cell["x"]["score"] for cell in eligible])
    y = _average([cell["y"]["score"] for cell in eligible])
    z = (x + y) / 2 if x is not None and y is not None else None
    if not eligible and len(cells) == 1:
        x, y = cells[0]["x"]["score"], cells[0]["y"]["score"]
    actual_x = _average([cell["x"]["actual_score"] for cell in eligible])
    actual_y = _average([cell["y"]["actual_score"] for cell in eligible])
    actual_z = (actual_x + actual_y) / 2 if actual_x is not None and actual_y is not None else None
    if not eligible and len(cells) == 1:
        actual_x, actual_y = cells[0]["x"]["actual_score"], cells[0]["y"]["actual_score"]
    coverage = _average([(cell["x"]["coverage"] + cell["y"]["coverage"]) / 2 for cell in cells]) or 0
    factor_cells = eligible or (cells if len(cells) == 1 else [])
    factor_contributions = {}
    for side in ("x", "y"):
        contributions = {}
        for cell in factor_cells:
            for factor in cell[side]["factors"]:
                entry = contributions.setdefault(factor["id"], {"id": factor["id"], "name": factor.get("name", factor["id"]),
                    "contribution": 0, "scenario_contribution": 0, "known_count": 0, "instance_ids": []})
                entry["contribution"] += factor["contribution"] / len(factor_cells)
                entry["scenario_contribution"] += factor["scenario_contribution"] / len(factor_cells)
                entry["known_count"] += factor.get("severity") is not None
                entry["instance_ids"].append(factor["instance_id"])
        factor_contributions[side] = list(contributions.values())
    return {"x": x, "y": y, "z": z, "actual": {"x": actual_x, "y": actual_y, "z": actual_z},
            "scenario": {"x": x, "y": y, "z": z}, "bands": {
                side: band(value, 1, policy) if eligible else cells[0][side]["band"] if len(cells) == 1 else "gray"
                for side, value in (("x", x), ("y", y))},
            "coverage": coverage, "observation_count": len(cells), "eligible_count": len(eligible),
            "observations": [{"pair_id": cell["pair_id"], "month": cell["month"]} for cell in eligible],
            "side_statistics": {"mentor": {"mean": _average(x_all), "count": len(x_all)},
                                "intern": {"mean": _average(y_all), "count": len(y_all)}},
            "received_surveys": sum(cell[side]["status"] not in ("missing", "future") for cell in cells for side in ("x", "y")),
            "expected_surveys": len(cells) * 2,
            "risk_distribution": {color: sum(band(cell["z"], 1, policy) == color for cell in cells) for color in ("green", "yellow", "red", "gray")},
            "factor_contributions": factor_contributions}


def _action_applies(cell: dict, role: str, factor: dict, selection: dict) -> bool:
    if factor["contribution"] <= 0 or not in_scope(cell["pair_id"], cell["month"], selection["scope"]):
        return False
    if selection["action_id"] == f'action_{role}_{factor["id"]}':
        return True
    return selection["action_id"] == f'action_pair_{factor["id"]}' and all(
        any(other["id"] == factor["id"] and other["contribution"] > 0 for other in cell[side]["factors"])
        for side in ("x", "y"))


def apply_scenario(cells: list[dict], selected: list[dict], policy: dict) -> list[dict]:
    result = deepcopy(cells)
    for cell in result:
        for role, side in (("mentor", "x"), ("intern", "y")):
            for factor in cell[side]["factors"]:
                if policy.get("expert"):
                    rules = set(factor["active_rules"])
                    covered = {rule for selection in selected
                               if in_scope(cell["pair_id"], cell["month"], selection["scope"])
                               and (action := policy["actions"].get(selection["action_id"]))
                               and action_matches(cell, side, factor, action) for rule in action["target_rules"]}
                    rating_resolved = any(selection["action_id"] == f'rating_{factor["id"]}'
                                          and in_scope(cell["pair_id"], cell["month"], selection["scope"])
                                          and factor["rating"] in (1, 2) and not rules for selection in selected)
                    resolved = rating_resolved or bool(rules) and rules <= covered and (
                        not factor["rating"] or policy["rating_levels"][str(factor["rating"])] <= max(
                            ({"moderate": .5, "high": 1}.get(signal["severity"], 0)
                             for signal in cell[side]["signals"] if signal.get("rule_id") in rules
                             and signal.get("validation_status") == "accepted" and signal.get("polarity") == "negative"
                             and signal.get("temporal_status") == "current"), default=0))
                else:
                    resolved = any(_action_applies(cell, role, factor, selection) for selection in selected)
                if resolved:
                    factor["scenario_contribution"] = 0
            if cell[side]["score"] is not None:
                cell[side]["score"] = sum(factor["scenario_contribution"] for factor in cell[side]["factors"])
            if not (policy.get("expert") and cell[side]["text_analysis_status"] == "error"):
                cell[side]["band"] = band(cell[side]["score"], cell[side]["coverage"], policy)
        cell["z"] = (cell["x"]["score"] + cell["y"]["score"]) / 2 if cell["eligible"] else None
        cell["scenario"] = {"x": cell["x"]["score"], "y": cell["y"]["score"], "z": cell["z"]}
    return result


def recommendations(cells: list[dict], scope: dict, policy: dict) -> list[dict]:
    result = []
    actual_cells = deepcopy(cells)
    for cell in actual_cells:
        for side in ("x", "y"):
            cell[side]["score"] = cell[side]["actual_score"]
            for factor in cell[side]["factors"]:
                factor["scenario_contribution"] = factor["contribution"]
        cell["z"] = cell["actual"]["z"]
    scope_key = hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()[:12]
    if policy.get("expert"):
        return expert_recommendations(cells, actual_cells, scope, scope_key, policy)
    for key, action in policy["actions"].items():
        targets = [(cell, role, side, factor) for cell in cells
                   for role, side in (("mentor", "x"), ("intern", "y"))
                   for factor in cell[side]["factors"] if factor["id"] == key and factor["contribution"] > 0]
        if not targets:
            continue
        joint = {(cell["pair_id"], cell["month"]) for cell, _, _, _ in targets
                 if all(any(factor["id"] == key and factor["contribution"] > 0 for factor in cell[side]["factors"])
                        for side in ("x", "y"))}
        grouped = {"PAIR": [target for target in targets if (target[0]["pair_id"], target[0]["month"]) in joint],
                   "MENTOR": [target for target in targets if target[1] == "mentor"],
                   "INTERN": [target for target in targets if target[1] == "intern"]}
        for impact, group in grouped.items():
            if not group:
                continue
            action_id = f"action_{impact.lower()}_{key}"
            simulated = apply_scenario(cells, [{"action_id": action_id, "scope": scope}], policy)
            current, after = aggregate(cells, policy), aggregate(simulated, policy)
            independent = aggregate(apply_scenario(actual_cells, [{"action_id": action_id, "scope": scope}], policy), policy)
            delta = {side: current["actual"][side] - independent[side] if current["actual"][side] is not None and independent[side] is not None else None for side in ("x", "y", "z")}
            additional = {side: current[side] - after[side] if current[side] is not None and after[side] is not None else None for side in ("x", "y", "z")}
            result.append({"id": f"{action_id}_{scope_key}", "action_id": action_id, "scope": scope,
                       "impact_scope": impact, "title": action["title"], "name": action["title"],
                       "rationale": "Подтверждённые активные факторы выбранной области",
                       "responsible": action["responsible"], "target_factors": [key],
                       "targets": [factor["instance_id"] for _, _, _, factor in group],
                       "target_observations": [{"pair_id": cell["pair_id"], "month": cell["month"], "role": role} for cell, role, _, _ in group],
                       "evidence_refs": sorted({reference for _, _, _, factor in group for reference in factor["score_evidence_refs"]}),
                       "delta": delta, "additional_delta": additional,
                       "completion_criteria": action["completion"], "resolution_criteria": action["resolution"],
                       "status": policy["status"], "assumption": "full_resolution"})
    return result


def expert_recommendations(cells, actual_cells, scope, scope_key, policy):
    current = aggregate(cells, policy)
    actions = list(policy["actions"].values())
    actions.extend(rating_actions(policy).values())
    sides = [cell[side] for cell in cells for side in ("x", "y")]
    topics = {s["factor_id"] for side in sides for s in side["signals"]
              if s.get("validation_status") in ("accepted", "review")}
    zero_conditions = {
        "N01": any(s["status"] == "partial" for s in sides),
        "N02": "AD_RESOURCE" in topics,
        "N03": "DEVELOPMENT_REQUEST" in topics,
        "N04": any(s["critical_flags"] for s in sides),
        "N05": any(s["status"] in ("missing", "insufficient_data") for s in sides),
        "N06": bool({"AD_EXPECTATION", "AD_FUTURE"} & topics),
    }
    actions += [action for key, action in policy["unscored"].items() if zero_conditions.get(key)]
    results = []
    for action in actions:
        action_id = action["id"]
        informational = action_id in policy["unscored"]
        group = [(cell, role, factor) for cell in cells for role, side in (("mentor", "x"), ("intern", "y"))
                 for factor in cell[side]["factors"] if not informational and action_matches(cell, side, factor, action)]
        if not informational and not group:
            continue
        selection = {"action_id": action_id, "scope": scope}
        after = aggregate(apply_scenario(cells, [selection], policy), policy) if group else current
        independent = aggregate(apply_scenario(actual_cells, [selection], policy), policy) if group else current["actual"]
        results.append({
            "id": f"{action_id}_{scope_key}", "action_id": action_id, "scope": scope,
            "impact_scope": action.get("scope", "PAIR"), "title": action["name"], "name": action["name"],
            "rationale": action.get("eligibility", "Уточнение с участием HR"),
            "responsible": " · ".join(action.get("responsible", ["hr"])),
            "target_factors": sorted({factor["id"] for _, _, factor in group}),
            "targets": [factor["instance_id"] for _, _, factor in group],
            "target_observations": [{"pair_id": cell["pair_id"], "month": cell["month"], "role": role}
                                    for cell, role, _ in group],
            "evidence_refs": sorted({ref for _, _, factor in group for ref in factor["score_evidence_refs"]}),
            "delta": {k: current["actual"][k] - independent[k] if independent[k] is not None and current["actual"][k] is not None else None
                      for k in ("x", "y", "z")},
            "additional_delta": {k: current[k] - after[k] if current[k] is not None and after[k] is not None else None
                                 for k in ("x", "y", "z")},
            "completion_criteria": " ".join(action.get("completion_criteria", [action["name"]])),
            "resolution_criteria": " ".join(action.get("resolution_criteria", ["Эффект на индекс отсутствует."])),
            "status": policy["status"], "assumption": "no_score_change" if informational else "full_resolution",
        })
    return results


def rating_actions(policy):
    return {f"rating_{key}": {
        "id": f"rating_{key}", "scope": role.upper(), "rating_resolution": True,
        "name": f"Условно устранить фактор «{factor['name'].lower()}»",
        "target_factors": [key], "target_rules": [], "responsible": ["hr", role],
        "eligibility": "Низкая оценка без подтверждённого текстового механизма. Причину нужно уточнить.",
        "completion_criteria": ["Уточнить причину отдельно с респондентом."],
        "resolution_criteria": ["Включение в сценарий предполагает полное устранение этого фактора."],
    } for role, factors in policy["roles"].items() for key, factor in factors.items()}


def expert_comparisons(cell, policy):
    results = []
    for relationship in policy["ontology"]["relationships"]:
        factor_ids = relationship["mentor_factor"], relationship["intern_factor"]
        observations = []
        for side, factor_id in zip(("x", "y"), factor_ids, strict=True):
            factor = next((f for f in cell[side]["factors"] if f["id"] == factor_id), None)
            observations.append([q for q in factor["evidence"] if q["kind"] == "text"
                                 and q["validation_status"] == "accepted"
                                 and q["temporal_status"] == "current"
                                 and q["polarity"] in ("positive", "negative")] if factor else [])
        if not all(observations):
            continue
        mentor, intern = ({q["polarity"] for q in obs} for obs in observations)
        if relationship["type"] == "comparable" and len(mentor) == len(intern) == 1:
            kind = "AGREEMENT" if mentor == intern else "CONTRADICTION"
        elif relationship["type"] == "comparable":
            kind = "NOT_COMPARABLE"
        else:
            kind = "COMPLEMENTARY"
        evidence = observations[0] + observations[1]
        results.append({
            "pair_id": cell["pair_id"], "month": cell["month"], "aspect": "/".join(factor_ids),
            "type": kind, "status": "requires_clarification" if kind in ("CONTRADICTION", "NOT_COMPARABLE")
            else "project_rule_provisional", "mentor_states": sorted(mentor), "intern_states": sorted(intern),
            "explanation": relationship["note"], "evidence": evidence,
            "evidence_refs": [q["id"] for q in evidence], "rule_version": policy["version"], "score_effect": 0,
        })
    return results


def analyze(records: list[dict], scope: dict | None = None, selected: list[dict] | None = None,
            current_month: int = 6, policy: dict | None = None, pairs: list[dict] | None = None) -> dict:
    policy = policy or load_policy()
    scope = AnalyticsScope.model_validate(scope or {}).model_dump()
    selected = selected or []
    pairs = {pair["pair_id"]: pair for pair in (pairs or [])} | {record["pair_id"]: {key: record[key] for key in ("pair_id", "mentor_id", "intern_id", "department")} for record in records}
    if scope["type"] != "GLOBAL" and scope["pair_id"] not in pairs:
        raise KeyError("pair_not_found")
    allowed_actions = {f"action_pair_{factor}" for factor in policy["actions"]}
    allowed_actions.update(f"action_{role}_{factor}" for role, factors in policy["roles"].items() for factor in factors)
    if policy.get("expert"):
        available_actions = policy["actions"] | rating_actions(policy)
        allowed_actions = available_actions.keys()
    if any(selection["action_id"] not in allowed_actions for selection in selected):
        raise KeyError("action_not_found")
    if any(selection["scope"]["type"] != "GLOBAL" and selection["scope"]["pair_id"] not in pairs for selection in selected):
        raise KeyError("scenario_pair_not_found")
    by_key = {(record["pair_id"], record["month"], record["role"]): record for record in records}
    cells = []
    for pair_id in sorted(pairs):
        for month in range(1, 7):
            x = analyze_side(by_key.get((pair_id, month, "mentor")), "mentor", pair_id, month, policy, current_month)
            y = analyze_side(by_key.get((pair_id, month, "intern")), "intern", pair_id, month, policy, current_month)
            eligible = all(side["score"] is not None and side["coverage"] >= policy["min_coverage"]
                           and not (policy.get("expert") and side["text_analysis_status"] == "error") for side in (x, y))
            z = (x["score"] + y["score"]) / 2 if eligible else None
            cells.append({"pair_id": pair_id, "month": month, "x": x, "y": y, "z": z,
                          "eligible": eligible, "actual": {"x": x["score"], "y": y["score"], "z": z}})
    for selection in selected:
        if not any(in_scope(cell["pair_id"], cell["month"], selection["scope"]) and (
                   action_matches(cell, side, factor, available_actions[selection["action_id"]])
                   if policy.get("expert") else _action_applies(cell, role, factor, selection))
                   for cell in cells for role, side in (("mentor", "x"), ("intern", "y")) for factor in cell[side]["factors"]):
            raise ValueError("scenario_action_has_no_active_targets")
    cells = apply_scenario(cells, selected, policy)
    cell_lookup = {(cell["pair_id"], cell["month"]): cell for cell in cells}
    chosen = [cell for cell in cells if in_scope(cell["pair_id"], cell["month"], scope)]
    recs = recommendations(chosen, scope, policy)
    semantic_rules = load_semantic_rules()
    semantic_comparisons = [comparison for cell in chosen for comparison in (
        expert_comparisons(cell, policy) if policy.get("expert") else compare_texts(
        by_key.get((cell["pair_id"], cell["month"], "mentor")),
        by_key.get((cell["pair_id"], cell["month"], "intern")), cell["pair_id"], cell["month"],
        policy["comparisons"], semantic_rules))]
    sections = {}
    for key, title, sides in (("pair", "Взаимодействие пары", ("x", "y")),
                              ("mentor", "Наставник", ("x",)), ("intern", "Стажёр", ("y",))):
        evidence = [quote for cell in chosen for side in sides for quote in cell[side]["evidence"]]
        active = [(cell, side, factor) for cell in chosen for side in sides for factor in cell[side]["factors"] if factor["contribution"] > 0]
        names = list(dict.fromkeys(factor["name"] for _, _, factor in active))
        coverage = _average([cell[side]["coverage"] for cell in chosen for side in sides]) or 0
        review_flags = sorted({flag for cell in chosen for side in sides for factor in cell[side]["factors"] for flag in factor["review_flags"]})
        claims = [{"text": f'М{cell["month"]}, {factor["name"]} — '
                   + (f'оценка {factor["rating"]}/5, ' if factor["rating"] is not None else 'подтверждённый текстовый сигнал, ')
                   + f'вклад {factor["contribution"]:g} балла.',
                   "pair_id": cell["pair_id"], "month": cell["month"], "source_id": cell[side].get("source_id"),
                   "evidence_refs": factor["score_evidence_refs"],
                   "factor_instance_id": factor["instance_id"]} for cell, side, factor in active]
        characteristic = ("Области внимания по сохранённым ответам — " + ", ".join(names) + ".") if names else (
            "По доступным оценкам активных проблемных факторов не выявлено." if coverage >= policy["min_coverage"]
            else "Информации недостаточно для характеристики ситуации.")
        if coverage < policy["min_coverage"]:
            characteristic += f" Покрытие данных {coverage:.0%}. Неизвестные факторы требуют уточнения."
        if review_flags:
            characteristic += " Есть расхождение текста и оценки, требующее уточнения."
        history_summary = _history_summary(chosen, {"pair": "z", "mentor": "x", "intern": "y"}[key], policy)
        if history_summary["first"]:
            first, last, maximum = (history_summary[name] for name in ("first", "last", "max"))
            if history_summary["periods_available"] > 1:
                characteristic += (f' Фактический индекс М{first["month"]} → М{last["month"]}: '
                                   f'{first["score"]:g} → {last["score"]:g} балла. '
                                   f'Максимум {maximum["score"]:g} в М{maximum["month"]}.')
            else:
                characteristic += f' Фактический индекс М{first["month"]}: {first["score"]:g} балла.'
            if history_summary["trend"]["warning"]:
                characteristic += " " + history_summary["trend"]["warning"]
        interpretation_counts = {}
        for cell in chosen:
            for side in sides:
                status = cell[side]["text_interpretation_status"]
                interpretation_counts[status] = interpretation_counts.get(status, 0) + 1
        text_interpretation = {"status_counts": interpretation_counts,
            "text_modes": sorted({cell[side]["text_mode"] for cell in chosen for side in sides}),
            "unclassified_count": sum(len(cell[side]["unclassified_fragments"]) for cell in chosen for side in sides),
            "rule_limitations": "Локальные правила выделяют явно заданные формулировки. Остальные фрагменты сохраняются для ручного просмотра."}
        supported_quotes = [(factor["contribution"], cell["month"], quote) for cell, side, factor in active
                            for quote in factor["evidence"] if quote["kind"] == "text"
                            and quote["id"] in factor["score_evidence_refs"] and quote.get("validation_status") == "accepted"]
        if not supported_quotes:
            supported_quotes = [(0, cell["month"], quote) for cell in chosen for side in sides
                                for factor in cell[side]["factors"] for quote in factor["evidence"]
                                if quote["kind"] == "text" and quote.get("validation_status") == "accepted"
                                and quote.get("temporal_status") in ("current", "resolved")
                                and quote.get("polarity") == "positive"]
        highlight = max(supported_quotes, key=lambda item: (item[0], item[1], item[2]["id"]))[2] if supported_quotes else None
        if highlight and len(highlight["quote"]) <= 240:
            label = "Наставник" if highlight["role"] == "mentor" else "Стажёр"
            characteristic += f' «{highlight["quote"]}» — {label}, М{highlight["month"]}.'
        source_facets = {"source_ids": sorted({cell[side]["source_id"] for cell in chosen for side in sides if cell[side].get("source_id")}),
                         "months": sorted({cell["month"] for cell in chosen if any(cell[side].get("source_id") for side in sides)}),
                         "text_modes": text_interpretation["text_modes"],
                         "roles": ["mentor" if side == "x" else "intern" for side in sides]}
        sections[key] = {"title": title, "characteristic": characteristic,
                         "generation": "deterministic_evidence_summary", "claims": claims,
                         "evidence": evidence, "recommendations": [rec for rec in recs if rec["impact_scope"] == key.upper()],
                         "critical_flags": [flag for cell in chosen for side in sides for flag in cell[side]["critical_flags"]],
                         "coverage": coverage, "review_flags": review_flags,
                         "history_summary": history_summary, "text_interpretation": text_interpretation}
        sections[key].update(highlighted_evidence=highlight, characteristic_evidence_refs=[highlight["id"]] if highlight else [],
                             source_facets=source_facets)
    discrepancies = []
    for cell in chosen:
        for mentor_id, intern_id in policy.get("numeric_pairs", [(key, key) for key in policy["comparisons"]]):
            mentor = next((factor for factor in cell["x"]["factors"] if factor["id"] == mentor_id), None)
            intern = next((factor for factor in cell["y"]["factors"] if factor["id"] == intern_id), None)
            if not mentor or not intern or mentor["rating"] is None or intern["rating"] is None:
                continue
            gap = (mentor["rating"] - intern["rating"]) / 4
            if abs(gap) >= policy["rating_gap_threshold"]:
                discrepancies.append({"pair_id": cell["pair_id"], "month": cell["month"], "aspect": mentor_id,
                                      "mentor_rating": mentor["rating"], "intern_rating": intern["rating"],
                                      "normalized_gap": abs(gap), "direction": "mentor_higher" if gap > 0 else "intern_higher",
                                      "type": "rating_gap", "status": "requires_clarification",
                                      "explanation": "Расхождение сопоставимых оценок с учётом разных перспектив. Баллы индекса не изменяются.",
                                      "evidence": mentor["evidence"] + intern["evidence"]})
    dynamics, patterns = [], []
    for pair_id in sorted(pairs):
        if scope["type"] != "GLOBAL" and scope["pair_id"] != pair_id:
            continue
        for role in ("mentor", "intern"):
            for factor_id, definition in policy["roles"][role].items():
                values = [by_key.get((pair_id, month, role), {}).get("ratings", {}).get(definition.get("wire_key", factor_id))
                          for month in range(1, 7)]
                changes = [{"month": month + 1, "from": values[month - 1], "to": values[month],
                            "direction": "improvement" if values[month] > values[month - 1] else "deterioration"}
                           for month in range(1, 6) if values[month] is not None and values[month - 1] is not None
                           and abs(values[month] - values[month - 1]) >= 1]
                dynamics.append({"pair_id": pair_id, "role": role, "factor_id": factor_id, "name": definition["name"], "values": values, "changes": changes, "scale": [1, 5]})
                side_key = "x" if role == "mentor" else "y"
                factor_history = {month: next((factor for factor in cell_lookup[(pair_id, month)][side_key]["factors"]
                                              if factor["id"] == factor_id), None) for month in range(1, 7)}
                active_months = [month for month, factor in factor_history.items() if factor and factor["contribution"] > 0
                                 and in_scope(pair_id, month, scope)]
                if len(active_months) >= 2:
                    patterns.append({"pair_id": pair_id, "role": role, "factor_id": factor_id, "type": "recurring_problem", "months": active_months,
                                     "status": "validated_observations", "explanation": "Фактор повторяется в подтверждённых наблюдениях. Дополнительные баллы не начисляются."})
                for change in changes:
                    if change["from"] <= 3 < change["to"] and in_scope(pair_id, change["month"], scope):
                        patterns.append({"pair_id": pair_id, "role": role, "factor_id": factor_id, "month": change["month"],
                                         "type": "rating_improvement", "status": "requires_text_confirmation",
                                         "explanation": "Оценка улучшилась. Устранение текстовой проблемы требует отдельного подтверждения."})
                for month in range(2, 7):
                    if not in_scope(pair_id, month, scope):
                        continue
                    previous = by_key.get((pair_id, month - 1, role))
                    current = by_key.get((pair_id, month, role))
                    if not current:
                        continue
                    previous_factor, current_factor = factor_history[month - 1], factor_history[month]
                    if (previous_factor and current_factor and previous_factor["severity"] == 0
                            and current_factor["contribution"] > 0):
                        patterns.append({"pair_id": pair_id, "role": role, "factor_id": factor_id, "month": month,
                                         "type": "new_problem", "status": "validated_observations",
                                         "explanation": "После непроблемного наблюдения обнаружен активный фактор по тому же показателю."})
                for month in range(1, 7):
                    if not in_scope(pair_id, month, scope):
                        continue
                    current = by_key.get((pair_id, month, role))
                    if not current:
                        continue
                    for signal in current.get("signals", []):
                        quote = signal.get("quote")
                        if (signal.get("factor_id") == factor_id and signal.get("validation_status") == "accepted"
                                and signal.get("temporal_status") == "resolved" and signal.get("polarity") == "positive"
                                and signal.get("rule_id") == f"text.{role}.{factor_id}" and quote
                                and quote in (current["texts"].get(signal.get("question_id")) or "")):
                            conflicting = factor_history[month] and factor_history[month]["contribution"] > 0
                            patterns.append({"pair_id": pair_id, "role": role, "factor_id": factor_id, "month": month,
                                             "type": "resolution_report" if conflicting else "confirmed_resolution",
                                             "status": "requires_clarification" if conflicting else "explicit_report",
                                             "explanation": "Респондент прямо сообщил об устранении проблемы. Прежние наблюдения сохранены.",
                                             "evidence": [_quote(current, signal["question_id"], quote, signal.get("signal_id", factor_id))]})
    versions = {"knowledge": policy["version"], "pipeline": PIPELINE_VERSION, "mapping": "mgts-official-v2", "prompt": "pair-signals-v2",
                "semantic_rules": semantic_rules["version"]}
    nlp_identities = {json.dumps(cell[side]["nlp_identity"], sort_keys=True) for cell in cells for side in ("x", "y") if cell[side]["nlp_identity"]}
    versions["nlp_identities"] = [json.loads(identity) for identity in sorted(nlp_identities)]
    version_payload = {"records": sorted(records, key=lambda record: (record["pair_id"], record["month"], record["role"], record["source_id"], record["version"])),
                       "pairs": pairs, "current_month": current_month,
                       "versions": versions, "policy": policy, "semantic_rules": semantic_rules}
    analysis_version = hashlib.sha256(json.dumps(version_payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    updated = max((record.get("analyzed_at") or record.get("received_at") or "" for record in records if in_scope(record["pair_id"], record["month"], scope)), default="") or None
    warnings = sorted({f'text_analysis_{record.get("text_analysis_status", "not_run")}' for record in records if record.get("text_analysis_status") not in ("completed", "empty")})
    warnings = sorted(set(warnings) | {f'text_interpretation_{cell[side]["text_interpretation_status"]}'
        for cell in chosen for side in ("x", "y")
        if cell[side]["text_interpretation_status"] in ("local_abstain", "abstained", "local_partial", "partial", "local_encoder_review")})
    index_dynamics = [{"month": month, **aggregate([cell for cell in chosen if cell["month"] == month], policy)} for month in range(1, 7)]
    top_factors = {}
    for cell in chosen:
        for role, side in (("mentor", "x"), ("intern", "y")):
            for factor in cell[side]["factors"]:
                if factor["contribution"] <= 0:
                    continue
                item = top_factors.setdefault((role, factor["id"]), {"role": role, "factor_id": factor["id"],
                    "name": factor["name"], "observation_count": 0, "contribution_sum": 0})
                item["observation_count"] += 1
                item["contribution_sum"] += factor["contribution"]
    summary = {"pair_count": len({cell["pair_id"] for cell in chosen}),
               "top_factors": sorted(top_factors.values(), key=lambda item: (-item["observation_count"], -item["contribution_sum"], item["role"], item["factor_id"])),
               "critical_cases": [{"pair_id": cell["pair_id"], "month": cell["month"],
                                   "flags": sorted(set(cell["x"]["critical_flags"] + cell["y"]["critical_flags"]))}
                                  for cell in chosen if cell["x"]["critical_flags"] or cell["y"]["critical_flags"]],
               "discrepancy_count": len(discrepancies), "recurring_problem_count": sum(item["type"] == "recurring_problem" for item in patterns),
               "aggregation": "equal_weight_eligible_pair_month", "scenario_assumption": "full_resolution",
               "retrospective": bool(selected),
               "formula": {"x": "sum(weight_mentor * max(validated_severity))", "y": "sum(weight_intern * max(validated_severity))", "z": "(x+y)/2"}}
    meaningful_comparisons = [comparison for comparison in semantic_comparisons if comparison["type"] != "NOT_COMPARABLE"]
    summary["semantic_comparison_counts"] = {kind: sum(comparison["type"] == kind for comparison in semantic_comparisons)
        for kind in ("AGREEMENT", "CONTRADICTION", "AWARENESS_GAP", "COMPLEMENTARY", "NOT_COMPARABLE")}
    summary["trend"] = sections["pair"]["history_summary"]
    pair_summary = (f'В выбранной области {len(chosen)} наблюдений «пара × месяц». '
                    f'Сопоставимые оценки расходятся в {len(discrepancies)} случаях. '
                    f'Явные текстовые сопоставления доступны в {len(meaningful_comparisons)} случаях.')
    if patterns:
        pair_summary += (f' Повторяющихся факторов: {summary["recurring_problem_count"]}. '
                         f'Прямых сообщений об устранении: {sum(pattern["type"] == "confirmed_resolution" for pattern in patterns)}.')
    summary["pair_summary"] = pair_summary
    sections["pair"]["characteristic"] += " " + pair_summary
    return {"analysis_version": analysis_version, "methodology": {key: policy[key] for key in ("version", "status", "source", "min_coverage", "thresholds")},
            "scope": scope, "updated_at": updated,
            "matrix": [{**pairs[pair_id], "months": [cell for cell in cells if cell["pair_id"] == pair_id]} for pair_id in sorted(pairs)],
            "indices": aggregate(chosen, policy), "sections": sections, "recommendations": recs,
            "dynamics": dynamics, "discrepancies": discrepancies, "patterns": patterns, "warnings": warnings, "versions": versions,
            "summary": summary, "index_dynamics": index_dynamics, "semantic_comparisons": semantic_comparisons}
