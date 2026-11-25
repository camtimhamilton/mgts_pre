"""Канонический вид анкеты.

Единственное представление, с которым работает расчёт. Всё, что приходит из формы,
из файла или из вебхука, сначала приводится к `Submission`, и только потом попадает
в базу. Так импорт, живой приём и генератор демо-данных не расходятся.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

FACTORS: tuple[str, ...] = ("motivation", "clarity", "mentor", "workload", "inclusion", "future")
MONTHS: tuple[str, ...] = tuple(f"M{i}" for i in range(1, 7))
ROLES: tuple[str, ...] = ("intern", "mentor")

FACTOR_LABELS: dict[str, str] = {
    "motivation": "Мотивация",
    "clarity": "Ясность задач",
    "mentor": "Наставник",
    "workload": "Нагрузка",
    "inclusion": "Включённость",
    "future": "Будущее",
}

ORIGINS = ("answer", "mapped", "estimated")


@dataclass
class Submission:
    intern_name: str
    role: str
    month: str
    scores: dict[str, float] = field(default_factory=dict)
    mentor_name: str | None = None
    direction: str | None = None
    origin: str = "answer"
    q_better: str = ""
    q_barrier: str = ""
    source: str = "unknown"
    external_id: str | None = None
    received_at: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def content_hash(submission: Submission) -> str:
    """Стабильный хэш содержимого: по нему отличаем дубль от правки."""
    payload = {
        "intern_name": submission.intern_name,
        "mentor_name": submission.mentor_name,
        "direction": submission.direction,
        "role": submission.role,
        "month": submission.month,
        "scores": {k: round(float(v), 2) for k, v in sorted(submission.scores.items())},
        "origin": submission.origin,
        "q_better": submission.q_better.strip(),
        "q_barrier": submission.q_barrier.strip(),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


_MONTH_RE = re.compile(r"(?:m|м|месяц|month)\s*[-_ ]?\s*([1-6])", re.IGNORECASE)


def parse_month(value: object) -> str | None:
    """Принимает M3, м3, 3, «Месяц 3», «month 3», 3.0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        return f"M{number}" if 1 <= number <= 6 else None

    text = str(value).strip().lower().replace("ё", "е")
    if not text:
        return None

    match = _MONTH_RE.search(text)
    if match:
        return f"M{match.group(1)}"
    if re.fullmatch(r"[1-6]", text):
        return f"M{text}"
    return None


_ROLE_WORDS = (
    ("mentor", ("наставник", "mentor", "наставники")),
    ("intern", ("стажер", "intern", "trainee", "стажеры")),
)


def parse_role(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower().replace("ё", "е")
    if not text:
        return None
    for role, words in _ROLE_WORDS:
        if any(word in text for word in words):
            return role
    return None


_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_score(value: object) -> float | None:
    """Оценка 0–10. Терпит 7, «7», «7,5», «7 из 10», «70%» (шкала 0–100 сжимается)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        score = float(value)
    else:
        text = str(value).strip()
        if not text:
            return None
        match = _NUMBER_RE.search(text)
        if not match:
            return None
        score = float(match.group().replace(",", "."))

    if 10 < score <= 100:
        score = score / 10
    return max(0.0, min(10.0, score))


def _pick(mapping: dict, *keys: str) -> object | None:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def from_payload(payload: dict, source: str = "form", origin: str = "answer") -> Submission:
    """Приводит к каноническому виду что угодно: нашу форму, JSON-RPC Яндекса, строку импорта.

    Понимает оба способа укладки полей: плоский (`f_clarity`) и вложенный
    (`answers.f_clarity`), потому что форма и импорт укладывают их по-разному.
    """
    raw: dict = dict(payload or {})
    answers = raw.get("answers")
    if isinstance(answers, dict):
        raw.update(answers)

    open_block = raw.get("open")
    if not isinstance(open_block, dict):
        open_block = {}

    scores: dict[str, float] = {}
    for key in FACTORS:
        parsed = parse_score(raw.get(f"f_{key}", raw.get(key)))
        if parsed is not None:
            scores[key] = parsed

    role = parse_role(_pick(raw, "role", "s_role", "respondent_type")) or "intern"
    month = parse_month(_pick(raw, "month", "s_month", "period", "месяц"))
    if month is None:
        raise ValueError("не удалось определить месяц: ожидается M1…M6")

    intern_name = _pick(raw, "intern_name", "s_name", "name", "фио", "стажер")
    if not intern_name:
        raise ValueError("не удалось определить имя стажёра")

    return Submission(
        intern_name=str(intern_name).strip(),
        mentor_name=_optional(_pick(raw, "mentor_name", "s_mentor", "mentor", "наставник")),
        direction=_optional(_pick(raw, "direction", "s_direction", "направление")),
        role=role,
        month=month,
        scores=scores,
        origin=origin if origin in ORIGINS else "answer",
        q_better=str(_pick(open_block, "q_better", "better") or _pick(raw, "q_better") or "").strip(),
        q_barrier=str(_pick(open_block, "q_barrier", "barrier") or _pick(raw, "q_barrier") or "").strip(),
        source=source,
        external_id=_optional(raw.get("external_id")),
        received_at=_optional(_pick(raw, "submitted_at", "received_at")) or _now(),
    )


def normalize_name(value: object) -> str:
    """Имя из формы: убираем лишние пробелы, но не трогаем регистр для показа."""
    return " ".join(str(value or "").split()).strip()


def name_key(value: object) -> str:
    """Ключ сравнения имён: без регистра, ё и лишних пробелов.

    Нужен, чтобы «Иванов Пётр», «иванов петр» и «Иванов  Пётр» были одной парой.
    """
    return normalize_name(value).casefold().replace("ё", "е")


def _optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
