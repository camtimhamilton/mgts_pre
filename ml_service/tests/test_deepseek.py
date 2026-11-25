import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import ComponentConfig, DeepSeekConfig
from app.deepseek import DeepSeekProvider
from app.knowledge import ROOT
from app.llm import ProviderError, parse_extraction
from app.main import create_app
from app.schemas import LLMExtraction, SurveyPeriod


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("MGTS_ALLOW_SYNTHETIC_LLM", "1")


@pytest.fixture
def config():
    return DeepSeekConfig(retry_base_seconds=0, retry_max_seconds=0)


def envelope(content=None, **updates):
    return {"model": "deepseek-flash", "choices": [{"finish_reason": "stop", "message": {
        "role": "assistant", "content": content if content is not None else
            '{"response_id":"response_001","signals":[],"unclassified_fragments":[],"requires_review":false}'}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                  "prompt_cache_hit_tokens": 2, "prompt_cache_miss_tokens": 8}, **updates}


def provider(config, handler, component="signal_extractor"):
    return DeepSeekProvider(config, component, transport=httpx.MockTransport(handler))


def generate(p):
    return asyncio.run(p.generate_structured("Извлеки сигналы", "Синтетический ответ", LLMExtraction.model_json_schema()))


def test_request_defaults_and_measurements(env, config):
    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic-test-key"
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash" and body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert body["temperature"] == 0 and body["stream"] is False and "tools" not in body
        assert "JSON Schema" in body["messages"][0]["content"]
        return httpx.Response(200, json=envelope(system_fingerprint="fp-test"))
    p = provider(config, handler)
    result = generate(p)
    call = result.telemetry
    assert call.provider == "deepseek" and call.model_identity_status == "reported_alias"
    assert call.system_fingerprint == "fp-test"
    assert call.latency_ms >= 0 and call.attempts[0].latency_ms >= 0
    assert call.attempts[0].usage.total_tokens == 15
    assert len(call.config_hash) == len(call.effective_prompt_hash) == len(call.schema_hash) == 64
    assert "synthetic-test-key" not in json.dumps(p.configuration) and "synthetic-test-key" not in repr(p)


@pytest.mark.parametrize("component", ["rule_extractor", "semantic_validator"])
def test_component_profiles(env, config, component):
    def handler(request):
        assert json.loads(request.content)["model"] == "deepseek-v4-pro"
        return httpx.Response(200, json=envelope(model="DeepSeek-V4-Pro-0813"))
    assert generate(provider(config, handler, component)).model_id == "DeepSeek-V4-Pro-0813"


def test_env_profiles_and_thinking(env, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_SIGNAL_EXTRACTOR_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("DEEPSEEK_SIGNAL_EXTRACTOR_THINKING", "1")
    monkeypatch.setenv("DEEPSEEK_SIGNAL_EXTRACTOR_MAX_TOKENS", "1234")
    config = DeepSeekConfig.from_env()
    def handler(request):
        body = json.loads(request.content)
        assert body["thinking"] == {"type": "enabled"} and body["reasoning_effort"] == "high"
        assert "temperature" not in body and body["max_tokens"] == 1234
        return httpx.Response(200, json=envelope())
    generate(provider(config, handler))


@pytest.mark.parametrize("status,code", [(400, "provider_invalid_request"), (401, "provider_authentication_failed"),
    (402, "provider_insufficient_balance"), (403, "provider_forbidden"), (413, "provider_request_too_large"),
    (422, "provider_invalid_parameters"), (302, "provider_http_error"), (404, "provider_http_error")])
def test_permanent_http_no_retry(env, config, status, code, caplog):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status, text="synthetic-test-key / confidential comment", headers={"Location": "https://evil.test"})
    with pytest.raises(ProviderError) as caught:
        generate(provider(config, handler))
    assert caught.value.code == code and len(calls) == 1
    assert caught.value.telemetry.attempts[0].http_status == status
    assert "confidential" not in str(caught.value) + caplog.text
    assert "synthetic-test-key" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_retry_success(env, config, status):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status, headers={"Retry-After": "0"}) if len(calls) == 1 else httpx.Response(200, json=envelope())
    result = generate(provider(config, handler))
    assert len(calls) == 2 and len(result.telemetry.attempts) == 2


