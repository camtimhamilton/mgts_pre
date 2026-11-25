"""Тесты чистой арифметики S2.1/S2.5: индекс, разрыв, текст, сценарии."""

from __future__ import annotations

from collections import defaultdict

import pytest

from app import scoring
from app.generator import generate
from app.normalize import FACTORS


def _scores(value: float) -> dict[str, float]:
    return {factor: value for factor in FACTORS}


def _clamp100(value: float) -> float:
    return min(100.0, max(0.0, value))


def test_constants_match_contract():
    assert scoring.FACTORS == FACTORS
    assert sum(scoring.WEIGHTS.values()) == 100.0
    assert set(scoring.WEIGHTS) == set(FACTORS)
    assert scoring.WEIGHTS == {
        "motivation": 25.0,
        "clarity": 20.0,
        "mentor": 20.0,
        "workload": 15.0,
        "inclusion": 10.0,
        "future": 10.0,
    }
    assert scoring.MAX_REDUCTION == {
        "motivation": 10.0,
        "clarity": 8.0,
        "mentor": 8.0,
        "future": 7.0,
        "workload": 6.0,
        "inclusion": 5.0,
    }
    assert scoring.TEXT_SIGNALS == {
        "left_intent": 8.0,
        "burnout": 5.0,
        "offer_doubt": 5.0,
        "conflict": 4.0,
    }
    assert (
        scoring.TEXT_CAP,
        scoring.GAP_MIN,
        scoring.GAP_STEP,
        scoring.GAP_CAP,
        scoring.STEP_CAP,
        scoring.BAND_LOW,
        scoring.BAND_HIGH,
    ) == (12.0, 2.0, 4.0, 20.0, 25.0, 34.0, 60.0)


def test_all_tens_give_zero_risk():
    factors = scoring.evaluate(_scores(10), _scores(10))

    assert len(factors) == len(FACTORS)
    assert all(factor.points == 0.0 for factor in factors)
    assert all(factor.gap == 0.0 for factor in factors)
    assert scoring.gap_penalty(factors) == 0.0
    assert scoring.risk(factors, 0.0, 0.0) == 0
    assert scoring.band(0) == "low"


def test_all_zeros_give_hundred_risk():
    factors = scoring.evaluate(_scores(0))

    assert sum(factor.points for factor in factors) == pytest.approx(100.0)
    assert scoring.risk(factors, 0.0, 0.0) == 100


def test_risk_clamps_to_zero_and_hundred():
    zeros = scoring.evaluate(_scores(0))
    assert scoring.risk(zeros, scoring.GAP_CAP, scoring.TEXT_CAP) == 100

    # Сумма может выйти за границы не только из-за реальных оценок.
    over = [scoring.Factor("motivation", 150.0, 0.0, 0.0, 0.0, "answer")]
    assert scoring.risk(over, 0.0, 0.0) == 100
    under = [scoring.Factor("motivation", -50.0, 10.0, 10.0, 0.0, "answer")]
    assert scoring.risk(under, 0.0, 0.0) == 0


def test_one_side_has_no_gap_and_needs_no_renormalization():
    factors = scoring.evaluate(_scores(5), None)

    assert len(factors) == len(FACTORS)
    assert all(factor.score_mentor is None for factor in factors)
    assert all(factor.gap is None for factor in factors)
    assert scoring.gap_penalty(factors) == 0.0
    # Все шесть факторов по 5 дают ровно половину шкалы, веса уже в сумме 100.
    assert sum(factor.points for factor in factors) == pytest.approx(50.0)
    assert scoring.risk(factors, 0.0, 0.0) == 50


def test_mentor_only_scores_are_symmetric():
    factors = scoring.evaluate({}, _scores(4))

    assert len(factors) == len(FACTORS)
    assert all(factor.score_intern is None for factor in factors)
    assert all(factor.score_mentor == 4.0 for factor in factors)
    assert scoring.risk(factors, 0.0, 0.0) == 60


def test_empty_inputs_do_not_crash():
    assert scoring.evaluate({}) == []
    assert scoring.evaluate({}, {}) == []
    assert scoring.evaluate({}, None) == []
    assert scoring.gap_penalty([]) == 0.0
    assert scoring.text_penalty([]) == 0.0
    assert scoring.risk([], 0.0, 0.0) == 0


def test_missing_factor_is_skipped():
    factors = scoring.evaluate({"motivation": 0})

    assert [factor.key for factor in factors] == ["motivation"]
    assert factors[0].points == 25.0
    assert factors[0].gap is None
    assert scoring.risk(factors, 0.0, 0.0) == 25


