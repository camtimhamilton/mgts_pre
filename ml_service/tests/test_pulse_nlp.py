"""Privacy, exact source, temporal status and replay regressions for v2 NLP."""
import asyncio
import json
import pytest

from app import pulse_nlp as nlp
from app.knowledge import ROOT
from app.llm import ProviderResult


def record(texts, **overrides):
    return {"role": "intern", "source_id": "source-synthetic", "version": 1, "pair_id": "pair",
            "month": 1, "intern_id": "intern", "texts": texts, **overrides}


@pytest.fixture(autouse=True)
def local_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MGTS_DATABASE", str(tmp_path/"analysis.sqlite3"))
    monkeypatch.setenv("PULSE_TEXT_MODE", "local_rules")


@pytest.mark.parametrize("comment", ["Неправда, что не хватает поддержки.", "Коллега считает, что не хватает поддержки.",
                                    "Если не хватает поддержки, можно обратиться к наставнику."])
def test_secondary_or_conditional_observation_abstains(comment):
    result = asyncio.run(nlp.enrich(record({"barriers": comment})))
    assert result["signals"]
    assert all(s["validation_status"] == "review" for s in result["signals"])


def test_cache_preserves_survey_ownership():
    r = record({"barriers": "Мне не хватает поддержки."})
    one = asyncio.run(nlp.enrich(r))
    other = asyncio.run(nlp.enrich(dict(r, pair_id="other", month=4)))
    assert one["signals"][0]["signal_id"] != other["signals"][0]["signal_id"]
    assert other["signals"][0]["pair_id"] == "other"
    assert other["signals"][0]["month"] == 4


def test_egress_is_denied_before_provider_construction(monkeypatch):
    monkeypatch.setenv("PULSE_TEXT_MODE", "deepseek")
    def unexpected(*args, **kwargs):
        pytest.fail("unapproved text reached provider")
    monkeypatch.setattr(nlp, "DeepSeekProvider", unexpected)
    result = asyncio.run(nlp.enrich(record({"barriers":"Произвольный текст."})))
    assert result["nlp_audit"]["error"] == "external_processing_not_authorized"
    assert result["text_analysis_status"] == "error" and not result["signals"]


def test_two_stage_quote_validation_and_concurrent_replay(monkeypatch):
    monkeypatch.setenv("PULSE_TEXT_MODE", "deepseek")
    fixture = json.loads((ROOT/"tests/fixtures/pair_synthetic_text.json").read_text("utf8"))
    r = record(fixture["texts"])
    assert r["role"] == fixture["role"]
    calls = []
    class Provider:
        def __init__(self, config, name, limiter):
            self.name = name
        async def generate_structured(self, prompt, content, response_schema):
            calls.append(self.name)
            await asyncio.sleep(.005)
            if self.name == "signal_extractor":
                sig = {"factor_id":"support", "question_id":"barriers", "temporal_status":"current",
                       "polarity":"negative", "severity":"moderate", "assertion":"Не хватает обратной связи",
                       "quote":r["texts"]["barriers"]}
                value = {"signals":[sig, dict(sig, quote="Выдуманная цитата.")]}
            else:
                assert len(json.loads(content)["candidates"]) == 1
                value = {"decisions":[{"index":0,"status":"accepted"}]}
            return ProviderResult(raw=json.dumps(value), provider="synthetic_transport", model_id="unit")
    monkeypatch.setattr(nlp, "DeepSeekProvider", Provider)
    async def concurrent():
        return await asyncio.gather(nlp.enrich(r), nlp.enrich(r))
    a,b = asyncio.run(concurrent())
    assert a == b
    assert calls == ["signal_extractor","semantic_validator"]
    assert [s["validation_status"] for s in a["signals"]] == ["accepted","rejected"]
