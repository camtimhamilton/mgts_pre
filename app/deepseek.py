"""OpenAI-compatible DeepSeek JSON adapter, with explicit bounded transport policy.

No scoring, no prompt logging, no fallback, no model tools. Keys come only from env.
Future NLP components may select a different config and supply their own schema.
"""
import asyncio
import json
import math
import os
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import perf_counter

import httpx
from pydantic import Field, SecretStr, ValidationError

from app.config import Component, DeepSeekConfig
from app.llm import ProviderError, ProviderResult
from app.schemas import Model, ProviderAttempt, ProviderCall, TokenUsage, freeze
from app.serialization import canonical, digest

ADAPTER_VERSION = "deepseek-json-v1"
RETRYABLE_HTTP = {408, 429, 500, 502, 503, 504}
HTTP_CODES = {400: "provider_invalid_request", 401: "provider_authentication_failed",
    402: "provider_insufficient_balance", 403: "provider_forbidden", 408: "provider_timeout",
    413: "provider_request_too_large", 422: "provider_invalid_parameters", 429: "provider_rate_limited"}
MAX_RESPONSE_BYTES = 2_000_000


class CompletionMessage(Model):
    # Provider-only schemas deliberately ignore newly added vendor fields.
    model_config = {"extra": "ignore"}
    content: str | None
    role: str = "assistant"


class CompletionChoice(Model):
    model_config = {"extra": "ignore"}
    finish_reason: str
    message: CompletionMessage


