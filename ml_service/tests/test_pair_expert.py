import asyncio
import json

from app import pulse_nlp
from app.knowledge import ROOT
from app.llm import ProviderResult
from app.pair_analytics import analyze
from app.pair_expert import WIRE, expert_policy
from app.pair_schemas import AnalyticsResponse


def survey(role, texts=None):
    return {"role": role, "source_id": f"source-{role}", "version": 1, "pair_id": "pair", "month": 1,
            "intern_id": "intern", "mentor_id": "mentor", "department": "test",
            "ratings": dict.fromkeys(WIRE[role], 5), "texts": texts or {}}


def signal(factor, rule, quote, **fields):
    return {"factor_id": factor, "rule_id": rule, "question_id": "barriers", "quote": quote,
            "assertion": "Наблюдение", "polarity": "negative", "severity": "moderate",
            "temporal_status": "current", "assertion_type": "explicit", "semantic_type": "PROBLEM",
            "verdict": "accepted", **fields}


def test_expert_role_configuration_and_one_call_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_PAIR_POLICY", "expert")
    monkeypatch.setenv("PULSE_TEXT_MODE", "deepseek")
    monkeypatch.setenv("MGTS_DATABASE", str(tmp_path / "cache.db"))
    calls = []
    fixture = json.loads((ROOT / "tests/fixtures/pair_synthetic_text.json").read_text("utf-8"))
    text = fixture["texts"]["barriers"]

    class Provider:
        def __init__(self, config, component, limiter):
            assert config.signal_extractor.model == "deepseek-flash"
            assert component == "signal_extractor"

        async def generate_structured(self, prompt, payload, schema):
            request = json.loads(payload)
            assert len(request["rules"]) == 21
            assert "intern_id" not in request
            calls.append(request)
            return ProviderResult(raw=json.dumps({"signals": [
                signal("S02", "S02_FEEDBACK", text),
                signal("S02", "S02_FEEDBACK", text, temporal_status="historical"),
                signal("S02", "M06_FEEDBACK", text),
                signal("S02", "S02_FEEDBACK", "Несуществующая цитата"),
            ]}), provider="test", model_id="unit")

    monkeypatch.setattr(pulse_nlp, "DeepSeekProvider", Provider)
    policy = expert_policy()
    assert (sum(map(len, policy["roles"].values())), len(policy["rules"]),
            len(policy["actions"]), len(policy["unscored"])) == (13, 39, 27, 6)
    assert policy["numeric_pairs"] == [("M01", "S01")]
    intern = asyncio.run(pulse_nlp.enrich(survey("intern", fixture["texts"])))
    assert intern == asyncio.run(pulse_nlp.enrich(survey("intern", fixture["texts"])))
    assert len(calls) == 1
    assert [s["validation_status"] for s in intern["signals"]] == ["accepted", "review"]
    mentor = survey("mentor")
    scope = {"type": "PAIR_MONTH", "pair_id": "pair", "start": 1, "end": 1}
    actual = AnalyticsResponse.model_validate(analyze([mentor, intern], scope, policy=policy))
    assert actual.indices.actual.model_dump() == {"x": 0, "y": 10, "z": 5}
    assert not any(r.action_id == "P02" for r in actual.recommendations)
    choices = [{"action_id": "S_A02", "scope": scope}, {"action_id": "S_A02", "scope": {"type": "GLOBAL"}}]
    result = AnalyticsResponse.model_validate(analyze([mentor, intern], scope, choices, policy=policy))
    assert result.indices.actual.y == 10 and result.indices.scenario.y == 0
    assert result.indices.actual.z == 5 and result.indices.scenario.z == 0