def test_exhausted_and_long_retry_after(env, config):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503)
    with pytest.raises(ProviderError) as caught:
        generate(provider(config, handler))
    assert len(calls) == 3 and len(caught.value.telemetry.attempts) == 3
    def rate_limit(request):
        return httpx.Response(429, headers={"Retry-After": "3600"})
    with pytest.raises(ProviderError) as caught:
        generate(provider(config, rate_limit))
    assert len(caught.value.telemetry.attempts) == 1
    assert DeepSeekProvider._retry_after("Wed, 25 Nov 2099 10:00:00 GMT") > 0
    assert DeepSeekProvider._retry_after("invalid") is None


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError])
def test_transport_retry(env, config, exception):
    calls = []
    def handler(request):
        calls.append(1)
        if len(calls) < 2:
            raise exception("secret raw text", request=request)
        return httpx.Response(200, json=envelope())
    assert len(generate(provider(config, handler)).telemetry.attempts) == 2


@pytest.mark.parametrize("content", ["not JSON", "[]", '{"x": NaN}', '{"x":1,"x":2}', "```json\n{}\n```"])
def test_invalid_content_no_retry(env, config, content):
    with pytest.raises(ProviderError, match="provider_invalid_json") as caught:
        generate(provider(config, lambda request: httpx.Response(200, json=envelope(content))))
    assert len(caught.value.telemetry.attempts) == 1
    assert caught.value.telemetry.attempts[0].usage.total_tokens == 15


def test_empty_retries_and_counts_usage(env, config):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=envelope(" " if len(calls) == 1 else "{}"))
    result = generate(provider(config, handler))
    assert len(result.telemetry.attempts) == 2
    assert sum(a.usage.total_tokens for a in result.telemetry.attempts) == 30
    with pytest.raises(ProviderError, match="provider_empty_response") as caught:
        generate(provider(config, lambda request: httpx.Response(200, json=envelope("   "))))
    assert len(caught.value.telemetry.attempts) == 3


@pytest.mark.parametrize("reason,code,retries", [("length", "provider_token_limit", 1),
    ("content_filter", "provider_content_filtered", 1), ("tool_calls", "provider_unexpected_finish", 1),
    ("insufficient_system_resource", "provider_generation_interrupted", 3), ("aborted", "provider_generation_interrupted", 3)])
def test_finish_reasons(env, config, reason, code, retries):
    data = envelope()
    data["choices"][0]["finish_reason"] = reason
    with pytest.raises(ProviderError, match=code) as caught:
        generate(provider(config, lambda request: httpx.Response(200, json=data)))
    assert len(caught.value.telemetry.attempts) == retries


@pytest.mark.parametrize("data", [{}, {"choices": []}, envelope(choices=[]), envelope(usage={"total_tokens": -1}),
    envelope(usage={"total_tokens": True})])
def test_bad_envelope_and_usage(env, config, data):
    with pytest.raises(ProviderError):
        generate(provider(config, lambda request: httpx.Response(200, json=data)))


def test_missing_usage_explicit(env, config):
    data = envelope()
    del data["usage"]
    assert generate(provider(config, lambda request: httpx.Response(200, json=data))).telemetry.attempts[0].usage is None


def test_limits_and_deadline_release(env, config):
    async def handler(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=envelope())
    p = provider(config.model_copy(update={"total_timeout_seconds": 0.05}), handler)
    with pytest.raises(ProviderError, match="provider_total_timeout") as caught:
        generate(p)
    assert len(caught.value.telemetry.attempts) == 1
    assert caught.value.telemetry.attempts[0].usage is None
    assert caught.value.telemetry.attempts[0].error_code == "provider_attempt_cancelled"
    assert p._limiter._value == config.max_concurrency
    with pytest.raises(ProviderError, match="provider_response_too_large"):
        generate(provider(config, lambda request: httpx.Response(200, content=b" " * 2_000_001)))