def test_factor_present_on_one_side_only_has_no_gap():
    factors = scoring.evaluate({"clarity": 6}, {"clarity": 8, "future": 5})

    assert [factor.key for factor in factors] == ["clarity", "future"]
    assert factors[0].score_intern == 6.0
    assert factors[0].score_mentor == 8.0
    assert factors[0].gap == 2.0
    assert factors[1].score_intern is None
    assert factors[1].gap is None


def test_points_average_sides_and_use_weight():
    factor = scoring.evaluate({"clarity": 7}, {"clarity": 9})[0]

    assert factor.score_intern == 7.0
    assert factor.score_mentor == 9.0
    assert factor.gap == 2.0
    assert factor.points == pytest.approx((10.0 - 8.0) * 20.0 / 10.0)
    assert factor.origin == "answer"


def test_origin_is_carried_to_every_factor():
    factors = scoring.evaluate(_scores(5), _scores(5), origin="estimated")

    assert {factor.origin for factor in factors} == {"estimated"}


@pytest.mark.parametrize(
    ("gap", "expected"),
    [(0.0, 0.0), (2.0, 0.0), (3.0, 4.0), (4.0, 8.0), (7.0, 20.0), (10.0, 20.0)],
)
def test_gap_penalty_step_and_cap(gap: float, expected: float):
    factor = scoring.Factor("clarity", 0.0, 5.0, 5.0 + gap, gap, "answer")

    assert scoring.gap_penalty([factor]) == expected


def test_gap_penalty_uses_maximum_gap():
    factors = [
        scoring.Factor("clarity", 0.0, 5.0, 7.0, 2.0, "answer"),
        scoring.Factor("mentor", 0.0, 5.0, 10.0, 5.0, "answer"),
    ]

    assert scoring.gap_penalty(factors) == 12.0


def test_text_penalty_counts_duplicate_signal_once():
    assert scoring.text_penalty([{"key": "left_intent"}, {"key": "left_intent"}]) == 8.0
    assert (
        scoring.text_penalty(
            [{"key": "left_intent"}, {"key": "left_intent"}, {"key": "conflict"}]
        )
        == 12.0
    )


def test_text_penalty_is_capped():
    signals = [{"key": key} for key in scoring.TEXT_SIGNALS]

    assert scoring.text_penalty(signals) == scoring.TEXT_CAP


def test_text_penalty_unknown_key_weighs_zero():
    assert scoring.text_penalty([{"key": "unknown"}]) == 0.0
    assert scoring.text_penalty([{"key": "burnout"}, {"key": "unknown"}]) == 5.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "low"), (34, "low"), (35, "watch"), (59, "watch"), (60, "critical"), (100, "critical")],
)
def test_band_boundaries(value: int, expected: str):
    assert scoring.band(value) == expected


def test_urgency_ignores_negative_delta():
    assert scoring.urgency(50, -20) == 50.0
    assert scoring.urgency(50, 0) == 50.0
    assert scoring.urgency(50, 8) == 62.0


def test_scenario_single_point_is_flat():
    result = scoring.scenario([("M2", 50)], reductions=10, horizon=3)

    assert [point["month"] for point in result["base"]] == ["M3", "M4", "M5"]
    assert [point["risk"] for point in result["base"]] == [50, 50, 50]
    assert [point["risk"] for point in result["with_actions"]] == [40, 40, 40]
    assert result["base"][0] == {"month": "M3", "risk": 50, "low": 40, "high": 60}
    assert result["event"] is None


def test_scenario_uses_last_three_points_and_attenuates():
    result = scoring.scenario(
        [("M1", 20), ("M2", 30), ("M3", 40)], reductions=0, horizon=3
    )
    risks = [point["risk"] for point in result["base"]]

    # Наклон последних трёх точек = 10: +10, затем 2×0.7×10 = +14, затем 3×0.49×10 = +14.7.
    assert risks == [50, 54, 55]
    assert risks[1] - risks[0] == 4  # прирост затухает
    assert risks[2] - risks[1] == 1
    assert [point["month"] for point in result["base"]] == ["M4", "M5", "M6"]


def test_scenario_ignores_points_older_than_three():
    history = [("M1", 0), ("M2", 0), ("M3", 40), ("M4", 50), ("M5", 60)]

    result = scoring.scenario(history, reductions=0, horizon=1)

    # Наклон по 40, 50, 60 = 10, а не по всей истории.
    assert result["base"] == [{"month": "M6", "risk": 70, "low": 64, "high": 76}]