def test_joint_action_uses_current_evidence_on_both_sides_and_preserves_facts():
    mentor_text = "Приоритеты приходится уточнять повторно, это затрудняет работу."
    intern_text = "Приоритеты задач остаются неясными после обсуждения."
    mentor = survey("mentor", {"comment": mentor_text})
    intern = survey("intern", {"barriers": intern_text})
    mentor_signal = signal("M01", "M01_PRIORITY", mentor_text, question_id="comment", validation_status="accepted")
    intern_signal = signal("S01", "S01_PRIORITY", intern_text, validation_status="accepted")
    mentor["signals"] = [mentor_signal]
    intern["signals"] = [intern_signal]
    available = AnalyticsResponse.model_validate(analyze([mentor, intern], policy=expert_policy()))
    comparison = available.semantic_comparisons[0]
    assert comparison.type == "AGREEMENT" and comparison.score_effect == 0
    assert {q.quote for q in comparison.evidence} == {mentor_text, intern_text}
    assert any(item.action_id == "P01" for item in available.recommendations)
    result = AnalyticsResponse.model_validate(analyze(
        [mentor, intern], selected=[{"action_id": "P01", "scope": {"type": "GLOBAL"}}], policy=expert_policy()))
    assert result.indices.actual.model_dump() == {"x": 10, "y": 10, "z": 10}
    assert result.indices.scenario.model_dump() == {"x": 0, "y": 0, "z": 0}


def test_score_four_contributes_without_problem_or_recommendation():
    mentor = survey("mentor")
    mentor["ratings"]["clarity"] = 4
    intern = survey("intern")
    intern["ratings"]["support"] = 3
    scope = {"type": "PAIR_MONTH", "pair_id": "pair", "start": 1, "end": 1}
    result = AnalyticsResponse.model_validate(analyze([mentor, intern], scope=scope, policy=expert_policy()))
    assert result.indices.actual.model_dump() == {"x": 2, "y": 7, "z": 4.5}
    assert not result.summary["top_factors"] and not result.recommendations
    assert expert_policy(archive=True)["rating_levels"]["4"] == 0


def test_low_ratings_support_pair_and_individual_actions_without_double_deduction():
    mentor, intern = survey("mentor"), survey("intern")
    mentor["ratings"]["autonomy"] = 2
    intern["ratings"].update(clarity=2, support=2)
    scope = {"type": "PAIR_MONTH", "pair_id": "pair", "start": 1, "end": 1}
    policy = expert_policy()
    actual = AnalyticsResponse.model_validate(analyze([mentor, intern], scope, policy=policy))
    options = {item.action_id: item for item in actual.recommendations}
    assert options["P04"].delta.model_dump() == {"x": 11.25, "y": 15, "z": 13.125}
    assert len(options["P04"].evidence_refs) == 2
    assert options["S_A01"].delta.model_dump() == {"x": 0, "y": 15, "z": 7.5}
    assert "P02" not in options and "N01" not in options
    choices = [{"action_id": action, "scope": scope} for action in ("P04", "M_A03", "S_A02")]
    result = AnalyticsResponse.model_validate(analyze([mentor, intern], scope, choices, policy=policy))
    assert result.indices.actual == actual.indices.actual
    assert result.indices.scenario.model_dump() == {"x": 0, "y": 15, "z": 7.5}
    assert all(r.additional_delta.z == 0 for r in result.recommendations if r.action_id in ("P04", "M_A03", "S_A02"))


def test_repeated_three_respects_scope_and_single_review_question_has_its_source():
    mentor = survey("mentor")
    quote = "Мне кажется, что обратной связи недостаточно."
    intern = survey("intern", {"barriers": quote})
    intern["ratings"]["clarity"] = 3
    intern["signals"] = [signal("S02", "S02_FEEDBACK", quote, temporal_status="uncertain", validation_status="review")]
    second = dict(intern, month=2, source_id="source-intern-next")
    scope = {"type": "PAIR_MONTH", "pair_id": "pair", "start": 1, "end": 1}
    policy = expert_policy()
    actual = AnalyticsResponse.model_validate(analyze([mentor, intern, second], scope, policy=policy))
    options = {item.action_id: item for item in actual.recommendations}
    assert options["S_A01"].delta.y == 7
    assert {target.month for target in options["S_A01"].target_observations} == {1}
    question = options["discuss_S02"]
    assert question.assumption == "no_score_change" and question.delta.y == 0 and len(question.evidence_refs) == 1
    assert any(e.id == question.evidence_refs[0] and e.quote == quote for e in actual.sections["intern"].evidence)
