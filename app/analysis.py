"""Сборка карточки пары на месяц.

Числа берутся из app.scoring, формулировки — из app.llm, цитаты проверяются здесь.
Модель не может повлиять на риск: если она вернёт чужую цитату или неизвестный фактор,
это отбрасывается.
"""

from __future__ import annotations

import json
from typing import Any

from . import llm, scoring
from .normalize import FACTORS, FACTOR_LABELS, Submission

SIGNAL_LABELS = {
    "left_intent": "намерение уйти",
    "burnout": "выгорание",
    "offer_doubt": "сомнение в оффере",
    "conflict": "конфликт или изоляция",
}

STEP_TEMPLATES: dict[str, tuple[str, str, str]] = {
    "clarity": ("Согласовать письменный список задач и критерии готовности", "наставник", "2 недели"),
    "motivation": ("Показать, как задача связана с результатом команды", "наставник", "1 месяц"),
    "mentor": ("Ввести регулярную встречу с обратной связью", "HR", "2 недели"),
    "workload": ("Пересобрать приоритеты и снять лишние задачи", "наставник", "2 недели"),
    "inclusion": ("Включить в командные ритуалы и общий чат", "HR", "1 месяц"),
    "future": ("Проговорить критерии оффера и сроки решения", "HR", "1 месяц"),
}

REASON_TEMPLATES: dict[str, str] = {
    "clarity": "Задачи и критерии готовности понятны не полностью",
    "motivation": "Интерес к работе ниже, чем нужно для устойчивого темпа",
    "mentor": "Обратной связи и поддержки не хватает",
    "workload": "Нагрузка выше комфортной",
    "inclusion": "Включённость в команду недостаточная",
    "future": "Нет ясности по офферу и будущему в компании",
}

MIN_QUOTE_LEN = 8


def _norm(value: str) -> str:
    return " ".join((value or "").lower().replace("ё", "е").split())


def _quote_ok(quote: str, sources: list[str]) -> bool:
    needle = _norm(quote)
    if len(needle) < MIN_QUOTE_LEN:
        return False
    return any(needle in _norm(source) for source in sources)


def load_submissions(conn, pair_id: int, month: str) -> dict[str, Submission]:
    rows = conn.execute(
        "SELECT payload_json FROM submissions WHERE pair_id = ? AND month = ?",
        (pair_id, month),
    ).fetchall()
    result: dict[str, Submission] = {}
    for row in rows:
        payload = json.loads(row["payload_json"])
        submission = Submission(**payload)
        result[submission.role] = submission
    return result


def _text(submission: Submission | None) -> str:
    if submission is None:
        return ""
    return " ".join(filter(None, [submission.q_better, submission.q_barrier])).strip()


def _safe_extract(text: str, role: str, month: str, answers: dict[str, float]) -> tuple[Any, bool]:
    try:
        return llm.extract(text, role=role, month=month, answers=answers), False
    except llm.LLMError:
        return llm.Extraction(summary="", signals=[], quotes=[], flags=[]), True


def _safe_explain(context: dict[str, Any]) -> tuple[Any, bool]:
    try:
        return llm.explain(context), False
    except llm.LLMError:
        return llm.Explanation(summary="", reasons=[], steps=[], gap_comment=""), True