class CompletionEnvelope(Model):
    model_config = {"extra": "ignore"}
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    choices: tuple[CompletionChoice, ...] = Field(min_length=1, max_length=1)
    usage: dict | None = None
    system_fingerprint: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class DeepSeekProvider:
    name = "deepseek"

    def __init__(self, config: DeepSeekConfig | None = None, component: Component = "signal_extractor",
                 *, transport: httpx.AsyncBaseTransport | None = None, limiter: asyncio.Semaphore | None = None):
        if component not in {"signal_extractor", "rule_extractor", "semantic_validator"}:
            raise ValueError("unknown_nlp_component")
        if os.getenv("MGTS_ALLOW_SYNTHETIC_LLM") != "1":
            raise ValueError("synthetic_llm_opt_in_required")
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise ValueError("deepseek_api_key_missing")
        self._api_key = SecretStr(api_key)
        self.config = config or DeepSeekConfig.from_env()
        self.component = component
        self.profile = getattr(self.config, component)
        self.model_id = self.profile.model  # requested alias; actual response ID is recorded separately
        self.total_timeout_seconds = self.config.total_timeout_seconds
        self._transport = transport
        self._limiter = limiter if limiter is not None else asyncio.Semaphore(self.config.max_concurrency)
        self.configuration = freeze({"adapter_version": ADAPTER_VERSION, "component": component,
            **self.config.model_dump(mode="json")})

    def _body(self, system_prompt: str, user_prompt: str, response_schema: dict):
        # JSON Output is not strict-schema output. Pydantic validates the result later.
        example = '{"response_id":"response_001","signals":[],"unclassified_fragments":[],"requires_review":false}'
        suffix = ("\nВерни только JSON-объект, соответствующий JSON Schema ниже. "
                  "Пример формы для Signal Extractor (значения не являются ответом): " + example +
                  "\nДля других компонентов следуй их схеме, а не примеру.\nJSON Schema:\n" + canonical(response_schema))
        body = {"model": self.profile.model, "messages": [
            {"role": "system", "content": system_prompt + suffix}, {"role": "user", "content": user_prompt}],
            "response_format": {"type": "json_object"}, "stream": False,
            "max_tokens": self.profile.max_tokens, "thinking": {"type": "enabled" if self.profile.thinking else "disabled"}}
        if self.profile.thinking:
            body["reasoning_effort"] = "high"
        else:
            body["temperature"] = 0
        return body

    async def generate_structured(self, system_prompt: str, user_prompt: str, response_schema: dict) -> ProviderResult:
        body = self._body(system_prompt, user_prompt, response_schema)
        start = perf_counter()
        attempts = []
        resolved_model = fingerprint = None
        error_code = None

        def telemetry(code=None):
            return ProviderCall(component=self.component, provider=self.name, requested_model=self.model_id,
                model_id=resolved_model, system_fingerprint=fingerprint,
                model_identity_status="not_reported" if resolved_model is None else
                    "reported_alias" if resolved_model == self.model_id else "reported_id",
                config_hash=digest(self.configuration), effective_prompt_hash=digest(body["messages"]),
                schema_hash=digest(response_schema), latency_ms=round((perf_counter() - start) * 1000, 3),
                attempts=tuple(attempts), error_code=code)

        acquired = False
        try:
            # Includes queue, retries, backoff, reading and parsing, not just socket inactivity.
            async with asyncio.timeout(self.config.total_timeout_seconds):
                try:
                    await asyncio.wait_for(self._limiter.acquire(), self.config.queue_timeout_seconds)
                except TimeoutError:
                    raise ProviderError("provider_busy") from None
                acquired = True
                async with httpx.AsyncClient(transport=self._transport, timeout=self.config.timeout_seconds,
                        follow_redirects=False, trust_env=False,
                        limits=httpx.Limits(max_connections=self.config.max_concurrency,
                                            max_keepalive_connections=self.config.max_concurrency)) as client:
                    for number in range(1, self.config.max_retries + 2):
                        attempt_start = perf_counter()
                        http_status = None
                        usage = None
                        attempt_model = None
                        finish = None
                        error_code = None
                        retryable = False
                        delay_hint = None
                        try:
                            async with client.stream("POST", self.config.base_url + "/chat/completions", json=body,
                                    headers={"Authorization": "Bearer " + self._api_key.get_secret_value()}) as response:
                                http_status = response.status_code
                                if http_status != 200:
                                    error_code = HTTP_CODES.get(http_status, "provider_server_error" if http_status >= 500 else "provider_http_error")
                                    retryable = http_status in RETRYABLE_HTTP
                                    delay_hint = self._retry_after(response.headers.get("Retry-After"))
                                    # Never parse or log upstream error bodies: they may echo prompts/secrets.
                                else:
                                    content = bytearray()
                                    async for chunk in response.aiter_bytes():
                                        content.extend(chunk)
                                        if len(content) > MAX_RESPONSE_BYTES:
                                            raise ProviderError("provider_response_too_large")
                                    envelope = self._envelope(bytes(content))
                                    resolved_model, fingerprint = envelope.model, envelope.system_fingerprint
                                    attempt_model = envelope.model
                                    usage = self._usage(envelope.usage)
                                    choice = envelope.choices[0]
                                    finish = choice.finish_reason if choice.finish_reason in {
                                        "stop", "length", "insufficient_system_resource", "aborted", "content_filter", "tool_calls"} else "unknown"
                                    if finish == "length":
                                        error_code = "provider_token_limit"
                                    elif finish in {"insufficient_system_resource", "aborted"}:
                                        error_code, retryable = "provider_generation_interrupted", True
                                    elif finish != "stop":
                                        error_code = "provider_content_filtered" if finish == "content_filter" else "provider_unexpected_finish"
                                    elif not choice.message.content or not choice.message.content.strip():
                                        error_code, retryable = "provider_empty_response", True
                                    else:
                                        self._require_json_object(choice.message.content)
                                        attempts.append(ProviderAttempt(attempt=number,
                                            latency_ms=round((perf_counter() - attempt_start) * 1000, 3),
                                            http_status=http_status, model_id=attempt_model, finish_reason=finish, usage=usage))
                                        return ProviderResult(raw=choice.message.content, provider=self.name,
                                            model_id=resolved_model, telemetry=telemetry())
                        except asyncio.CancelledError:
                            attempts.append(ProviderAttempt(attempt=number,
                                latency_ms=round((perf_counter() - attempt_start) * 1000, 3),
                                http_status=http_status, model_id=attempt_model, finish_reason=finish,
                                usage=usage, error_code="provider_attempt_cancelled"))
                            raise
                        except httpx.TimeoutException:
                            error_code, retryable = "provider_timeout", True
                        except (httpx.NetworkError, httpx.RemoteProtocolError):
                            error_code, retryable = "provider_connection_error", True
                        except httpx.RequestError:
                            error_code = "provider_transport_error"
                        except ProviderError as error:
                            error_code = error.code
                        attempts.append(ProviderAttempt(attempt=number,
                            latency_ms=round((perf_counter() - attempt_start) * 1000, 3),
                            http_status=http_status, model_id=attempt_model, finish_reason=finish,
                            usage=usage, error_code=error_code))
                        if not retryable or number > self.config.max_retries:
                            raise ProviderError(error_code or "provider_failure")
                        # A long Retry-After is not shortened: return control instead of retrying too early.
                        if delay_hint is not None and delay_hint > self.config.retry_max_seconds:
                            raise ProviderError(error_code or "provider_rate_limited")
                        delay = max(delay_hint or 0, min(self.config.retry_max_seconds,
                            self.config.retry_base_seconds * 2 ** (number - 1) * random.uniform(0.75, 1.25)))
                        await asyncio.sleep(delay)
        except TimeoutError:
            raise ProviderError("provider_total_timeout", telemetry("provider_total_timeout")) from None
        except ProviderError as error:
            raise ProviderError(error.code, telemetry(error.code)) from None
        finally:
            if acquired:
                self._limiter.release()

    @staticmethod
    def _envelope(content: bytes) -> CompletionEnvelope:
        try:
            return CompletionEnvelope.model_validate_json(content)
        except (ValidationError, ValueError):
            raise ProviderError("provider_invalid_envelope") from None

    @staticmethod
    def _usage(value: dict | None) -> TokenUsage | None:
        if value is None:
            return None
        try:
            return TokenUsage.model_validate({key: value[key] for key in TokenUsage.model_fields if key in value})
        except ValidationError:
            raise ProviderError("provider_invalid_usage") from None

    @staticmethod
    def _require_json_object(content: str):
        if len(content.encode("utf-8")) > 256000:
            raise ProviderError("provider_response_too_large")
        def invalid_constant(value):
            raise ValueError("nonfinite_json")
        def unique_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result
        try:
            value = json.loads(content, parse_constant=invalid_constant, object_pairs_hook=unique_keys)
            if not isinstance(value, dict):
                raise ValueError("object_required")
        except (ValueError, RecursionError):
            raise ProviderError("provider_invalid_json") from None

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                return None
        return max(0, seconds) if math.isfinite(seconds) else None
