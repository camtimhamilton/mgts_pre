"""Чистая арифметика индекса риска и сценариев.

Ни БД, ни сети, ни LLM: только числа и dataclass. Все пороги — модульные
константы, чтобы на финале крутить их в одном месте, не трогая формулу.
"""

from __future__ import annotations

from dataclasses import dataclass

from .normalize import FACTORS, MONTHS

# Веса факторов: сумма ровно 100.
WEIGHTS: dict[str, float] = {
    "motivation": 25.0,
    "clarity": 20.0,
    "mentor": 20.0,
    "workload": 15.0,
    "inclusion": 10.0,
    "future": 10.0,
}

# Максимальный эффект шага по фактору; сумма шагов ограничена STEP_CAP.
MAX_REDUCTION: dict[str, float] = {
    "motivation": 10.0,
    "clarity": 8.0,
    "mentor": 8.0,
    "future": 7.0,
    "workload": 6.0,
    "inclusion": 5.0,
}

# Надбавки за текстовые сигналы; повтор одного ключа считается один раз.
TEXT_SIGNALS: dict[str, float] = {
    "left_intent": 8.0,
    "burnout": 5.0,
    "offer_doubt": 5.0,
    "conflict": 4.0,
}

TEXT_CAP: float = 12.0
GAP_MIN: float = 2.0
GAP_STEP: float = 4.0
GAP_CAP: float = 20.0
STEP_CAP: float = 25.0
BAND_LOW: float = 34.0
BAND_HIGH: float = 60.0

# Позиция месяца в шестимесячной программе: M1 → 0, M6 → 5.
_MONTH_POSITION: dict[str, int] = {month: index for index, month in enumerate(MONTHS)}


@dataclass(frozen=True)
class Factor:
    """Вклад одного фактора в риск и обе оценки, из которых он получен."""

    key: str
    points: float
    score_intern: float | None
    score_mentor: float | None
    gap: float | None
    origin: str


def _score(value: float | int | None) -> float | None:
    """Оценка 0–10; None означает, что сторона этот фактор не оценивала."""
    if value is None:
        return None
    return max(0.0, min(10.0, float(value)))


def _clamp100(value: float) -> float:
    return max(0.0, min(100.0, value))


def evaluate(
    intern: dict[str, float],
    mentor: dict[str, float] | None = None,
    origin: str = "answer",
) -> list[Factor]:
    """Среднее по доступным сторонам, вклады по весам.

    Фактор без оценок пропускается. Разрыв считается только там, где есть
    обе стороны; при одной стороне перенормировка не нужна — веса уже дают
    шкалу 0..100.
    """
    left_scores = intern or {}
    right_scores = mentor or {}
    factors: list[Factor] = []

    for key in FACTORS:
        left = _score(left_scores.get(key))
        right = _score(right_scores.get(key))

        available = [score for score in (left, right) if score is not None]
        if not available:
            continue

        score = sum(available) / len(available)
        gap = abs(left - right) if left is not None and right is not None else None
        factors.append(
            Factor(
                key=key,
                points=(10.0 - score) * WEIGHTS[key] / 10.0,
                score_intern=left,
                score_mentor=right,
                gap=gap,
                origin=origin,
            )
        )

    return factors


def gap_penalty(factors: list[Factor]) -> float:
    """Надбавка за максимальный разрыв восприятия: шаг 4, потолок 20."""
    max_gap = max((factor.gap for factor in factors if factor.gap is not None), default=0.0)
    return max(0.0, min(GAP_CAP, (max_gap - GAP_MIN) * GAP_STEP))


def text_penalty(signals: list[dict]) -> float:
    """Сумма весов уникальных сигналов, но не больше TEXT_CAP."""
    seen: set[str] = set()
    total = 0.0

    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        key = signal.get("key")
        if not isinstance(key, str) or key in seen:
            continue
        seen.add(key)
        # Неизвестный ключ весит 0, но в seen попадает — на результат не влияет.
        total += TEXT_SIGNALS.get(key, 0.0)

    return min(TEXT_CAP, total)


def risk(factors: list[Factor], gap: float, text: float) -> int:
    """Округлённая и обрезанная сумма вкладов, надбавки за разрыв и текста."""
    total = sum((factor.points for factor in factors), 0.0) + gap + text
    return int(round(_clamp100(total)))


def band(value: int) -> str:
    """Полоса индекса: 0–34 low, 35–59 watch, 60–100 critical."""
    if value <= BAND_LOW:
        return "low"
    if value < BAND_HIGH:
        return "watch"
    return "critical"


def urgency(risk_value: int, delta: int) -> float:
    """Срочность: рост повышает приоритет, падение — нет."""
    return risk_value + 1.5 * max(0, delta)


def _scenario_point(month: str, risk_value: int, width: float) -> dict:
    return {
        "month": month,
        "risk": risk_value,
        "low": int(round(max(0.0, risk_value - width))),
        "high": int(round(min(100.0, risk_value + width))),
    }


def scenario(
    history: list[tuple[str, int]],
    reductions: float,
    horizon: int = 1,
) -> dict:
    """Сценарий на будущие месяцы: базовый и с выполненными шагами.

    Наклон — линейная регрессия по последним ≤3 точкам, затухание 0.7 на шаг.
    Коридор ±max(6, 12 − 2n), всё обрезается в 0..100. При n < 2 линия плоская.
    Если следующего месяца нет в M1..M6 — возвращаем пустые списки: за пределы
    программы не прогнозируем. Горизонт, упирающийся в M6, укорачивается.
    """
    empty = {"base": [], "with_actions": [], "event": None}
    if not history or horizon <= 0:
        return empty

    current_month = str(history[-1][0]).strip().upper()
    position = _MONTH_POSITION.get(current_month)
    if position is None or position >= len(MONTHS) - 1:
        return empty

    steps = min(int(horizon), len(MONTHS) - 1 - position)
    if steps <= 0:
        return empty

    points = list(history[-3:])
    n = len(points)
    current_risk = float(points[-1][1])

    slope = 0.0
    if n >= 2:
        mean_x = (n - 1) / 2.0
        mean_y = sum(float(value) for _, value in points) / n
        denominator = sum((x - mean_x) ** 2 for x in range(n))
        slope = sum(
            (x - mean_x) * (float(value) - mean_y)
            for x, (_, value) in enumerate(points)
        ) / denominator

    width = max(6.0, 12.0 - 2.0 * n)
    applied = max(0.0, min(float(reductions), STEP_CAP))
    base: list[dict] = []
    with_actions: list[dict] = []
    event: str | None = None

    for step in range(1, steps + 1):
        month = MONTHS[position + step]
        base_raw = _clamp100(current_risk + slope * step * 0.7 ** (step - 1))
        base_risk = int(round(base_raw))
        action_risk = int(round(_clamp100(base_raw - applied)))

        base.append(_scenario_point(month, base_risk, width))
        with_actions.append(_scenario_point(month, action_risk, width))

        # Красная зона начинается с BAND_HIGH (60), поэтому событие — не «строго выше».
        if event is None and base_risk >= BAND_HIGH:
            event = f"красная зона к {month}"

    return {"base": base, "with_actions": with_actions, "event": event}