def _fallback_steps(factors: list[scoring.Factor], used: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for factor in sorted(factors, key=lambda item: item.points, reverse=True):
        if len(result) >= 2:
            break
        if factor.key in used:
            continue
        text, owner, due = STEP_TEMPLATES.get(
            factor.key, ("Обсудить ситуацию с наставником и HR", "HR", "2 недели")
        )
        result.append(
            {
                "key": factor.key,
                "text": text,
                "reduction": scoring.MAX_REDUCTION.get(factor.key, 4.0),
                "owner": owner,
                "due": due,
            }
        )
    return result


def build_core(conn, pair_id: int, month: str) -> dict[str, Any] | None:
    """Считает всё, что зависит только от этой пары и месяца. Без истории и сценариев."""
    submissions = load_submissions(conn, pair_id, month)
    if not submissions:
        return None

    intern = submissions.get("intern")
    mentor = submissions.get("mentor")
    intern_scores = dict(intern.scores) if intern else {}
    mentor_scores = dict(mentor.scores) if mentor else None
    origin = (intern or mentor).origin  # type: ignore[union-attr]

    degraded = False
    extractions: dict[str, llm.Extraction] = {}
    texts = {"intern": _text(intern), "mentor": _text(mentor)}
    for role in ("intern", "mentor"):
        submission = submissions.get(role)
        if submission is None:
            continue
        extraction, failed = _safe_extract(texts[role], role, month, submission.scores)
        extractions[role] = extraction
        degraded = degraded or failed

    sources = [texts["intern"], texts["mentor"]]

    # Сигналы: только с подтверждённой цитатой, без повторов, с доверием не ниже 0.5.
    signals: list[dict[str, Any]] = []
    seen: set[str] = set()
    for role in ("intern", "mentor"):
        for raw in extractions.get(role, llm.Extraction("", [], [], [])).signals:
            key = str(raw.get("key", ""))
            quote = str(raw.get("quote", ""))
            try:
                confidence = float(raw.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            if key not in SIGNAL_LABELS or key in seen:
                continue
            if confidence < 0.5 or not _quote_ok(quote, sources):
                continue
            seen.add(key)
            signals.append(
                {
                    "key": key,
                    "label": SIGNAL_LABELS[key],
                    "quote": quote,
                    "points": scoring.TEXT_SIGNALS.get(key, 0.0),
                }
            )

    # Цитаты по факторам: первая валидная от любой стороны.
    factor_quotes: dict[str, str] = {}
    for role in ("intern", "mentor"):
        for raw in extractions.get(role, llm.Extraction("", [], [], [])).quotes:
            factor = str(raw.get("factor", ""))
            quote = str(raw.get("quote", ""))
            if factor in FACTORS and factor not in factor_quotes and _quote_ok(quote, sources):
                factor_quotes[factor] = quote

    factors = scoring.evaluate(intern_scores, mentor_scores, origin)
    if not factors:
        return None

    # Вклады показываем целыми: на экране они должны складываться в индекс без
    # «плюс-минус полтора». Риск считаем из тех же чисел, что видит человек.
    factor_points = [int(round(factor.points)) for factor in factors]
    base_total = sum(factor_points)
    gap_pen = int(round(scoring.gap_penalty(factors)))
    text_pen = int(round(scoring.text_penalty([{"key": s["key"]} for s in signals])))
    risk_value = max(0, min(100, base_total + gap_pen + text_pen))

    factor_rows = [
        {
            "key": factor.key,
            "label": FACTOR_LABELS.get(factor.key, factor.key),
            "points": points,
            "score_intern": factor.score_intern,
            "score_mentor": factor.score_mentor,
            "gap": factor.gap,
            "origin": factor.origin,
            "quote": factor_quotes.get(factor.key, ""),
        }
        for factor, points in zip(factors, factor_points)
    ]

    context = {
        "intern_name": (intern or mentor).intern_name,  # type: ignore[union-attr]
        "month": month,
        "factors": factor_rows,
        "signals": signals,
        "intern_text": texts["intern"],
        "mentor_text": texts["mentor"],
    }
    explanation, explain_failed = _safe_explain(context)
    degraded = degraded or explain_failed

    # Причины: до трёх по очкам. Текст модели — только если ключ существует.
    model_text = {
        str(item.get("key")): str(item.get("text", ""))
        for item in explanation.reasons
        if str(item.get("key")) in FACTORS
    }
    reasons = []
    for factor in sorted(factors, key=lambda item: item.points, reverse=True)[:3]:
        reasons.append(
            {
                "key": factor.key,
                "label": FACTOR_LABELS.get(factor.key, factor.key),
                "points": int(round(factor.points)),
                "text": model_text.get(factor.key) or REASON_TEMPLATES.get(factor.key, ""),
                "quote": factor_quotes.get(factor.key, ""),
            }
        )

    # Шаги: только по трём самым проблемным факторам, эффект из таблицы,
    # суммарно не больше STEP_CAP. Предложение модели по благополучному фактору
    # отбрасывается: советовать «улучшить включённость» при риске в мотивации глупо.
    worst_keys = {
        factor.key
        for factor in sorted(factors, key=lambda item: item.points, reverse=True)[:3]
    }
    steps: list[dict[str, Any]] = []
    used: set[str] = set()
    total_reduction = 0.0
    for raw in explanation.steps:
        key = str(raw.get("key", ""))
        if key not in worst_keys or key in used:
            continue
        reduction = float(scoring.MAX_REDUCTION.get(key, 0))
        if reduction <= 0:
            continue
        if total_reduction + reduction > scoring.STEP_CAP:
            reduction = max(0.0, scoring.STEP_CAP - total_reduction)
            if reduction <= 0:
                continue
        total_reduction += reduction
        used.add(key)
        steps.append(
            {
                "key": key,
                "text": str(raw.get("text", "")).strip() or STEP_TEMPLATES.get(key, ("", "", ""))[0],
                "reduction": round(reduction, 1),
                "owner": str(raw.get("owner", "")).strip() or STEP_TEMPLATES.get(key, ("", "HR", ""))[1],
                "due": str(raw.get("due", "")).strip() or STEP_TEMPLATES.get(key, ("", "", "2 недели"))[2],
            }
        )
        if len(steps) >= 3:
            break

    if len(steps) < 2:
        for extra in _fallback_steps(factors, used):
            if total_reduction + extra["reduction"] > scoring.STEP_CAP:
                continue
            total_reduction += extra["reduction"]
            used.add(extra["key"])
            steps.append(extra)
            if len(steps) >= 3:
                break

    gap_comment = explanation.gap_comment.strip()
    if not gap_comment and gap_pen > 0:
        worst = max(factors, key=lambda item: item.gap or 0)
        gap_comment = (
            f"Стажёр и наставник по-разному видят «{FACTOR_LABELS.get(worst.key, worst.key)}»: "
            f"{worst.score_intern} против {worst.score_mentor}."
        )

    return {
        "risk": risk_value,
        "base": base_total,
        "gap_penalty": gap_pen,
        "text_penalty": text_pen,
        "band": scoring.band(risk_value),
        "summary": explanation.summary.strip(),
        "gap_comment": gap_comment,
        "factors": factor_rows,
        "reasons": reasons,
        "signals": signals,
        "steps": steps,
        "steps_reduction": round(total_reduction, 1),
        "degraded": degraded,
        "originals": {
            role: {"received_at": submission.received_at, "source": submission.source}
            for role, submission in submissions.items()
        },
        "observation": {
            "months": len({month}),
            "with_both_forms": 1 if intern and mentor else 0,
        },
        "texts": {"intern": texts["intern"], "mentor": texts["mentor"]},
    }
