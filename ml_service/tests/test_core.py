from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.core import aggregate, discrepancies, rating_signals, score, validate_candidate
from app.knowledge import load_knowledge, load_mapping, validate_knowledge
from app.schemas import Candidate, Policy, Response, SurveyPeriod


@pytest.fixture
def kb():
    return load_knowledge()


def response(text="Наставник регулярно не отвечает", role="intern"):
    return Response(response_id="r1", respondent_role=role, answers=[{
        "question_id": "q02", "question_text": "Какие сложности?", "type": "text", "value": text,
    }])


def candidate(**changes):
    return Candidate.model_validate({"factor_id": "R01", "rule_id": "RULE_R01_001", "question_id": "q02",
        "polarity": "negative", "severity": "high", "evidence": "Наставник регулярно не отвечает",
        "assertion": "explicit", "temporal_status": "current", **changes})


def survey(responses=()):
    return SurveyPeriod(program_id="demo", intern_id="i1", period=3, responses=responses)


def test_knowledge(kb):
    assert sum(kb.policy.weights.values()) == 100
    assert len(kb.factors) == 10
    assert len(kb.content_hash) == 64
    assert load_knowledge().content_hash == kb.content_hash


def test_bad_weight(kb):
    data = kb.policy.model_dump()
    data["weights"]["R01"] = 13
    with pytest.raises(ValidationError):
        Policy.model_validate(data)


def test_bad_reference(kb):
    broken = kb.rules[0].model_copy(update={"action_ids": ("A99",)})
    with pytest.raises(ValueError):
        validate_knowledge(kb.model_copy(update={"rules": (broken,) + kb.rules[1:]}))


@pytest.mark.parametrize("field,value", [("period", 0), ("period", 7), ("period", True),
    ("program_id", ""), ("context", {"age": True})])
def test_invalid_survey(field, value):
    data = survey().model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        SurveyPeriod.model_validate(data)


@pytest.mark.parametrize("value,lo,hi", [(0, 1, 5), (6, 1, 5), (float("nan"), 1, 5),
    (float("inf"), 1, 5), (1, 1, 1), (True, 1, 5), ("1", 1, 5)])
def test_invalid_rating(value, lo, hi):
    with pytest.raises(ValidationError):
        Response(response_id="r", respondent_role="intern", answers=[{
            "type": "rating", "question_id": "q01", "question_text": "Оценка", "value": value,
            "scale_min": lo, "scale_max": hi}])


@pytest.mark.parametrize("changes,expected", [
    ({"evidence": "Выдуманная цитата"}, "rejected"),
    ({"rule_id": "invented"}, "rejected"),
    ({"factor_id": "R02"}, "rejected"),
    ({"question_id": "q99"}, "rejected"),
    ({"assertion": "implicit"}, "review"),
    ({"assertion": "ambiguous"}, "review"),
    ({"temporal_status": "historical"}, "rejected"),
    ({"temporal_status": "resolved"}, "rejected"),
    ({"temporal_status": "hypothetical"}, "rejected"),
    ({"temporal_status": "unknown"}, "review"),
    ({"polarity": "positive"}, "rejected"),
])
def test_candidate_validation(kb, changes, expected):
    assert validate_candidate(candidate(**changes), response(), kb).validation_status == expected


def test_quote_offsets_and_duplicate(kb):
    text = "😀 " + candidate().evidence
    signal = validate_candidate(candidate(), response(text), kb)
    assert text[signal.start:signal.end] == signal.quote
    assert signal.start == 2
    assert signal.semantic_verification == "not_run"
    repeat = validate_candidate(candidate(), response(text + " " + text), kb)
    assert repeat.validation_status == "review" and repeat.start is None


def test_r10_and_strict(kb):
    c = candidate(factor_id="R10", rule_id="RULE_R10_001", evidence="Я решил досрочно прекратить стажировку")
    r = response(c.evidence)
    s = validate_candidate(c, r, kb)
    assert s.validation_status == "review"
    factors = aggregate([s], kb)
    risk = score(factors, kb, survey([r]))
    assert risk.score is None and risk.priority == "pending_review"
    assert validate_candidate(c, response(c.evidence, "mentor"), kb).validation_status == "rejected"
    assert validate_candidate(candidate(), response(), kb, "strict").validation_status == "review"


def test_positive_and_empty(kb):
    s = validate_candidate(candidate(polarity="positive", severity="none"), response(), kb)
    factors = aggregate([s], kb)
    assert factors[0].state == "observed_no_signal"
    assert score(factors, kb, survey()).score == 0
    risk = score(aggregate([], kb), kb, survey())
    assert risk.score is None and risk.data_coverage == 0 and risk.priority == "insufficient_data"


def test_max_and_coverage(kb):
    r = response(candidate().evidence + ". Не совсем понимаю, какого результата от меня ждут.")
    a = validate_candidate(candidate(), r, kb)
    b = validate_candidate(candidate(factor_id="R03", rule_id="RULE_R03_001", severity="moderate",
        evidence="Не совсем понимаю, какого результата от меня ждут"), r, kb)
    ratings = Response(response_id="r2", respondent_role="mentor", answers=[{
        "type": "rating", "question_id": "q03", "question_text": "Поддержка", "value": 5,
        "scale_min": 1, "scale_max": 5}])
    numeric, _ = rating_signals(survey([r, ratings]), load_mapping())
    factors = aggregate([a, a, b, *numeric], kb)
    risk = score(factors, kb, survey([r, ratings]))
    assert risk.score == 17 and risk.data_coverage == 0.22
    assert risk.priority == "insufficient_data" and risk.score_band == "routine"
    assert factors[0].contribution == 12 and factors[0].state == "conflicting"


