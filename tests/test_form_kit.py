"""Мок-набор ответов формы: целостность данных и сухой прогон заливки."""

import csv
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "fixtures" / "form-kit"
SCORES = ("Мотивация", "Ясность", "Наставник", "Нагрузка", "Включённость", "Будущее")


def _rows():
    with (KIT / "answers.csv").open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def test_kit_has_five_people_six_months_two_roles():
    rows = _rows()
    assert len(rows) == 60

    people = Counter(row["ФИО"] for row in rows)
    assert len(people) == 5
    assert set(people.values()) == {12}

    for name in people:
        months = {row["Месяц"] for row in rows if row["ФИО"] == name}
        roles = {row["Роль"] for row in rows if row["ФИО"] == name}
        assert months == {f"M{i}" for i in range(1, 7)}, name
        assert roles == {"intern", "mentor"}, name


def test_scores_and_texts_are_usable():
    for row in _rows():
        for column in SCORES:
            value = int(row[column])
            assert 1 <= value <= 10, (row["ФИО"], row["Месяц"], column, value)
        assert row["Что улучшилось"].strip(), row
        assert row["Что мешает"].strip(), row


def test_manual_test_covers_both_roles():
    text = (KIT / "manual-test.md").read_text(encoding="utf-8")
    assert "Петров Алексей" in text
    assert "## Стажёр" in text
    assert "## Наставник" in text


def test_replay_dry_run_builds_payload():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_form_kit.py"), "--dry-run", "--limit", "1"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert '"s_name": "Иванова Мария"' in result.stdout
    assert '"f_clarity"' in result.stdout
    assert '"q_barrier"' in result.stdout