def test_scenario_corridor_narrows_with_more_history():
    one = scoring.scenario([("M3", 50)], 0, 1)["base"][0]
    two = scoring.scenario([("M2", 50), ("M3", 50)], 0, 1)["base"][0]
    three = scoring.scenario([("M1", 50), ("M2", 50), ("M3", 50)], 0, 1)["base"][0]

    assert (one["high"] - one["low"], two["high"] - two["low"], three["high"] - three["low"]) == (
        20,
        16,
        12,
    )


def test_scenario_actions_lower_risk_and_are_capped():
    history = [("M1", 20), ("M2", 30), ("M3", 40)]

    base = scoring.scenario(history, reductions=0, horizon=1)["base"][0]["risk"]
    action = scoring.scenario(history, reductions=10, horizon=1)["with_actions"][0]["risk"]
    capped = scoring.scenario(history, reductions=100, horizon=1)["with_actions"][0]["risk"]

    assert base == 50
    assert action == 40
    assert capped == 25  # 50 − STEP_CAP


def test_scenario_event_found_for_first_red_month():
    result = scoring.scenario([("M1", 40), ("M2", 50), ("M3", 60)], reductions=10, horizon=3)

    assert [point["risk"] for point in result["base"]] == [70, 74, 75]
    assert result["event"] == "красная зона к M4"


def test_scenario_has_no_event_below_red_zone():
    result = scoring.scenario([("M1", 30), ("M2", 35), ("M3", 40)], reductions=0, horizon=3)

    assert [point["risk"] for point in result["base"]] == [45, 47, 47]
    assert result["event"] is None


def test_scenario_is_empty_after_m6():
    result = scoring.scenario([("M4", 40), ("M5", 50), ("M6", 60)], reductions=10, horizon=3)

    assert result == {"base": [], "with_actions": [], "event": None}


def test_scenario_empty_history_is_empty():
    assert scoring.scenario([], reductions=10) == {"base": [], "with_actions": [], "event": None}


def test_scenario_horizon_does_not_leave_program():
    result = scoring.scenario([("M4", 50)], reductions=0, horizon=3)

    assert [point["month"] for point in result["base"]] == ["M5", "M6"]


def test_scenario_clamps_risk_and_corridor():
    high = scoring.scenario([("M3", 95)], reductions=0, horizon=1)["base"][0]
    low = scoring.scenario([("M3", 5)], reductions=0, horizon=1)["base"][0]

    assert high["risk"] == 95
    assert high["high"] == 100
    assert low["low"] == 0


def test_scenario_actions_never_go_below_zero():
    result = scoring.scenario([("M3", 10)], reductions=100, horizon=1)

    assert result["with_actions"][0]["risk"] == 0


def test_risk_is_exact_sum_when_components_are_integral():
    factors = [
        scoring.Factor("motivation", 25.0, 0.0, 0.0, 0.0, "answer"),
        scoring.Factor("clarity", 20.0, 0.0, 0.0, 0.0, "answer"),
    ]
    gap, text = 8.0, 12.0

    assert scoring.risk(factors, gap, text) == sum(f.points for f in factors) + gap + text == 65


def test_demo_data_risk_invariants():
    grouped: dict[tuple[str, str], dict] = defaultdict(dict)
    for submission in generate(seed=42):
        grouped[(submission.intern_name, submission.month)][submission.role] = submission

    checked = 0
    for (_, month), roles in grouped.items():
        intern = roles.get("intern")
        if intern is None:
            continue
        mentor = roles.get("mentor")
        factors = scoring.evaluate(intern.scores, mentor.scores if mentor else None)
        gap = scoring.gap_penalty(factors)
        text = scoring.text_penalty([{"key": "left_intent"}] if month in ("M5", "M6") else [])
        value = scoring.risk(factors, gap, text)
        total = sum(factor.points for factor in factors) + gap + text

        assert 0 <= value <= 100
        # risk() округляет сумму: сверяем с точностью до округления, а не побитово.
        assert value == round(_clamp100(total))
        assert abs(_clamp100(total) - value) <= 0.5 + 1e-9
        checked += 1

    assert checked == 72  # 12 пар × 6 месяцев, включая месяц без анкеты наставника

    # Каждая анкета по отдельности — тоже валидный односторонний расчёт.
    for submission in generate(seed=42):
        factors = scoring.evaluate(submission.scores)
        gap = scoring.gap_penalty(factors)
        value = scoring.risk(factors, gap, 0.0)
        total = sum(factor.points for factor in factors) + gap

        assert 0 <= value <= 100
        assert abs(_clamp100(total) - value) <= 0.5 + 1e-9