def test_concurrency_queue_and_cancellation(env, config):
    async def run():
        active = peak = 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json=envelope())
        p = provider(config, handler)
        await asyncio.gather(*(p.generate_structured("json", "synthetic", {}) for _ in range(6)))
        assert peak == 2
        await p._limiter.acquire()
        await p._limiter.acquire()
        busy = provider(config.model_copy(update={"queue_timeout_seconds": 0.01}), handler)
        busy._limiter = p._limiter
        with pytest.raises(ProviderError, match="provider_busy"):
            await busy.generate_structured("json", "synthetic", {})
        pending = asyncio.create_task(p.generate_structured("json", "synthetic", {}))
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert p._limiter._value == 0
        p._limiter.release()
        p._limiter.release()
    asyncio.run(run())


def test_configuration_fail_closed(monkeypatch, env, tmp_path):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ValueError, match="deepseek_api_key_missing"):
        DeepSeekProvider()
    monkeypatch.setenv("MGTS_PROVIDER", "deepseek")
    with pytest.raises(ValueError, match="deepseek_api_key_missing"):
        create_app(tmp_path / "config.sqlite3")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-test-key")
    monkeypatch.delenv("MGTS_ALLOW_SYNTHETIC_LLM")
    with pytest.raises(ValueError, match="synthetic_llm_opt_in_required"):
        DeepSeekProvider()
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://evil.test")
    with pytest.raises(ValueError, match="invalid_deepseek_configuration"):
        DeepSeekConfig.from_env()


def test_api_wiring_schema_validation_and_egress_guard(env, config, tmp_path, caplog):
    survey = json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=envelope())
    with TestClient(create_app(tmp_path / "api.sqlite3", provider(config, handler))) as client:
        assert client.get("/health").json()["provider"] == "deepseek"
        result = client.post("/v1/analyze", json=survey)
        assert result.status_code == 200
        metadata = result.json()["metadata"]
        assert metadata["provider"] == "deepseek" and metadata["model_id"] == "deepseek-flash"
        assert metadata["provider_calls"][0]["attempts"][0]["usage"]["total_tokens"] == 15
        assert metadata["prompt_hash"] and metadata["knowledge_hash"] and metadata["provider_config_hash"]
        survey["responses"][0]["answers"][1]["value"] = "unapproved confidential text"
        response = client.post("/v1/analyze", json=survey)
        assert response.status_code == 503 and response.json()["code"] == "synthetic_input_not_allowlisted"
        assert len(calls) == 1
    assert "unapproved confidential text" not in caplog.text and "synthetic-test-key" not in caplog.text


def test_schema_failure_and_balance_stay_partial(env, config, tmp_path):
    survey = json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
    for name, reply, code in [("schema", httpx.Response(200, json=envelope("{}")), "invalid_extraction_schema"),
                              ("balance", httpx.Response(402, text="secret"), "provider_insufficient_balance")]:
        with TestClient(create_app(tmp_path / f"{name}.sqlite3", provider(config, lambda request: reply))) as client:
            response = client.post("/v1/analyze", json=survey)
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "partial" and result["risk"]["priority"] == "insufficient_data"
            assert result["risk"]["score"] == 6 and code in result["warnings"]
            assert result["metadata"]["provider_calls"][0]["error_code"] == code
            assert result["metadata"]["provider"] == "deepseek"


def test_telemetry_model_and_immutable_config(env, config):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=envelope(" ")) if len(calls) == 1 else httpx.Response(503)
    p = provider(config, handler)
    with pytest.raises(TypeError):
        p.configuration["signal_extractor"]["model"] = "wrong"
    with pytest.raises(ProviderError) as caught:
        generate(p)
    assert caught.value.telemetry.attempts[0].model_id == "deepseek-flash"
    assert caught.value.telemetry.attempts[1].model_id is None


