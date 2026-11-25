"""LLM-клиент фазы 2: текстовые сигналы и объяснение расчёта.

Модель отвечает только за формулировки и цитаты. Числа (`points`, `reduction`,
`risk`) считает `app/scoring.py`, поэтому здесь их нет ни в промптах, ни в
результате.

Два провайдера за одним интерфейсом:

* DeepSeek — OpenAI-совместимый `/chat/completions`;
* GigaChat — OAuth Сбера, затем `/api/v1/chat/completions`.

Режим задаёт `LLM_MODE`: ``fake`` работает без сети и детерминирован, ``real``
ходит к провайдеру, ``auto`` выбирает ``real`` только при непустом ключе.
Кэш общий для ``extract`` и ``explain``: ключ — SHA-256 от режима, провайдера,
модели, версии промпта и канонического JSON входа.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .config import Settings, get_settings
from .db import connect, write_lock
from .normalize import FACTORS, FACTOR_LABELS

# Версия промптов входит в ключ кэша: после правки формулировок старые ответы
# перестают использоваться, хотя входной текст не изменился.
PROMPT_VERSION = "2026-11-25.1"

DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com"
DEFAULT_GIGACHAT_URL = "https://gigachat.devices.sberbank.ru"
GIGACHAT_OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
GIGACHAT_SCOPE = "GIGACHAT_API_PERS"

DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "gigachat": "GigaChat",
}

SIGNAL_KEYS: tuple[str, ...] = ("left_intent", "burnout", "offer_doubt", "conflict")

SIGNAL_LABELS: dict[str, str] = {
    "left_intent": "намерение уйти",
    "burnout": "выгорание",
    "offer_doubt": "сомнение в оффере",
    "conflict": "конфликт или изоляция",
}

SIGNAL_FLAGS: dict[str, str] = {
    "left_intent": "риск ухода",
    "burnout": "риск выгорания",
    "offer_doubt": "сомнение в будущем",
    "conflict": "конфликт или изоляция",
}

SIGNAL_CONFIDENCE: dict[str, float] = {
    "left_intent": 0.9,
    "burnout": 0.8,
    "offer_doubt": 0.75,
    "conflict": 0.8,
}

# Кому отдать шаг: «наставник» или «HR» — только эти два значения разрешены.
FACTOR_OWNER: dict[str, str] = {
    "motivation": "HR",
    "clarity": "наставник",
    "mentor": "наставник",
    "workload": "HR",
    "inclusion": "HR",
    "future": "HR",
}

FACTOR_STEP_TEXT: dict[str, str] = {
    "motivation": (
        "Обсудить со стажёром, какие задачи ему интересны, и добавить одну такую "
        "задачу в план на ближайший месяц."
    ),
    "clarity": "Зафиксировать список задач и критерии готовности, чтобы ожидания сторон совпали.",
    "mentor": (
        "Ввести регулярную обратную связь с наставником: короткая встреча раз в неделю "
        "и разбор того, что получается, а что нет."
    ),
    "workload": "Пересмотреть нагрузку: убрать необязательные задачи и согласовать реалистичный срок.",
    "inclusion": "Включить стажёра в командные ритуалы и дать ему небольшую роль в общей задаче.",
    "future": "Проговорить перспективу и критерии оффера, чтобы у стажёра была понятная картина будущего.",
}

# Ключевые фразы fake-режима. Порядок фиксирован: от него зависит порядок сигналов
# и, значит, детерминированность ответа.
_FAKE_SIGNAL_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("left_intent", ("собеседован", "другую работу")),
    ("burnout", ("не вывожу", "устал", "переработ")),
    ("offer_doubt", ("не знаю, возьмут", "не знаю возьмут", "оффер")),
    ("conflict", ("конфликт", "не обща", "игнор")),
)

_FAKE_QUOTE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("clarity", ("не понимаю",)),
    ("mentor", ("наставник", "обратн")),
    ("workload", ("нагрузк", "успева")),
    ("inclusion", ("команд", "включ")),
    ("future", ("оффер", "штат", "будущ")),
    ("motivation", ("интерес", "мотивац")),
)

_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT    NOT NULL UNIQUE,
    result_json TEXT    NOT NULL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
)
"""


class LLMError(RuntimeError):
    """Провайдер недоступен или вернул ответ, не проходящий проверку схемы.

    Вызывающий сам решает, деградировать до шаблонов или показать баннер:
    ``app/analysis.py`` ловит это исключение и помечает снапшот как degraded.
    """