def test_overlap_routes_review(kb):
    a = validate_candidate(candidate(), response(), kb)
    b = validate_candidate(candidate(factor_id="R03", rule_id="RULE_R03_001"), response(), kb)
    factors = aggregate([a, b], kb)
    assert sum(f.contribution for f in factors) == 0
    assert all(s.validation_status == "review" for f in factors for s in f.evidence)


def test_nested_overlap(kb):
    r = response("Нужна помощь с задачей")
    a = validate_candidate(candidate(evidence="Нужна помощь с задачей"), r, kb)
    b = validate_candidate(candidate(factor_id="R03", rule_id="RULE_R03_001", evidence="помощь с задачей"), r, kb)
    assert sum(f.contribution for f in aggregate([a, b], kb)) == 0


def test_snapshot_immutable(kb):
    with pytest.raises(TypeError):
        kb.policy.weights["R01"] = 999
    with pytest.raises(TypeError):
        kb.rules[0].severity.update(high="changed")
    assert sum(kb.policy.weights.values()) == 100


@pytest.mark.parametrize("temporal,polarity,severity", [
    ("historical", "negative", "high"), ("resolved", "negative", "high"),
    ("hypothetical", "negative", "high"), ("current", "positive", "none")])
def test_noncritical_r10_repeated_quote(kb, temporal, polarity, severity):
    text = "Я прекращу программу досрочно"
    c = candidate(factor_id="R10", rule_id="RULE_R10_001", evidence=text,
                  temporal_status=temporal, polarity=polarity, severity=severity)
    r = response(text + ". " + text)
    s = validate_candidate(c, r, kb, "strict")
    assert not score(aggregate([s], kb), kb, survey([r])).critical_flags


def test_ratings_and_gap(kb):
    responses = [Response(response_id=f"r{i}", respondent_role=role, answers=[{
        "type": "rating", "question_id": q, "question_text": "Поддержка", "value": value,
        "scale_min": 1, "scale_max": 5}]) for i, (role, q, value) in enumerate([
        ("intern", "q01", 2), ("mentor", "q03", 5)])]
    data = survey(responses)
    signals, warnings = rating_signals(data, load_mapping())
    assert not warnings and aggregate(signals, kb)[0].contribution == 6
    gaps, warnings = discrepancies(data, load_mapping())
    assert not warnings and gaps[0].normalized_gap == 0.75
    assert gaps[0].threshold_exceeded
    changed = data.model_dump()
    changed["responses"][0]["answers"][0]["question_id"] = "unknown"
    assert "unmapped_rating" in rating_signals(SurveyPeriod.model_validate(changed), load_mapping())[1]


def test_missing_and_fractional_rating(kb):
    r = Response(response_id="r", respondent_role="intern", answers=[{
        "type": "rating", "question_id": "q01", "question_text": "Поддержка", "value": 2.5,
        "scale_min": 1, "scale_max": 5}])
    signals, warnings = rating_signals(survey([r]), load_mapping())
    assert signals == [] and warnings == ["unmapped_rating_value"]


@pytest.mark.parametrize("active,expected,band", [
    ([], 0, "routine"), (["R01"], 12, "routine"),
    (["R01", "R02"], 20, "elevated"),
    (["R01", "R02", "R03", "R05"], 40, "high")])
def test_complete_coverage_thresholds(kb, active, expected, band):
    signals = []
    # Each factor gets its own explicit evidence; inactive factors have positive coverage.
    texts = {factor.id: f"Фактор {factor.id}: синтетическое отдельное свидетельство" for factor in kb.factors}
    full_answer = " | ".join(texts.values())
    for factor in kb.factors:
        text = texts[factor.id]
        role = "mentor" if factor.id == "R07" else "intern"
        r = response(full_answer, role).model_copy(update={"response_id": "r_" + role})
        c = candidate(factor_id=factor.id, rule_id=f"RULE_{factor.id}_001", evidence=text,
            polarity="negative" if factor.id in active else "positive",
            severity="high" if factor.id in active else "none")
        signals.append(validate_candidate(c, r, kb))
    factors = aggregate(signals, kb)
    risk = score(factors, kb, survey())
    assert risk.score == expected and risk.data_coverage == 1
    assert risk.score_band == band and risk.priority == band
    assert not risk.missing_required_roles
    incomplete = score(factors, kb, survey(), incomplete=True)
    assert incomplete.score_band == band and incomplete.priority == "insufficient_data"


def test_draft_rule_and_positive_no_compensation(kb):
    draft = kb.rules[0].model_copy(update={"status": "draft"})
    modified = kb.model_copy(update={"rules": (draft,) + kb.rules[1:]})
    assert validate_candidate(candidate(), response(), modified).validation_status == "rejected"
    negative = validate_candidate(candidate(), response(), kb)
    text = "Наставник помогает по другому вопросу"
    positive = validate_candidate(candidate(evidence=text, polarity="positive", severity="none"), response(text, "mentor"), kb)
    factors = aggregate([negative, positive], kb)
    assert factors[0].contribution == 12 and factors[0].state == "conflicting"
