"""Validate invariants of remote analytics before committing a dashboard snapshot."""
import math
from statistics import mean


def equal(actual, expected):
    if expected is None:
        if actual is not None:
            raise ValueError("expected unavailable value")
    elif actual is None or type(actual) not in (int, float) or not math.isclose(actual, expected, abs_tol=1e-8):
        raise ValueError("inconsistent numeric value")


def in_scope(cell, scope):
    return scope["type"] == "GLOBAL" or (cell["pair_id"] == scope["pair_id"] and scope["start"] <= cell["month"] <= scope["end"])


def validate_aggregate(indices, cells):
    eligible = [cell for cell in cells if cell["eligible"]]
    equal(indices["observation_count"], len(cells))
    equal(indices["eligible_count"], len(eligible))
    expected_refs = {(cell["pair_id"], cell["month"]) for cell in eligible}
    refs = [(ref["pair_id"], ref["month"]) for ref in indices["observations"]]
    if set(refs) != expected_refs or len(refs) != len(expected_refs):
        raise ValueError("aggregate uses different observations")
    population = eligible or (cells if len(cells) == 1 else [])
    for side in ("x", "y"):
        for key, score in (("actual", "actual_score"), ("scenario", "score")):
            values = [cell[side][score] for cell in population if cell[side][score] is not None]
            equal(indices[key][side], mean(values) if values else None)
        equal(indices[side], indices["scenario"][side])
        expected = {}
        for cell in population:
            for factor in cell[side]["factors"]:
                entry = expected.setdefault(factor["id"], {"contribution": 0, "scenario_contribution": 0, "instances": []})
                for key in ("contribution", "scenario_contribution"):
                    entry[key] += factor[key] / len(population)
                entry["instances"].append(factor["instance_id"])
        returned = indices["factor_contributions"][side]
        if len(returned) != len(expected) or {factor["id"] for factor in returned} != set(expected):
            raise ValueError("aggregate factor population mismatch")
        for factor in returned:
            entry = expected[factor["id"]]
            for key in ("contribution", "scenario_contribution"):
                equal(factor[key], entry[key])
            if sorted(factor["instance_ids"]) != sorted(entry["instances"]):
                raise ValueError("aggregate factor references mismatch")
    for key in ("actual", "scenario"):
        expected_z = (indices[key]["x"] + indices[key]["y"]) / 2 if eligible else None
        equal(indices[key]["z"], expected_z)
    equal(indices["z"], indices["scenario"]["z"])


def number(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100):
        raise ValueError("invalid index")


def trio(value: dict):
    for key in ("x", "y", "z"):
        number(value[key])
    if value["z"] is not None:
        if value["x"] is None or value["y"] is None or not math.isclose(value["z"], (value["x"] + value["y"]) / 2, abs_tol=1e-8):
            raise ValueError("inconsistent pair index")


def validate_evidence(evidence, sources, observation=None):
    record = sources[evidence["source_id"]]
    identity = evidence["pair_id"], evidence["month"], evidence["role"]
    if (evidence["version"] != record["version"]
            or identity != (record["pair_id"], record["month"], record["role"])
            or observation is not None and identity != observation):
        raise ValueError("evidence identity mismatch")
    question = evidence["question_id"]
    if evidence["kind"] == "rating":
        if type(evidence["value"]) is not int or record["ratings"].get(question) != evidence["value"]:
            raise ValueError("rating evidence mismatch")
    elif not evidence["quote"] or evidence["quote"] not in (record["texts"].get(question) or ""):
        raise ValueError("quote does not belong to source")


def validate_nested_evidence(value, sources):
    if isinstance(value, dict):
        if {"source_id", "question_id", "kind", "quote", "role", "pair_id", "month", "version"} <= value.keys():
            validate_evidence(value, sources)
        for child in value.values():
            validate_nested_evidence(child, sources)
    elif isinstance(value, list):
        for child in value:
            validate_nested_evidence(child, sources)