@dataclass
class Extraction:
    summary: str
    signals: list[dict]      # {"key": str, "quote": str, "confidence": float}
    quotes: list[dict]       # {"factor": str, "quote": str}
    flags: list[str]


@dataclass
class Explanation:
    summary: str
    reasons: list[dict]      # {"key": str, "text": str, "quote": str}
    steps: list[dict]        # {"key": str, "text": str, "owner": str, "due": str}
    gap_comment: str


# ---------------------------------------------------------------- настройки


def _settings() -> Settings:
    """Единственная точка чтения настроек: тестам достаточно подменить её."""
    return get_settings()


def _resolve_mode(settings: Settings) -> str:
    """auto → real только при непустом ключе, иначе fake (демо живёт без сети)."""
    mode = str(getattr(settings, "llm_mode", "auto") or "auto").strip().lower()
    if mode == "fake":
        return "fake"
    if mode == "real":
        return "real"
    key = str(getattr(settings, "llm_api_key", "") or "").strip()
    return "real" if key else "fake"


def _provider(settings: Settings) -> str:
    provider = str(getattr(settings, "llm_provider", "") or "deepseek").strip().lower()
    # Неизвестное значение не должно ронять дашборд: безопасный дефолт — DeepSeek.
    return provider if provider in DEFAULT_MODELS else "deepseek"


def _model_name(provider: str, settings: Settings) -> str:
    explicit = str(getattr(settings, "model_name", "") or "").strip()
    return explicit or DEFAULT_MODELS[provider]


def provider_name() -> str:
    """Имя провайдера для снапшота: ``fake`` либо ``deepseek``/``gigachat``."""
    settings = _settings()
    if _resolve_mode(settings) == "fake":
        return "fake"
    return _provider(settings)


# ---------------------------------------------------------------- утилиты


def _collapse(text: object) -> str:
    """Схлопывает любые пробелы: так сравниваются цитаты после переносов строк."""
    return " ".join(str(text or "").split())


def _quote_in_text(quote: object, text: object) -> bool:
    """Дословная подстрока после нормализации пробелов (регистр сохраняем)."""
    needle = _collapse(quote)
    return bool(needle) and needle in _collapse(text)


