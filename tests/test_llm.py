"""Тесты LLM-клиента: fake-эвристика, схема, кэш и деградация без сети.

Ни один тест не ходит в интернет и не требует ключей: fake-режим работает всегда,
а real-ветка проверяется подменой внутренней функции провайдера.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from app import llm
from app.normalize import FACTORS

OWNERS = {"наставник", "HR"}
DUES = {"2 недели", "1 месяц"}


def _settings(**overrides) -> SimpleNamespace:
    values = dict(
        llm_mode="fake",
        llm_provider="deepseek",
        llm_api_key="",
        llm_base_url="",
        model_name="",
        llm_timeout=2.0,
        llm_retries=0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture(autouse=True)
def _fake_mode(monkeypatch):
    """По умолчанию все тесты идут в fake, даже если в .env лежит ключ."""
    monkeypatch.setattr(llm, "_settings", lambda: _settings())


def _real_mode(monkeypatch, provider: str = "deepseek", **overrides) -> SimpleNamespace:
    settings = _settings(
        llm_mode="real", llm_provider=provider, llm_api_key="test-key", **overrides
    )
    monkeypatch.setattr(llm, "_settings", lambda: settings)
    return settings


def _provider_stub(monkeypatch, payload: dict, calls: list[str] | None = None):
    def provider(kind, system_prompt, user_prompt, settings):
        if calls is not None:
            calls.append(kind)
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(llm, "_provider_request", provider)


def _extract_payload(quote: str) -> dict:
    return {
        "summary": "Модель увидела намерение уйти.",
        "signals": [{"key": "left_intent", "quote": quote, "confidence": 0.9}],
        "quotes": [],
        "flags": ["риск ухода"],
    }


# ---------------------------------------------------------------- fake: extract


def test_fake_extraction_schema_and_determinism():
    text = (
        "Первый месяц прошёл спокойно. Не понимаю, что от меня хотят в новой задаче. "
        "Наставник помогает, но нагрузка растёт. Уже хожу по собеседованиям в другую компанию."
    )
    answers = {"motivation": 4, "clarity": 2, "mentor": "7,5"}

    first = llm.extract(text, role="intern", month="M3", answers=answers)
    second = llm.extract(text, role="intern", month="M3", answers=answers)

    assert first == second
    assert isinstance(first.summary, str) and first.summary
    assert set(first.__dataclass_fields__) == {"summary", "signals", "quotes", "flags"}

    for signal in first.signals:
        assert set(signal) == {"key", "quote", "confidence"}
        assert signal["quote"] in text
        assert 0.0 <= signal["confidence"] <= 1.0
        assert signal["key"] in {"left_intent", "burnout", "offer_doubt", "conflict"}

    for quote in first.quotes:
        assert set(quote) == {"factor", "quote"}
        assert quote["factor"] in FACTORS
        assert quote["quote"] in text

    assert first.flags and all(isinstance(flag, str) for flag in first.flags)


def test_fake_extraction_is_deterministic_without_cache(monkeypatch):
    """Проверяем саму эвристику, а не только попадание в кэш."""
    monkeypatch.setattr(llm, "_cache_get", lambda key: None)
    text = "Не понимаю, что от меня хотят. Уже хожу по собеседованиям."
    answers = {"clarity": 2, "motivation": 5}

    first = llm.extract(text, role="intern", month="M5", answers=answers)
    second = llm.extract(text, role="intern", month="M5", answers=answers)

    assert first == second
    assert first.summary == second.summary


def test_fake_left_intent_from_interviews_with_verbatim_quote():
    text = "Уже хожу по собеседованиям в другую компанию, но команда хорошая."

    result = llm.extract(text, role="intern", month="M4")

    signal = next(item for item in result.signals if item["key"] == "left_intent")
    assert signal["quote"] in text
    assert "собеседован" in signal["quote"].lower()
    assert signal["confidence"] >= 0.5


def test_fake_quotes_are_exact_substrings_for_each_factor():
    text = (
        "Команда приняла хорошо, но не понимаю, что от меня хотят. "
        "Наставник не даёт обратную связь, нагрузка растёт, интерес пропадает."
    )

    result = llm.extract(text, role="intern", month="M2")
    found = {item["factor"] for item in result.quotes}

    assert {"clarity", "mentor", "workload", "inclusion", "motivation"} <= found
    for item in result.quotes:
        assert item["factor"] in FACTORS
        assert item["quote"] in text


def test_fake_handles_empty_text_without_network():
    result = llm.extract("   ", role="mentor", month="M1")

    assert result.signals == []
    assert result.quotes == []
    assert result.summary
    assert "текст не заполнен" in result.flags


# ---------------------------------------------------------------- fake: explain


def _explain_context(**overrides) -> dict:
    context = {
        "intern_name": "Иванов Пётр",
        "month": "M5",
        "factors": [
            {
                "key": "clarity",
                "label": "Ясность задач",
                "points": 14,
                "score_intern": 2,
                "score_mentor": 8,
                "gap": 6,
                "quote": "Не понимаю, что от меня хотят.",
            },
            {
                "key": "motivation",
                "label": "Мотивация",
                "points": 12,
                "score_intern": 4,
                "score_mentor": 6,
                "gap": 2,
                "quote": "",
            },
            {
                "key": "workload",
                "label": "Нагрузка",
                "points": 9,
                "score_intern": 3,
                "score_mentor": 5,
                "gap": 2,
                "quote": "",
            },
            {
                "key": "mentor",
                "label": "Наставник",
                "points": 4,
                "score_intern": 6,
                "score_mentor": 8,
                "gap": 2,
                "quote": "",
            },
        ],
        "signals": [
            {"key": "left_intent", "label": "намерение уйти", "quote": "уже ищу"},
        ],
        "intern_text": "Не понимаю, что от меня хотят. Нагрузка растёт, уже ищу.",
        "mentor_text": "Всё в порядке, стажёр справляется.",
    }
    context.update(overrides)
    return context


def test_fake_explain_respects_schema_and_gap_rule():
    context = _explain_context()

    result = llm.explain(context)

    assert 1 <= len(result.reasons) <= 3
    assert all(item["key"] in FACTORS for item in result.reasons)
    assert all(isinstance(item["text"], str) and item["text"] for item in result.reasons)
    assert all(item["quote"] == "" or item["quote"] in context["intern_text"] for item in result.reasons)

    assert 2 <= len(result.steps) <= 3
    assert all(item["key"] in FACTORS for item in result.steps)
    assert all(item["owner"] in OWNERS for item in result.steps)
    assert all(item["due"] in DUES for item in result.steps)

    assert result.gap_comment
    assert result.summary
    assert result == llm.explain(context)


def test_fake_explain_gap_comment_empty_below_threshold():
    context = _explain_context(
        intern_name="Без разрыва",
        factors=[
            {
                "key": "clarity",
                "label": "Ясность задач",
                "points": 5,
                "score_intern": 5,
                "score_mentor": 7,
                "gap": 2,
                "quote": "",
            },
            {
                "key": "workload",
                "label": "Нагрузка",
                "points": 3,
                "score_intern": 7,
                "score_mentor": None,
                "gap": None,
                "quote": "",
            },
        ],
        signals=[],
    )

    result = llm.explain(context)

    assert result.gap_comment == ""
    assert 2 <= len(result.steps) <= 3


# ---------------------------------------------------------------- real: кэш и HTTP


def test_cache_does_not_call_provider_twice(monkeypatch):
    _real_mode(monkeypatch)
    calls: list[str] = []
    text = "Кэш-проверка: уже хожу по собеседованиям в другую сферу."
    _provider_stub(monkeypatch, _extract_payload("уже хожу по собеседованиям в другую сферу"), calls)

    first = llm.extract(text, role="intern", month="M3")
    second = llm.extract(text, role="intern", month="M3")

    assert first == second
    assert calls == ["extract"]


def test_cache_shared_by_explain(monkeypatch):
    _real_mode(monkeypatch)
    calls: list[str] = []
    context = _explain_context(intern_name="Кэш Объяснение")
    payload = {
        "summary": "Главный вклад даёт ясность задач.",
        "reasons": [
            {
                "key": "clarity",
                "text": "Ожидания расходятся.",
                "quote": "Не понимаю, что от меня хотят.",
            }
        ],
        "gap_comment": "Разрыв по ясности задач.",
        "steps": [
            {"key": "clarity", "text": "Согласовать задачи.", "owner": "наставник", "due": "2 недели"},
            {"key": "mentor", "text": "Ввести обратную связь.", "owner": "HR", "due": "1 месяц"},
        ],
    }
    _provider_stub(monkeypatch, payload, calls)

    first = llm.explain(context)
    second = llm.explain(context)

    assert first == second
    assert calls == ["explain"]


def test_invalid_quotes_and_foreign_keys_are_dropped(monkeypatch):
    _real_mode(monkeypatch)
    text = (
        "Задачи понятны, но не понимаю, что от меня хотят. "
        "Уже хожу по собеседованиям."
    )
    payload = {
        "summary": "Модель что-то говорит.",
        "signals": [
            {
                "key": "left_intent",
                "quote": "Уже хожу по собеседованиям.",
                "confidence": 0.9,
            },
            {
                "key": "burnout",
                "quote": "не вывожу от усталости",
                "confidence": 0.9,
            },
            {
                "key": "unknown_signal",
                "quote": "Задачи понятны",
                "confidence": 0.9,
            },
        ],
        "quotes": [
            {"factor": "clarity", "quote": "не понимаю, что от меня хотят"},
            {"factor": "clarity", "quote": "такой цитаты нет в тексте"},
            {"factor": "unknown_factor", "quote": "Задачи понятны"},
        ],
        "flags": ["модель"],
    }
    _provider_stub(monkeypatch, payload)

    result = llm.extract(text, role="intern", month="M2")

    assert [item["key"] for item in result.signals] == ["left_intent"]
    assert result.signals[0]["quote"] in text
    assert [item["factor"] for item in result.quotes] == ["clarity"]
    assert result.quotes[0]["quote"] in text


def test_real_explain_truncates_and_sanitizes(monkeypatch):
    _real_mode(monkeypatch)
    context = _explain_context(intern_name="Валидация Ответа")
    payload = {
        "summary": "Короткое объяснение.",
        "reasons": [
            {
                "key": "clarity",
                "text": "Ожидания расходятся.",
                "quote": "Не понимаю, что от меня хотят.",
            },
            {"key": "mentor", "text": "Обратной связи мало.", "quote": "цитаты нет"},
            {"key": "workload", "text": "Нагрузка растёт.", "quote": ""},
            {"key": "motivation", "text": "Мотивация падает.", "quote": ""},
            {"key": "future", "text": "Будущее туманно.", "quote": ""},
        ],
        "gap_comment": "Комментарий модели про разрыв.",
        "steps": [
            {"key": "clarity", "text": "Согласовать задачи.", "owner": "начальник", "due": "завтра"},
            {"key": "mentor", "text": "Ввести обратную связь.", "owner": "HR", "due": "1 месяц"},
            {"key": "workload", "text": "Снять лишние задачи.", "owner": "наставник", "due": "2 недели"},
            {"key": "future", "text": "Проговорить оффер.", "owner": "HR", "due": "1 месяц"},
        ],
    }
    _provider_stub(monkeypatch, payload)

    result = llm.explain(context)

    assert len(result.reasons) == 3
    assert len(result.steps) == 3
    assert result.reasons[1]["quote"] == ""  # невалидная цитата снята, причина осталась
    assert all(step["owner"] in OWNERS for step in result.steps)
    assert all(step["due"] in DUES for step in result.steps)
    assert result.steps[0]["owner"] == "наставник"
    assert result.steps[0]["due"] == "2 недели"
    assert result.gap_comment


def test_retries_from_settings(monkeypatch):
    _real_mode(monkeypatch, llm_retries=2)
    attempts: list[int] = []
    payload = _extract_payload("уже хожу по собеседованиям")

    class Response:
        def __init__(self, status_code: int, body: dict | None = None):
            self.status_code = status_code
            self.text = "boom" if status_code >= 400 else "{}"
            self._body = body or {}

        def json(self):
            return self._body

    class RetryClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, *args, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                return Response(500)
            return Response(
                200,
                {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
            )

    monkeypatch.setattr(llm.httpx, "Client", RetryClient)

    result = llm.extract(
        "Ретраи: уже хожу по собеседованиям.", role="intern", month="M2"
    )

    assert result.signals[0]["key"] == "left_intent"
    assert len(attempts) == 3  # первая попытка + два ретрая из настроек


def test_deepseek_drops_response_format_on_400(monkeypatch):
    _real_mode(monkeypatch, llm_retries=0)
    calls: list[dict] = []
    payload = _extract_payload("уже хожу по собеседованиям")

    def fake_http(
        url,
        *,
        headers,
        json_body=None,
        data=None,
        settings,
        label,
        verify=True,
    ):
        calls.append(dict(json_body or {}))
        if len(calls) == 1:
            raise llm.LLMError("DeepSeek: HTTP 400: response_format не поддерживается")
        return {
            "choices": [
                {"message": {"content": json.dumps(payload, ensure_ascii=False)}}
            ]
        }

    monkeypatch.setattr(llm, "_http_json", fake_http)

    result = llm.extract(
        "Фолбэк формата: уже хожу по собеседованиям.", role="intern", month="M2"
    )

    assert result.signals[0]["key"] == "left_intent"
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_network_error_raises_llm_error(monkeypatch):
    _real_mode(monkeypatch, llm_retries=0)

    class BoomClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def post(self, *args, **kwargs):
            raise httpx.ConnectError("нет сети")

    monkeypatch.setattr(llm.httpx, "Client", BoomClient)

    with pytest.raises(llm.LLMError):
        llm.extract(
            "Сетевой тест: уже хожу по собеседованиям.",
            role="intern",
            month="M1",
        )


def test_garbage_json_raises_llm_error(monkeypatch):
    _real_mode(monkeypatch)
    monkeypatch.setattr(
        llm, "_provider_request", lambda *args, **kwargs: "это точно не JSON {{{"
    )

    with pytest.raises(llm.LLMError):
        llm.extract("Мусорный ответ провайдера.", role="intern", month="M1")


def test_gigachat_oauth_then_chat(monkeypatch):
    _real_mode(monkeypatch, provider="gigachat")
    calls: list[dict] = []
    payload = _extract_payload("уже хожу по собеседованиям")

    def fake_http(
        url,
        *,
        headers,
        json_body=None,
        data=None,
        settings,
        label,
        verify=True,
    ):
        calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json_body,
                "data": data,
                "verify": verify,
            }
        )
        if "oauth" in url:
            return {"access_token": "token-123"}
        return {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}

    monkeypatch.setattr(llm, "_http_json", fake_http)

    result = llm.extract(
        "ГигаЧат: уже хожу по собеседованиям.", role="intern", month="M2"
    )

    assert [item["key"] for item in result.signals] == ["left_intent"]
    assert len(calls) == 2
    oauth, chat = calls
    assert oauth["url"] == llm.GIGACHAT_OAUTH_URL
    assert oauth["headers"]["Authorization"] == "Basic test-key"
    assert oauth["data"] == {"scope": llm.GIGACHAT_SCOPE}
    assert oauth["verify"] is False
    assert chat["url"].endswith("/api/v1/chat/completions")
    assert chat["headers"]["Authorization"] == "Bearer token-123"
    assert chat["verify"] is False


# ---------------------------------------------------------------- режим и имя


def test_provider_name_for_fake_and_real(monkeypatch):
    assert llm.provider_name() == "fake"

    _real_mode(monkeypatch, provider="deepseek")
    assert llm.provider_name() == "deepseek"

    _real_mode(monkeypatch, provider="gigachat")
    assert llm.provider_name() == "gigachat"


def test_auto_mode_falls_back_to_fake_without_key(monkeypatch):
    monkeypatch.setattr(llm, "_settings", lambda: _settings(llm_mode="auto", llm_api_key=""))

    assert llm.provider_name() == "fake"
    result = llm.extract("Авто-режим: уже хожу по собеседованиям.", role="intern", month="M2")
    assert any(item["key"] == "left_intent" for item in result.signals)