def validate_analysis(result: dict, records: list[dict], scope: dict, pairs: list[dict]) -> None:
    if result["scope"] != scope:
        raise ValueError("scope mismatch")
    indices = result["indices"]
    trio(indices)
    trio(indices["actual"])
    trio(indices["scenario"])
    by_source = {r["source_id"]: r for r in records}
    by_observation = {(r["pair_id"], r["month"], r["role"]): r for r in records}
    registry = {pair["pair_id"]: pair for pair in pairs}
    cells = []
    expected_pairs = {pair["pair_id"] for pair in pairs}
    if {row["pair_id"] for row in result["matrix"]} != expected_pairs or len(result["matrix"]) != len(expected_pairs):
        raise ValueError("matrix pair registry mismatch")
    seen = set()
    for row in result["matrix"]:
        if any(row[key] != registry[row["pair_id"]][key] for key in ("mentor_id", "intern_id", "department")):
            raise ValueError("matrix identity mismatch")
        if len(row["months"]) != 6:
            raise ValueError("six months required")
        for cell in row["months"]:
            identity = cell["pair_id"], cell["month"]
            if identity in seen or not 1 <= cell["month"] <= 6 or cell["pair_id"] != row["pair_id"]:
                raise ValueError("invalid matrix cell")
            seen.add(identity)
            cells.append(cell)
            number(cell["z"])
            eligible = all(cell[side]["score"] is not None and cell[side]["coverage"] >= result["methodology"]["min_coverage"] for side in ("x", "y"))
            if cell["eligible"] != eligible:
                raise ValueError("monthly eligibility mismatch")
            for key, score_key in (("actual", "actual_score"), ("scenario", "score")):
                for side in ("x", "y"):
                    equal(cell[key][side], cell[side][score_key])
                equal(cell[key]["z"], (cell["x"][score_key] + cell["y"][score_key]) / 2 if eligible else None)
            equal(cell["z"], cell["scenario"]["z"])
            for side, role in (("x", "mentor"), ("y", "intern")):
                value = cell[side]
                record = by_observation.get((*identity, role))
                if record is None:
                    if value.get("source_id") is not None or value["factors"] or value["score"] is not None or value["actual_score"] is not None:
                        raise ValueError("result for missing survey")
                elif value["source_id"] != record["source_id"] or value["version"] != record["version"]:
                    raise ValueError("monthly source mismatch")
                number(value["score"])
                number(value["actual_score"])
                factors = value["factors"]
                if len({f["instance_id"] for f in factors}) != len(factors) or len({f["id"] for f in factors}) != len(factors):
                    raise ValueError("duplicate factors")
                if record:
                    equal(sum(f["weight"] for f in factors), 100)
                    equal(value["coverage"], sum(f["weight"] for f in factors if f["severity"] is not None) / 100)
                    for factor in factors:
                        equal(factor["contribution"], factor["weight"] * (factor["severity"] or 0))
                        if factor["scenario_contribution"] not in (0, factor["contribution"]):
                            raise ValueError("invalid full-resolution scenario factor")
                    if value["coverage"] == 0 and (value["score"] is not None or value["actual_score"] is not None):
                        raise ValueError("unknown factors produce an index")
                for score_key, factor_key in (("score", "scenario_contribution"), ("actual_score", "contribution")):
                    if value[score_key] is not None and not math.isclose(value[score_key], sum(f[factor_key] for f in factors), abs_tol=1e-8):
                        raise ValueError("contribution mismatch")
                all_evidence = value["evidence"] + value["unclassified_fragments"] + [e for f in factors for e in f["evidence"]]
                for evidence in all_evidence:
                    validate_evidence(evidence, by_source, (*identity, role))
    validate_aggregate(indices, [cell for cell in cells if in_scope(cell, scope)])
    for entry in result["index_dynamics"]:
        validate_aggregate(entry, [cell for cell in cells if in_scope(cell, scope) and cell["month"] == entry["month"]])
    validate_nested_evidence(result, by_source)