def test_active_cancellation_releases_slot(env, config):
    async def run():
        started = asyncio.Event()
        async def handler(request):
            started.set()
            await asyncio.sleep(1)
            return httpx.Response(200, json=envelope())
        p = provider(config, handler)
        task = asyncio.create_task(p.generate_structured("json", "synthetic", {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert p._limiter._value == config.max_concurrency
    asyncio.run(run())


def test_p0_contract_and_saved_snapshot_compatible(tmp_path):
    from app.schemas import AnalysisResult
    original = json.loads((ROOT / "tests/fixtures/api_v1_p0_contract.json").read_text(encoding="utf-8"))
    api = create_app(tmp_path / "contract.sqlite3")
    schemas = api.openapi()["components"]["schemas"]
    for name, schema in original["schemas"].items():
        assert schemas[name] == schema, name
    assert schemas["Metadata"]["required"] == original["metadata_required"]
    snapshot = AnalysisResult.model_validate_json((ROOT / "tests/fixtures/analysis_p0_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot.risk.score == 17 and snapshot.metadata.provider == "mock"
    assert snapshot.metadata.provider_calls == ()


def test_smoke_requires_key_even_with_opt_in(monkeypatch):
    from scripts.smoke_deepseek import run_smoke
    monkeypatch.setenv("MGTS_RUN_DEEPSEEK_SMOKE", "1")
    monkeypatch.setenv("MGTS_ALLOW_SYNTHETIC_LLM", "1")
    result = asyncio.run(run_smoke())
    assert result == {"status": "skipped", "reason": "DEEPSEEK_API_KEY is absent"}


def test_all_provider_profiles_share_limiter_when_requested(env, config):
    async def run():
        limiter = asyncio.Semaphore(1)
        active = peak = 0
        async def handler(request):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return httpx.Response(200, json=envelope())
        providers = [DeepSeekProvider(config, component, transport=httpx.MockTransport(handler), limiter=limiter)
                     for component in ("signal_extractor", "rule_extractor", "semantic_validator")]
        await asyncio.gather(*(p.generate_structured("json", "synthetic", {}) for p in providers))
        assert peak == 1
    asyncio.run(run())


def test_analysis_key_ignores_timings_but_tracks_config(env, config, tmp_path):
    survey = json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
    def handler(request):
        return httpx.Response(200, json=envelope())
    with TestClient(create_app(tmp_path / "keys.sqlite3", provider(config, handler))) as client:
        a = client.post("/v1/analyze", json=survey).json()["metadata"]
        b = client.post("/v1/analyze", json=survey).json()["metadata"]
        assert a["analysis_key"] == b["analysis_key"]
    changed = DeepSeekConfig.model_validate({**config.model_dump(), "signal_extractor": {"model": "deepseek-v4-pro"}})
    with TestClient(create_app(tmp_path / "changed.sqlite3", provider(changed, handler))) as client:
        c = client.post("/v1/analyze", json=survey).json()["metadata"]
        assert c["analysis_key"] != a["analysis_key"]
        assert c["provider_config_hash"] != a["provider_config_hash"]


def test_api_schema_rejects_fabricated_quotes_after_http_success(env, config, tmp_path):
    survey = json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
    extraction = {"response_id": "response_001", "signals": [{"factor_id": "R01", "rule_id": "RULE_R01_001",
        "question_id": "q02", "polarity": "negative", "severity": "high", "evidence": "invented quote",
        "assertion": "explicit", "temporal_status": "current"}], "requires_review": False, "unclassified_fragments": []}
    with TestClient(create_app(tmp_path / "quotes.sqlite3", provider(config, lambda request:
            httpx.Response(200, json=envelope(json.dumps(extraction)))))) as client:
        body = client.post("/v1/analyze", json=survey).json()
        assert body["metadata"]["rejected_count"] == 1 and body["risk"]["score"] == 6
        assert body["risk"]["priority"] == "insufficient_data"