def _num(value: object) -> str:
    """Число для шаблонной фразы: 12 → «12», 6.5 → «6.5»."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}".rstrip("0").rstrip(".")


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", "."))
    except ValueError:
        return None


def _clean_answers(answers: object) -> dict[str, float]:
    """Оставляет только факторы из FACTORS и приводит значения к float."""
    result: dict[str, float] = {}
    if not isinstance(answers, dict):
        return result
    for key, value in answers.items():
        if key not in FACTORS:
            continue
        number = _as_float(value)
        if number is not None:
            result[str(key)] = round(number, 2)
    return result


_SENTENCE_END = ".!?…\n"


def _sentence_at(text: str, start: int, end: int) -> str:
    """Возвращает предложение вокруг совпадения — дословный срез исходного текста."""
    left = start
    while left > 0 and text[left - 1] not in _SENTENCE_END:
        left -= 1
    right = end
    while right < len(text) and text[right] not in _SENTENCE_END:
        right += 1
    if right < len(text):
        right += 1
    return text[left:right].strip()


def _find_phrase(text: str, words: tuple[str, ...]) -> tuple[str, str] | None:
    """Ищет самую раннюю из фраз и возвращает (предложение, найденная фраза).

    Регистр и «ё» нормализуются; срез берётся из оригинального текста, поэтому
    цитата остаётся его точной подстрокой.
    """
    folded = text.lower().replace("ё", "е")
    best: tuple[int, int] | None = None
    for word in words:
        position = folded.find(word)
        if position < 0:
            continue
        if best is None or position < best[0]:
            best = (position, position + len(word))
    if best is None:
        return None
    return _sentence_at(text, best[0], best[1]), folded[best[0] : best[1]]


def _max_gap(context: dict) -> tuple[float, dict] | None:
    """Максимальный по модулю разрыв и фактор, к которому он относится."""
    best: tuple[float, dict] | None = None
    factors = context.get("factors")
    if not isinstance(factors, list):
        return None
    for factor in factors:
        if not isinstance(factor, dict):
            continue
        gap = _as_float(factor.get("gap"))
        if gap is None:
            continue
        gap = abs(gap)
        if best is None or gap > best[0]:
            best = (gap, factor)
    return best


def _context_source(context: dict) -> str:
    """Тексты обеих сторон: по ним проверяются цитаты в explain."""
    parts = []
    for key in ("intern_text", "mentor_text"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    return "\n".join(parts)


# ---------------------------------------------------------------- fake


def _fake_signals(text: str) -> list[dict]:
    signals: list[dict] = []
    for key, words in _FAKE_SIGNAL_RULES:
        found = _find_phrase(text, words)
        if found is None:
            continue
        quote = found[0]
        if not _quote_in_text(quote, text):
            continue
        signals.append(
            {"key": key, "quote": quote, "confidence": SIGNAL_CONFIDENCE[key]}
        )
    return signals


def _fake_quotes(text: str) -> list[dict]:
    quotes: list[dict] = []
    for factor, words in _FAKE_QUOTE_RULES:
        found = _find_phrase(text, words)
        if found is None:
            continue
        quote = found[0]
        if not _quote_in_text(quote, text):
            continue
        quotes.append({"factor": factor, "quote": quote})
    return quotes


def _fake_extract(
    text: str, role: str, month: str, answers: dict[str, float] | None
) -> Extraction:
    """Детерминированная эвристика: только фразы из текста, без выдуманных чисел."""
    signals = _fake_signals(text)
    quotes = _fake_quotes(text)

    flags = [SIGNAL_FLAGS[item["key"]] for item in signals]
    if not text.strip():
        flags.append("текст не заполнен")
    elif not signals and not quotes:
        flags.append("явных сигналов в тексте нет")

    parts: list[str] = []
    scored: list[tuple[str, float]] = []
    for key, value in (answers or {}).items():
        number = _as_float(value)
        if key in FACTORS and number is not None:
            scored.append((str(key), number))
    if scored:
        key, score = min(scored, key=lambda item: (item[1], FACTORS.index(item[0])))
        parts.append(f"Самый слабый фактор — «{FACTOR_LABELS[key]}»: {_num(score)} из 10.")
    else:
        parts.append(f"Ответ {role or 'без роли'} за {month or 'без месяца'}: числовых оценок нет.")

    if signals:
        labels = ", ".join(SIGNAL_LABELS[item["key"]] for item in signals)
        parts.append(f"В тексте есть сигналы: {labels}.")
    elif text.strip():
        parts.append("Явных текстовых сигналов в ответе не найдено.")
    else:
        parts.append("Текст ответа пустой — разбирать нечего.")

    return Extraction(summary=" ".join(parts), signals=signals, quotes=quotes, flags=flags)


def _normalized_factor(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key", ""))
    if key not in FACTORS:
        return None
    points = _as_float(raw.get("points"))
    return {
        "key": key,
        "label": str(raw.get("label") or FACTOR_LABELS[key]),
        "points": points if points is not None else 0.0,
        "score_intern": _as_float(raw.get("score_intern")),
        "score_mentor": _as_float(raw.get("score_mentor")),
        "gap": _as_float(raw.get("gap")),
        "quote": str(raw.get("quote") or ""),
    }


def _reason_text(factor: dict) -> str:
    pieces = [f"«{factor['label']}» — вклад {_num(factor['points'])} п.п. в риск"]
    intern = factor.get("score_intern")
    mentor = factor.get("score_mentor")
    if intern is not None and mentor is not None:
        pieces.append(
            f"стажёр {_num(intern)} из 10, наставник {_num(mentor)} из 10, "
            f"разрыв {_num(factor.get('gap'))}"
        )
    elif intern is not None:
        pieces.append(f"оценка стажёра {_num(intern)} из 10")
    elif mentor is not None:
        pieces.append(f"оценка наставника {_num(mentor)} из 10")
    return ", ".join(pieces) + "."


def _gap_comment(context: dict) -> str:
    best = _max_gap(context)
    if best is None or best[0] < 4:
        return ""
    gap, factor = best
    label = str(factor.get("label") or FACTOR_LABELS.get(str(factor.get("key")), "фактор"))
    intern = _as_float(factor.get("score_intern"))
    mentor = _as_float(factor.get("score_mentor"))
    if intern is not None and mentor is not None:
        return (
            f"Расхождение по фактору «{label}»: стажёр {_num(intern)} из 10, "
            f"наставник {_num(mentor)} из 10, разрыв {_num(gap)} п. — "
            "это сигнал сверить ожидания на ближайшей встрече."
        )
    return (
        f"Расхождение по фактору «{label}» — {_num(gap)} п.; оценки сторон "
        "расходятся, это повод сверить ожидания."
    )


def _fake_explain(context: dict) -> Explanation:
    raw_factors = context.get("factors")
    factors: list[dict] = []
    if isinstance(raw_factors, list):
        for raw in raw_factors:
            factor = _normalized_factor(raw)
            if factor is not None:
                factors.append(factor)

    ordered = sorted(factors, key=lambda item: (-item["points"], FACTORS.index(item["key"])))
    reasons: list[dict] = []
    for factor in ordered[:3]:
        # Цитата уже проверена при extract и пришла из контекста: не перепроверяем
        # её здесь, чтобы не потерять валидную фразу из-за нормализации.
        quote = factor.get("quote") or ""
        reasons.append(
            {"key": factor["key"], "text": _reason_text(factor), "quote": str(quote)}
        )

    # Самые слабые факторы — наименьшие points. Если факторов меньше двух,
    # добираем допустимым ключом из FACTORS, чтобы шагов всегда было 2–3.
    weakest = sorted(factors, key=lambda item: (item["points"], FACTORS.index(item["key"])))
    chosen = [factor["key"] for factor in weakest[:3]]
    for fallback in ("clarity", "mentor", "inclusion"):
        if len(chosen) >= 2:
            break
        if fallback not in chosen:
            chosen.append(fallback)
    chosen = chosen[:3]

    steps: list[dict] = []
    for index, key in enumerate(chosen):
        steps.append(
            {
                "key": key,
                "text": FACTOR_STEP_TEXT[key],
                "owner": FACTOR_OWNER[key],
                "due": "2 недели" if index == 0 else "1 месяц",
            }
        )

    gap_comment = _gap_comment(context)
    name = str(context.get("intern_name") or "Стажёр")
    month = str(context.get("month") or "—")
    if reasons:
        summary = f"{name}, {month}: главный вклад в риск даёт «{FACTOR_LABELS[reasons[0]['key']]}»."
    else:
        summary = f"{name}, {month}: факторов для объяснения не передано."

    signal_labels = []
    raw_signals = context.get("signals")
    if isinstance(raw_signals, list):
        for item in raw_signals:
            if isinstance(item, dict) and item.get("key") in SIGNAL_LABELS:
                signal_labels.append(SIGNAL_LABELS[str(item["key"])])
    if signal_labels:
        summary += f" В тексте отмечен сигнал: {', '.join(signal_labels)}."

    return Explanation(
        summary=summary, reasons=reasons, steps=steps, gap_comment=gap_comment
    )


# ---------------------------------------------------------------- кэш


def _cache_key(kind: str, payload: dict) -> str:
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def _prepare(
    kind: str, settings: Settings, input_payload: dict
) -> tuple[str, str, str, str]:
    """Считает режим, провайдера, модель и ключ кэша для конкретного запроса."""
    mode = _resolve_mode(settings)
    provider = _provider(settings) if mode == "real" else "fake"
    model = _model_name(provider, settings) if mode == "real" else "fake"
    key = _cache_key(
        kind,
        {
            "kind": kind,
            "mode": mode,
            "provider": provider,
            "model": model,
            "prompt_version": PROMPT_VERSION,
            "input": input_payload,
        },
    )
    return mode, provider, model, key


def _create_cache_table(conn: sqlite3.Connection) -> None:
    """Создаёт таблицу, только если её нет.

    На каждом чтении DDL не выполняем: `CREATE TABLE` берёт блокировку записи
    и может подраться с фоновым пересчётом, который идёт сразу после импорта.
    """
    conn.execute(_CACHE_DDL)
    conn.commit()


def _is_missing_table(exc: sqlite3.Error) -> bool:
    return "no such table" in str(exc).lower()


def _cache_get(key: str) -> Any | None:
    """Читает кэш; любые проблемы с БД — это промах, а не падение запроса."""
    try:
        with closing(connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT result_json FROM llm_cache WHERE key = ?", (key,)
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if not _is_missing_table(exc):
                    raise
                _create_cache_table(conn)
                row = conn.execute(
                    "SELECT result_json FROM llm_cache WHERE key = ?", (key,)
                ).fetchone()
            stored = row["result_json"] if row is not None else None
    except (sqlite3.Error, OSError):
        return None
    if stored is None:
        return None
    try:
        return json.loads(stored)
    except (TypeError, ValueError):
        return None


def _cache_put(key: str, value: dict) -> None:
    statement = (
        "INSERT INTO llm_cache (key, result_json) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET result_json = excluded.result_json"
    )
    params = (key, json.dumps(value, ensure_ascii=False, sort_keys=True, default=str))
    try:
        with write_lock:
            with closing(connect()) as conn:
                try:
                    conn.execute(statement, params)
                except sqlite3.OperationalError as exc:
                    if not _is_missing_table(exc):
                        raise
                    _create_cache_table(conn)
                    conn.execute(statement, params)
                conn.commit()
    except (sqlite3.Error, OSError):
        # Кэш — оптимизация. Если БД занята, ответ всё равно должен дойти до UI.
        return


def _extraction_from_dict(data: Any) -> Extraction | None:
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    signals = data.get("signals")
    quotes = data.get("quotes")
    flags = data.get("flags")
    if not isinstance(summary, str):
        return None
    if not isinstance(signals, list) or not all(isinstance(item, dict) for item in signals):
        return None
    if not isinstance(quotes, list) or not all(isinstance(item, dict) for item in quotes):
        return None
    if not isinstance(flags, list) or not all(isinstance(item, str) for item in flags):
        return None
    return Extraction(summary=summary, signals=signals, quotes=quotes, flags=flags)


def _explanation_from_dict(data: Any) -> Explanation | None:
    if not isinstance(data, dict):
        return None
    summary = data.get("summary")
    reasons = data.get("reasons")
    steps = data.get("steps")
    gap_comment = data.get("gap_comment")
    if not isinstance(summary, str) or not isinstance(gap_comment, str):
        return None
    if not isinstance(reasons, list) or not all(isinstance(item, dict) for item in reasons):
        return None
    if not isinstance(steps, list) or not all(isinstance(item, dict) for item in steps):
        return None
    return Explanation(summary=summary, reasons=reasons, steps=steps, gap_comment=gap_comment)


# ---------------------------------------------------------------- публичный API


def extract(
    text: str,
    *,
    role: str,
    month: str,
    answers: dict[str, float] | None = None,
) -> Extraction:
    """Разбирает открытый текст одной анкеты. Вход кэшируется целиком."""
    text = "" if text is None else str(text)
    role = "" if role is None else str(role)
    month = "" if month is None else str(month)
    settings = _settings()

    mode, _provider_key, _model, key = _prepare(
        "extract",
        settings,
        {
            "text": text,
            "role": role,
            "month": month,
            "answers": _clean_answers(answers),
        },
    )

    cached = _cache_get(key)
    if cached is not None:
        result = _extraction_from_dict(cached)
        if result is not None:
            return result

    if not text.strip():
        # Пустой текст не стоит ни сети, ни токенов: отвечаем сразу в обоих режимах.
        result = Extraction(
            summary=f"Ответ {role or 'без роли'} за {month or 'без месяца'}: текст не заполнен.",
            signals=[],
            quotes=[],
            flags=["текст не заполнен"],
        )
    elif mode == "fake":
        result = _fake_extract(text, role, month, answers)
    else:
        content = _provider_request(
            "extract",
            _extract_system_prompt(),
            _extract_user_prompt(text, role, month, answers),
            settings,
        )
        raw = _parse_json(content)
        result = _validate_extraction(raw, text)

    _cache_put(key, asdict(result))
    return result


def explain(context: dict) -> Explanation:
    """Формулирует причины и шаги по уже посчитанным числам контекста."""
    if not isinstance(context, dict):
        raise LLMError("explain: context должен быть объектом")
    settings = _settings()
    mode, _provider_key, _model, key = _prepare("explain", settings, {"context": context})

    cached = _cache_get(key)
    if cached is not None:
        result = _explanation_from_dict(cached)
        if result is not None:
            return result

    if mode == "fake":
        result = _fake_explain(context)
    else:
        content = _provider_request(
            "explain",
            _explain_system_prompt(),
            _explain_user_prompt(context),
            settings,
        )
        raw = _parse_json(content)
        result = _validate_explanation(raw, context)

    _cache_put(key, asdict(result))
    return result


# ---------------------------------------------------------------- промпты


def _extract_system_prompt() -> str:
    return (
        "Ты — аналитик HR-дашборда стажировок. Разбери открытый текст анкеты и верни "
        "только текстовые наблюдения: сигналы, цитаты и флаги. Числа не придумывай: "
        "баллы уже пришли из анкеты отдельно.\n\n"
        "Ответ — строго один JSON-объект без markdown:\n"
        '{"summary": "1–2 предложения",\n'
        ' "signals": [{"key": "left_intent", "quote": "дословная цитата", "confidence": 0.9}],\n'
        ' "quotes": [{"factor": "clarity", "quote": "дословная цитата"}],\n'
        ' "flags": ["короткий флаг"]}\n\n'
        "Правила:\n"
        f"- signals.key — только из {', '.join(SIGNAL_KEYS)};\n"
        f"- quotes.factor — только из {', '.join(FACTORS)};\n"
        "- quote — дословная подстрока текста анкеты, не перефразируй и не исправляй;\n"
        "- если подтверждения нет, не добавляй запись;\n"
        "- confidence — число от 0 до 1;\n"
        "- summary — по-русски, без оценок и баллов."
    )


def _extract_user_prompt(
    text: str, role: str, month: str, answers: dict[str, float] | None
) -> str:
    answers_json = json.dumps(
        _clean_answers(answers), ensure_ascii=False, sort_keys=True
    )
    return (
        f"Роль: {role or 'не указана'}\n"
        f"Месяц: {month or 'не указан'}\n"
        f"Числовые ответы: {answers_json}\n\n"
        f"Текст анкеты:\n{text}"
    )


def _explain_system_prompt() -> str:
    return (
        "Ты — аналитик HR-дашборда стажировок. Тебе дан уже посчитанный расчёт: "
        "вклады факторов, сигналы и цитаты. Числа не меняй и не придумывай новых.\n\n"
        "Ответ — строго один JSON-объект без markdown:\n"
        '{"summary": "1–2 предложения",\n'
        ' "reasons": [{"key": "clarity", "text": "почему фактор влияет", "quote": "цитата"}],\n'
        ' "gap_comment": "комментарий к расхождению или пустая строка",\n'
        ' "steps": [{"key": "clarity", "text": "что сделать", '
        '"owner": "наставник", "due": "2 недели"}]}\n\n'
        "Правила:\n"
        f"- reasons.key и steps.key — только из {', '.join(FACTORS)};\n"
        "- reasons — не больше трёх, только самые весомые;\n"
        "- steps — 2–3 конкретных шага по самым слабым факторам;\n"
        "- owner — «наставник» или «HR»;\n"
        "- due — «2 недели» или «1 месяц»;\n"
        "- quote — дословная подстрока текста одной из сторон, иначе оставь пустую строку;\n"
        "- gap_comment заполняй, только если разрыв действительно есть (4 пункта и больше)."
    )


def _explain_user_prompt(context: dict) -> str:
    return (
        "Контекст расчёта в JSON:\n"
        + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    )


# ---------------------------------------------------------------- провайдеры


def _http_json(
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict | None = None,
    data: dict | None = None,
    settings: Settings,
    label: str,
    verify: bool = True,
) -> Any:
    """HTTP-запрос с таймаутом и ретраями. Сеть — только здесь."""
    timeout = float(getattr(settings, "llm_timeout", 20.0) or 20.0)
    retries = max(0, int(getattr(settings, "llm_retries", 0) or 0))
    attempts = retries + 1
    last_error = "нет ответа"

    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=timeout, verify=verify) as client:
                response = client.post(
                    url, headers=headers, json=json_body, data=data
                )
        except httpx.HTTPError as exc:
            last_error = f"сеть: {exc}"
            if attempt < attempts:
                continue
            raise LLMError(
                f"{label}: провайдер недоступен после {attempts} попыток ({exc})"
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            last_error = f"HTTP {response.status_code}: {_short(response.text)}"
            if attempt < attempts:
                continue
            raise LLMError(f"{label}: {last_error} после {attempts} попыток")

        if response.status_code >= 400:
            # 4xx не ретраим: неверный ключ или запрос повторится с тем же итогом.
            raise LLMError(
                f"{label}: HTTP {response.status_code}: {_short(response.text)}"
            )

        try:
            return response.json()
        except ValueError as exc:
            raise LLMError(f"{label}: ответ провайдера не JSON") from exc

    raise LLMError(f"{label}: запрос не выполнен ({last_error})")


def _short(value: object, limit: int = 300) -> str:
    text = _collapse(value)
    return text[:limit] + ("…" if len(text) > limit else "")


def _chat_payload(model: str, system_prompt: str, user_prompt: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
        # DeepSeek и GigaChat умеют json_object; при отказе повторим без поля.
        "response_format": {"type": "json_object"},
    }


def _endpoint(base: str, path: str, *, prefix: str = "") -> str:
    base = (base or "").rstrip("/")
    if base.endswith(path):
        return base
    if prefix and base.endswith(prefix):
        return base + path[len(prefix) :]
    return base + path


def _content_from_response(data: Any, label: str) -> str:
    if not isinstance(data, dict):
        raise LLMError(f"{label}: ответ не JSON-объект")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise LLMError(f"{label}: в ответе нет choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LLMError(f"{label}: в ответе нет message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise LLMError(f"{label}: пустой ответ модели")
    return content


def _deepseek_request(system_prompt: str, user_prompt: str, settings: Settings) -> str:
    model = _model_name("deepseek", settings)
    base = str(getattr(settings, "llm_base_url", "") or DEFAULT_DEEPSEEK_URL).rstrip("/")
    url = _endpoint(base, "/chat/completions")
    headers = {
        "Authorization": f"Bearer {str(getattr(settings, 'llm_api_key', '') or '').strip()}",
        "Content-Type": "application/json",
    }
    payload = _chat_payload(model, system_prompt, user_prompt)
    try:
        data = _http_json(
            url, headers=headers, json_body=payload, settings=settings, label="DeepSeek"
        )
    except LLMError as exc:
        if "response_format" not in str(exc):
            raise
        payload.pop("response_format", None)
        data = _http_json(
            url, headers=headers, json_body=payload, settings=settings, label="DeepSeek"
        )
    return _content_from_response(data, "DeepSeek")


def _gigachat_request(system_prompt: str, user_prompt: str, settings: Settings) -> str:
    key = str(getattr(settings, "llm_api_key", "") or "").strip()
    oauth_headers = {
        "Authorization": f"Basic {key}",
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    # GigaChat подписан сертификатом НУЦ Минцифры, которого обычно нет в системном
    # хранилище контейнера. Для демо проверку сертификата отключаем (verify=False);
    # в продакшене нужно положить корневой сертификат в trust store и вернуть True.
    token_data = _http_json(
        GIGACHAT_OAUTH_URL,
        headers=oauth_headers,
        data={"scope": GIGACHAT_SCOPE},
        settings=settings,
        label="GigaChat OAuth",
        verify=False,
    )
    token = token_data.get("access_token") if isinstance(token_data, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise LLMError("GigaChat OAuth: в ответе нет access_token")

    model = _model_name("gigachat", settings)
    base = str(getattr(settings, "llm_base_url", "") or DEFAULT_GIGACHAT_URL).rstrip("/")
    url = _endpoint(base, "/api/v1/chat/completions", prefix="/api/v1")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = _chat_payload(model, system_prompt, user_prompt)
    try:
        data = _http_json(
            url,
            headers=headers,
            json_body=payload,
            settings=settings,
            label="GigaChat",
            verify=False,
        )
    except LLMError as exc:
        if "response_format" not in str(exc):
            raise
        payload.pop("response_format", None)
        data = _http_json(
            url,
            headers=headers,
            json_body=payload,
            settings=settings,
            label="GigaChat",
            verify=False,
        )
    return _content_from_response(data, "GigaChat")


def _provider_request(
    kind: str, system_prompt: str, user_prompt: str, settings: Settings
) -> str:
    """Единая точка вызова провайдера: её подменяют тесты и будущий мок-сервер."""
    if _provider(settings) == "gigachat":
        return _gigachat_request(system_prompt, user_prompt, settings)
    return _deepseek_request(system_prompt, user_prompt, settings)


# ---------------------------------------------------------------- разбор JSON


def _parse_json(content: str) -> dict:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        raw = json.loads(cleaned)
    except (TypeError, ValueError) as exc:
        raise LLMError("провайдер вернул невалидный JSON") from exc
    if not isinstance(raw, dict):
        raise LLMError("провайдер вернул JSON не-объект")
    return raw


def _valid_confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.5
    return max(0.0, min(1.0, round(float(value), 2)))


def _validate_extraction(raw: dict, text: str) -> Extraction:
    """Строгая схема extract: битые цитаты и чужие ключи отбрасываются."""
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LLMError("extract: поле summary отсутствует или пустое")

    signals_raw = raw.get("signals")
    quotes_raw = raw.get("quotes")
    flags_raw = raw.get("flags")
    if not isinstance(signals_raw, list):
        raise LLMError("extract: поле signals должно быть списком")
    if not isinstance(quotes_raw, list):
        raise LLMError("extract: поле quotes должно быть списком")
    if not isinstance(flags_raw, list):
        raise LLMError("extract: поле flags должно быть списком")

    signals: list[dict] = []
    seen_signals: set[str] = set()
    for item in signals_raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        quote = item.get("quote")
        if key not in SIGNAL_KEYS or key in seen_signals:
            continue
        if not isinstance(quote, str) or not _quote_in_text(quote, text):
            continue
        signals.append(
            {
                "key": key,
                "quote": quote.strip(),
                "confidence": _valid_confidence(item.get("confidence")),
            }
        )
        seen_signals.add(key)

    quotes: list[dict] = []
    seen_quotes: set[tuple[str, str]] = set()
    for item in quotes_raw:
        if not isinstance(item, dict):
            continue
        factor = str(item.get("factor", ""))
        quote = item.get("quote")
        if factor not in FACTORS or not isinstance(quote, str):
            continue
        if not _quote_in_text(quote, text):
            continue
        marker = (factor, _collapse(quote))
        if marker in seen_quotes:
            continue
        quotes.append({"factor": factor, "quote": quote.strip()})
        seen_quotes.add(marker)

    flags: list[str] = []
    for item in flags_raw:
        if not isinstance(item, str):
            continue
        flag = item.strip()
        if flag and flag not in flags:
            flags.append(flag)

    return Extraction(
        summary=summary.strip(), signals=signals, quotes=quotes, flags=flags[:10]
    )


def _clean_owner(value: object, index: int) -> str:
    owner = str(value or "").strip().lower()
    if owner in {"hr", "нр"}:
        return "HR"
    if owner in {"наставник", "mentor", "наставника"}:
        return "наставник"
    return "наставник" if index == 0 else "HR"


def _clean_due(value: object, index: int) -> str:
    due = str(value or "").strip().lower().replace("ё", "е")
    if due in {"2 недели", "две недели"}:
        return "2 недели"
    if due in {"1 месяц", "месяц", "один месяц"}:
        return "1 месяц"
    return "2 недели" if index == 0 else "1 месяц"


def _validate_explanation(raw: dict, context: dict) -> Explanation:
    """Строгая схема explain: ≤3 причин, 2–3 шага, цитаты только из текста."""
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise LLMError("explain: поле summary отсутствует или пустое")

    reasons_raw = raw.get("reasons")
    steps_raw = raw.get("steps")
    if not isinstance(reasons_raw, list):
        raise LLMError("explain: поле reasons должно быть списком")
    if not isinstance(steps_raw, list):
        raise LLMError("explain: поле steps должно быть списком")

    gap_raw = raw.get("gap_comment", "")
    if gap_raw is None:
        gap_raw = ""
    if not isinstance(gap_raw, str):
        raise LLMError("explain: поле gap_comment должно быть строкой")

    source = _context_source(context)

    reasons: list[dict] = []
    seen_reasons: set[str] = set()
    for item in reasons_raw:
        if len(reasons) >= 3:
            break
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        text = item.get("text")
        if key not in FACTORS or key in seen_reasons:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        quote = item.get("quote")
        if not isinstance(quote, str):
            quote = ""
        # Битые цитаты не роняют весь ответ: причина остаётся без цитаты.
        if quote and source and not _quote_in_text(quote, source):
            quote = ""
        reasons.append({"key": key, "text": text.strip(), "quote": quote.strip()})
        seen_reasons.add(key)

    steps: list[dict] = []
    seen_steps: set[str] = set()
    for index, item in enumerate(steps_raw):
        if len(steps) >= 3:
            break
        if not isinstance(item, dict):
            continue
        key = str(item.get("key", ""))
        text = item.get("text")
        if key not in FACTORS or key in seen_steps:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        steps.append(
            {
                "key": key,
                "text": text.strip(),
                "owner": _clean_owner(item.get("owner"), len(steps)),
                "due": _clean_due(item.get("due"), len(steps)),
            }
        )
        seen_steps.add(key)

    if len(steps) < 2:
        raise LLMError("explain: провайдер вернул меньше двух корректных шагов")

    gap_comment = gap_raw.strip()
    best_gap = _max_gap(context)
    if best_gap is None or best_gap[0] < 4:
        gap_comment = ""

    return Explanation(
        summary=summary.strip(), reasons=reasons, steps=steps, gap_comment=gap_comment
    )
