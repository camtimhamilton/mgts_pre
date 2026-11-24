"""Тесты генератора демо-данных S1.2."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import replace

from app.db import connect, init_db
from app.generator import STORIES, generate, plan_pairs
from app.normalize import FACTORS, MONTHS, ROLES
from app.repo import upsert_submission


def test_generate_is_deterministic():
    first = [submission.as_dict() for submission in generate(seed=42)]
    second = [submission.as_dict() for submission in generate(seed=42)]

    assert first == second
    assert first != [submission.as_dict() for submission in generate(seed=43)]


def test_all_five_stories_present():
    plans = plan_pairs(seed=42, pairs=12)
    counts = Counter(plan.story for plan in plans)

    assert len(plans) == 12
    assert set(counts) == set(STORIES)
    assert min(counts.values()) >= 2


def test_missing_pair_has_one_less_submission():
    plans = {plan.intern_name: plan for plan in plan_pairs(seed=42, pairs=12)}
    grouped: dict[str, list] = defaultdict(list)
    for submission in generate(seed=42):
        grouped[submission.intern_name].append(submission)

    assert set(grouped) == set(plans)
    for plan in plans.values():
        items = grouped[plan.intern_name]
        intern_months = {item.month for item in items if item.role == "intern"}
        mentor_months = {item.month for item in items if item.role == "mentor"}

        assert intern_months == set(MONTHS)
        if plan.story == "missing":
            assert plan.missing_month is not None
            assert len(items) == 11
            assert plan.missing_month not in mentor_months
            assert len(mentor_months) == 5
        else:
            assert len(items) == 12
            assert mentor_months == set(MONTHS)


def test_months_roles_and_scores_are_valid():
    submissions = generate(seed=42)
    assert len(submissions) == 142

    for submission in submissions:
        assert submission.month in MONTHS
        assert submission.role in ROLES
        assert set(submission.scores) == set(FACTORS)
        for score in submission.scores.values():
            assert isinstance(score, int)
            assert 0 <= score <= 10

        assert submission.received_at is not None
        assert "2026-02-01" <= submission.received_at[:10] <= "2026-07-31"
        assert submission.q_better.strip()
        assert submission.q_barrier.strip()


def test_profiles_are_not_all_the_same():
    submissions = generate(seed=42)
    intern_profiles = {
        tuple(sorted(submission.scores.items()))
        for submission in submissions
        if submission.role == "intern"
    }

    assert len(intern_profiles) > 1
    assert any(set(dict(profile).values()) != {5} for profile in intern_profiles)


def test_gap_story_keeps_four_to_six_points_of_spread():
    plans = {plan.intern_name: plan for plan in plan_pairs(seed=42, pairs=12)}
    by_pair: dict[str, dict[str, dict[str, object]]] = defaultdict(lambda: defaultdict(dict))
    for submission in generate(seed=42):
        by_pair[submission.intern_name][submission.month][submission.role] = submission

    for name, plan in plans.items():
        if plan.story != "gap":
            continue
        for month in MONTHS:
            intern = by_pair[name][month]["intern"].scores
            mentor = by_pair[name][month]["mentor"].scores
            for factor in ("clarity", "inclusion"):
                assert 4 <= abs(intern[factor] - mentor[factor]) <= 6, (name, month, factor)


def test_gap_and_signal_phrases_are_in_answers():
    plans = {plan.intern_name: plan for plan in plan_pairs(seed=42, pairs=12)}
    submissions = generate(seed=42)

    gap_intern = " ".join(
        submission.q_better + " " + submission.q_barrier
        for submission in submissions
        if plans[submission.intern_name].story == "gap" and submission.role == "intern"
    ).lower()
    gap_mentor = " ".join(
        submission.q_better + " " + submission.q_barrier
        for submission in submissions
        if plans[submission.intern_name].story == "gap" and submission.role == "mentor"
    ).lower()

    assert "не понимаю, что от меня хотят" in gap_intern
    assert "всё нормально, проблем не вижу" in gap_mentor

    for story in ("quiet_growth", "rescue"):
        text = " ".join(
            submission.q_better + " " + submission.q_barrier
            for submission in submissions
            if plans[submission.intern_name].story == story
        ).lower()
        for phrase in ("не вывожу нагрузку", "уже хожу по собеседованиям", "возьмут ли в штат"):
            assert phrase in text, (story, phrase)


def test_repeated_seed_is_idempotent_through_repo():
    init_db()
    # Уникальный префикс, чтобы не зависеть от строк, оставленных другими тестами.
    prefix = f"тест-генератор-{uuid.uuid4().hex[:8]}-"
    submissions = [
        replace(
            submission,
            intern_name=prefix + submission.intern_name,
            mentor_name=prefix + (submission.mentor_name or ""),
        )
        for submission in generate(seed=42)
    ]

    conn = connect()
    try:
        first = [upsert_submission(conn, submission)[1] for submission in submissions]
        conn.commit()
        second = [upsert_submission(conn, submission)[1] for submission in submissions]
        conn.commit()

        assert all(status == "created" for status in first)
        assert all(status == "duplicate" for status in second)

        names = sorted({submission.intern_name for submission in submissions})
        marks = ",".join("?" for _ in names)
        pair_count = conn.execute(
            f"SELECT COUNT(*) FROM pairs WHERE intern_name IN ({marks})", names
        ).fetchone()[0]
        assert pair_count == 12
    finally:
        conn.close()
