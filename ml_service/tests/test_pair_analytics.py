"""Regression invariants for independent indices and scenario factor unions."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.pair_analytics import aggregate, analyze, apply_scenario, band, load_policy, validate_policy
from app.pair_schemas import AnalyticsRequest, AnalyticsScope


def record(role="mentor", month=1, ratings=None, pair_id="pair-a"):
    policy = load_policy()
    return {"pair_id": pair_id, "mentor_id": "Н-001", "intern_id": "СТ-001", "department": "IT",
            "month": month, "role": role, "version": 1, "source_id": f"{pair_id}-{role}-{month}",
            "ratings": ratings if ratings is not None else dict.fromkeys(policy["roles"][role], 4),
            "texts": {}, "text_analysis_status": "completed"}


def test_separate_policy_weight_totals_and_boundaries():
    policy = load_policy()
    assert [sum(factor["weight"] for factor in factors.values()) for factors in policy["roles"].values()] == [100, 100]
    assert [band(value, 1, policy) for value in (0, 30, 70, 100, None)] == ["green", "yellow", "red", "red", "gray"]
    assert band(0, .1, policy) == "gray"


def test_max_union_rating_text_and_proof_integrity():
    item = record(ratings={"clarity": 3})
    item["texts"] = {"comment": "Задачи непонятны."}
    signal = {"factor_id": "clarity", "rule_id": "text.mentor.clarity", "question_id": "comment",
              "quote": "Задачи непонятны.", "severity": "high", "validation_status": "accepted",
              "temporal_status": "current", "polarity": "negative"}
    item["signals"] = [signal, signal, {**signal, "factor_id": "quality", "quote": "выдуманная цитата"}]
    result = analyze([item])["matrix"][0]["months"][0]["x"]
    assert result["score"] == 20
    assert result["score"] == sum(factor["contribution"] for factor in result["factors"])
    assert result["coverage"] == .2
    assert result["band"] == "gray"
    assert result["unknown_factors"] == ["quality", "autonomy", "deadlines", "initiative", "team"]


def test_missing_and_empty_surveys_do_not_become_zero():
    result = analyze([record(ratings={})])["matrix"][0]["months"]
    assert result[0]["x"]["score"] is None
    assert result[0]["y"]["score"] is None
    assert result[0]["z"] is None
    assert result[1]["x"]["status"] == "missing"


def test_common_aggregation_population_excludes_unpaired():
    records = [record(), record("intern"), record(month=2, ratings=dict.fromkeys(load_policy()["roles"]["mentor"], 1))]
    result = analyze(records)["indices"]
    assert result["x"] == result["y"] == result["z"] == 0
    assert result["eligible_count"] == 1
    assert result["side_statistics"]["mentor"] == {"mean": 50, "count": 2}
    assert result["z"] == (result["x"] + result["y"]) / 2


def test_one_month_range_matches_month_and_reverse_range():
    records = [record(), record("intern")]
    month = analyze(records, {"type": "PAIR_MONTH", "pair_id": "pair-a", "start": 1})
    interval = analyze(records, {"type": "PAIR_RANGE", "pair_id": "pair-a", "start": 1, "end": 1})
    assert month["indices"] == interval["indices"]
    assert AnalyticsScope(type="PAIR_RANGE", pair_id="pair-a", start=5, end=2).model_dump()["start"] == 2


def test_joint_scenario_60_80_to_40_56_union_and_actual_immutable():
    policy = load_policy()
    cell = {"pair_id": "p", "month": 1, "eligible": True, "z": 70,
            "actual": {"x": 60, "y": 80, "z": 70}}
    for side, role, score, removed in (("x", "mentor", 60, 20), ("y", "intern", 80, 24)):
        cell[side] = {"score": score, "actual_score": score, "coverage": 1, "status": "completed", "factors": [
            {"id": "clarity", "instance_id": f"p:1:{role}:clarity", "contribution": removed, "scenario_contribution": removed},
            {"id": "other", "instance_id": f"p:1:{role}:other", "contribution": score - removed, "scenario_contribution": score - removed}]}
    selection = {"action_id": "action_pair_clarity", "scope": {"type": "GLOBAL"}}
    scenario = apply_scenario([cell], [selection, selection], policy)[0]
    assert scenario["scenario"] == {"x": 40, "y": 56, "z": 48}
    assert cell["z"] == 70
    assert scenario["actual"] == {"x": 60, "y": 80, "z": 70}
    repeated = apply_scenario([scenario], [{"action_id": "action_mentor_clarity", "scope": {"type": "GLOBAL"}}], policy)[0]
    assert repeated["scenario"] == scenario["scenario"]


def test_scoped_actions_only_affect_confirmed_instances():
    records = [record(role, month, dict.fromkeys(load_policy()["roles"][role], 1))
               for month in (1, 2) for role in ("mentor", "intern")]
    selected = [{"action_id": "action_mentor_clarity", "scope": {"type": "PAIR_MONTH", "pair_id": "pair-a", "start": 1, "end": 1}}]
    actual = analyze(records)
    scenario = analyze(records, selected=selected)
    cells = scenario["matrix"][0]["months"]
    assert cells[0]["x"]["score"] == 80
    assert cells[0]["y"]["score"] == 100
    assert cells[1]["x"]["score"] == 100
    assert scenario["indices"]["actual"] == actual["indices"]["actual"]
    assert analyze(records)["indices"] == actual["indices"]


def test_version_changes_analysis_identity_and_critical_pending_no_points():
    first = record("intern")
    first["texts"] = {"difficulties": "Я планирую досрочно прекратить стажировку."}
    first["signals"] = [{"critical": True, "validation_status": "review", "temporal_status": "current", "signal_id": "exit-review",
                         "question_id": "difficulties", "quote": first["texts"]["difficulties"]}]
    result = analyze([first])
    assert result["matrix"][0]["months"][0]["y"]["score"] == 0
    assert result["sections"]["intern"]["critical_flags"] == ["exit-review"]
    changed = deepcopy(first)
    changed["version"] = 2
    assert result["analysis_version"] != analyze([changed])["analysis_version"]


def test_typed_http_local_text_conflict_resolution_and_source_links(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import create_app
    monkeypatch.setenv("PULSE_TEXT_MODE", "local_rules")
    monkeypatch.setenv("MGTS_DATABASE", str(tmp_path / "nlp.db"))
    records = [record(role, month) for month in (1, 2) for role in ("mentor", "intern")]
    for item in records:
        item.pop("text_analysis_status")
    records[1]["texts"] = {"difficulties": "Слишком высокая нагрузка."}
    records[3]["texts"] = {"difficulties": "Проблема с нагрузкой решена."}
    with TestClient(create_app(tmp_path / "analysis.db")) as client:
        response = client.post("/v2/analytics", json={"records": records})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["matrix"][0]["months"][0]["y"]["score"] == 7.5
        workload = next(factor for factor in result["matrix"][0]["months"][0]["y"]["factors"] if factor["id"] == "workload")
        assert workload["review_flags"] == ["rating_text_conflict_requires_review"]
        assert any(pattern["type"] == "confirmed_resolution" for pattern in result["patterns"])
        claims = result["sections"]["intern"]["claims"]
        assert all(claim["evidence_refs"] for claim in claims)
        assert client.post("/v2/analytics", json={"records": records}).json()["analysis_version"] == result["analysis_version"]
        methodology = client.get("/v2/methodology").json()
        assert methodology["cache_version"]
        assert methodology["text_mode"] == "local_rules"


def test_registry_without_records_has_six_gray_cells_and_future_status():
    identity = {key: record()[key] for key in ("pair_id", "mentor_id", "intern_id", "department")}
    result = analyze([], pairs=[identity], current_month=2)
    assert len(result["matrix"][0]["months"]) == 6
    assert result["matrix"][0]["months"][2]["x"]["status"] == "future"
    assert result["indices"]["z"] is None


def test_joint_action_requires_both_sides_in_same_observation():
    mentor = record(ratings={"clarity": 1})
    intern = record("intern", 2, {"clarity": 1})
    recommendations = analyze([mentor, intern])["recommendations"]
    assert all(item["impact_scope"] != "PAIR" for item in recommendations)


def test_scope_patterns_and_aggregate_factor_sums():
    records = [record(role, month, dict.fromkeys(load_policy()["roles"][role], 1))
               for month in range(1, 7) for role in ("mentor", "intern")]
    result = analyze(records, {"type": "PAIR_RANGE", "pair_id": "pair-a", "start": 3, "end": 4})
    assert all(set(pattern.get("months", [pattern.get("month")])) <= {3, 4} for pattern in result["patterns"])
    assert result["indices"]["x"] == sum(item["contribution"] for item in result["indices"]["factor_contributions"]["x"])
    assert result["indices"]["y"] == sum(item["contribution"] for item in result["indices"]["factor_contributions"]["y"])


def test_recommendation_potential_independent_of_existing_selections():
    records = [record(role, 1, dict.fromkeys(load_policy()["roles"][role], 1)) for role in ("mentor", "intern")]
    scope = {"type": "PAIR_MONTH", "pair_id": "pair-a", "start": 1, "end": 1}
    actual = analyze(records, scope)
    selected = [{"action_id": "action_pair_clarity", "scope": scope}]
    scenario = analyze(records, scope, selected)
    first = next(item for item in actual["recommendations"] if item["action_id"] == "action_mentor_quality")
    second = next(item for item in scenario["recommendations"] if item["action_id"] == "action_mentor_quality")
    assert first["delta"] == second["delta"] == {"x": 20, "y": 0, "z": 10}
    repeated = next(item for item in scenario["recommendations"] if item["action_id"] == "action_mentor_clarity")
    assert repeated["additional_delta"] == {"x": 0, "y": 0, "z": 0}


@pytest.mark.parametrize("mentor_text,intern_text,expected", [
    ("Задачи понятны.", "Задачи понятны.", "AGREEMENT"),
    ("Хорошо понимает задачи.", "Задачи непонятны.", "CONTRADICTION"),
    ("Нужны регулярные короткие сверки по задачам.", "Неясные задачи мешают работе.", "COMPLEMENTARY"),
    ("Хорошо понимает задачи.", "Наставник не знает о проблемах с задачами.", "AWARENESS_GAP"),
    ("Хорошо понимает задачи.", "", "NOT_COMPARABLE"),
    ("Если задачи понятны, можно продолжать.", "Задачи непонятны.", "NOT_COMPARABLE"),
])
def test_semantic_comparison_requires_explicit_supported_text(mentor_text, intern_text, expected):
    mentor, intern = record(), record("intern")
    mentor["texts"] = {"comment": mentor_text}
    intern["texts"] = {"difficulties": intern_text}
    result = analyze([mentor, intern], {"type": "PAIR_MONTH", "pair_id": "pair-a", "start": 1})
    comparison = next(item for item in result["semantic_comparisons"] if item["aspect"] == "clarity")
    assert comparison["type"] == expected
    assert comparison["score_effect"] == 0
    assert result["indices"]["z"] == 0
    for evidence in comparison["evidence"]:
        original = mentor if evidence["role"] == "mentor" else intern
        assert evidence["quote"] in original["texts"][evidence["question_id"]]


def test_numeric_gap_alone_is_not_semantic_contradiction_or_awareness_gap():
    mentor, intern = record(ratings={"clarity": 5}), record("intern", ratings={"clarity": 1})
    result = analyze([mentor, intern])
    assert result["discrepancies"][0]["type"] == "rating_gap"
    assert all(item["type"] == "NOT_COMPARABLE" for item in result["semantic_comparisons"])


def test_text_abstention_and_scope_actual_trend():
    first = record(ratings=dict.fromkeys(load_policy()["roles"]["mentor"], 1))
    first["texts"] = {"comment": "Новая предметная область требует изучения."}
    first["nlp_audit"] = {"mode": "local_rules"}
    records = [first, record("intern"), record(month=3), record("intern", 3)]
    result = analyze(records, {"type": "PAIR_RANGE", "pair_id": "pair-a", "start": 1, "end": 3})
    side = result["matrix"][0]["months"][0]["x"]
    assert side["text_interpretation_status"] == "local_abstain"
    assert side["unclassified_fragments"][0]["quote"] == first["texts"]["comment"]
    history = result["sections"]["pair"]["history_summary"]
    assert history["first"]["score"] == history["max"]["score"] == 50
    assert history["last"]["score"] == 0
    assert history["trend"]["direction"] == "improvement"
    assert history["timeline"][1]["score"] is None
    assert history["periods_available"] == 2


def test_request_rejects_duplicate_revision_and_bad_rating():
    item = record()
    item.pop("text_analysis_status")
    with pytest.raises(ValidationError):
        AnalyticsRequest(records=[item, item])
    item["ratings"]["clarity"] = 6
    with pytest.raises(ValidationError):
        AnalyticsRequest(records=[item])


@pytest.mark.parametrize("path,value", [
    (("roles", "mentor", "clarity", "weight"), True),
    (("roles", "mentor", "clarity", "weight"), -1),
    (("rating_levels", "1"), True),
    (("rating_levels", "3"), .7),
    (("rating_levels", "6"), 0),
    (("thresholds", "yellow"), float("nan")),
    (("min_coverage",), True),
    (("rating_gap_threshold",), 0),
    (("actions", "clarity", "responsible"), "unknown-role"),
    (("rules", "rule_mentor_clarity", "action_factor"), "quality"),
    (("rules", "rule_mentor_clarity", "source_id"), "missing-source"),
    (("comparisons",), ["quality"]),
    (("roles", "unknown-role"), {}),
])
def test_policy_rejects_malformed_references_and_numbers(path, value):
    policy = load_policy()
    parent = policy
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(ValueError):
        validate_policy(policy)


def test_explanations_and_gaps_follow_current_policy():
    policy = load_policy()
    policy["rating_levels"]["3"] = 1
    policy["rating_gap_threshold"] = .9
    result = analyze([record(ratings={"clarity": 3}), record("intern", ratings={"clarity": 5})], policy=policy)
    assert result["matrix"][0]["months"][0]["x"]["score"] == 20
    assert "3 → 1" in result["matrix"][0]["months"][0]["x"]["factors"][0]["activation_rule"]
    assert not result["discrepancies"]


def test_invalid_action_alias_outside_registry_and_no_target_fail_closed():
    records = [record(), record("intern")]
    with pytest.raises(KeyError):
        analyze(records, selected=[{"action_id": "action_clarity", "scope": {"type": "GLOBAL"}}])
    with pytest.raises(KeyError):
        analyze(records, selected=[{"action_id": "action_pair_clarity", "scope": {"type": "PAIR_MONTH", "pair_id": "missing", "start": 1, "end": 1}}])
    with pytest.raises(ValueError, match="no_active_targets"):
        analyze(records, selected=[{"action_id": "action_pair_clarity", "scope": {"type": "GLOBAL"}}])


def test_selection_scope_survives_changing_view():
    records = [record(role, month, dict.fromkeys(load_policy()["roles"][role], 1)) for role in ("mentor", "intern") for month in (1, 2)]
    selection = {"action_id": "action_mentor_clarity", "scope": {"type": "PAIR_MONTH", "pair_id": "pair-a", "start": 1, "end": 1}}
    result = analyze(records, {"type": "PAIR_MONTH", "pair_id": "pair-a", "start": 2}, [selection])
    assert result["indices"]["x"] == 100
    assert result["matrix"][0]["months"][0]["x"]["score"] == 80


def test_side_recalculation_reuses_unchanged_revisions_and_returns_isolated_results():
    from app.pair_analytics import _cached_side
    _cached_side.cache_clear()
    records = [record(), record("intern")]
    first = analyze(records)
    info = _cached_side.cache_info()
    first["matrix"][0]["months"][0]["x"]["factors"][0]["contribution"] = 100
    second = analyze(records)
    assert _cached_side.cache_info().hits == info.hits + 2
    assert second["matrix"][0]["months"][0]["x"]["factors"][0]["contribution"] == 0
    changed = deepcopy(records)
    changed[0]["version"] = 2
    analyze(changed)
    assert _cached_side.cache_info().misses == info.misses + 1


def test_review_topics_add_no_score_and_do_not_become_score_reasons_or_characteristic_quotes():
    item = record("intern", ratings={"workload": 1})
    item["texts"] = {"difficulties": "Нагрузка слишком высокая. Синтетический фрагмент для проверки темы."}
    item["nlp_audit"] = {"mode": "local_encoder", "config_hash": "encoder-config"}
    item["nlp_version"] = "encoder-nlp-v1"
    item["signals"] = [{"factor_id": "workload", "question_id": "difficulties", "quote": "Синтетический фрагмент для проверки темы.",
                        "rule_id": "text.intern.workload", "severity": "none", "polarity": "neutral", "temporal_status": "uncertain",
                        "validation_status": "review", "encoder_version": "test-encoder-v1"}]
    result = analyze([item])
    side = result["matrix"][0]["months"][0]["y"]
    assert side["score"] == 15
    assert side["band"] == "gray"
    assert side["text_mode"] == "local_encoder"
    assert side["text_interpretation_status"] == "local_encoder_review"
    assert side["nlp_identity"]["encoder_versions"] == ["test-encoder-v1"]
    factor = next(factor for factor in side["factors"] if factor["id"] == "workload")
    assert all(reference["kind"] == "rating" for reference in factor["evidence"] if reference["id"] in factor["score_evidence_refs"])
    assert result["sections"]["intern"]["highlighted_evidence"] is None
    assert result["versions"]["nlp_identities"][0]["version"] == "encoder-nlp-v1"
