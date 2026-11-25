"""Environment-only provider selection and bounded DeepSeek component profiles."""
import os
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from app.schemas import Model

Component = Literal["signal_extractor", "rule_extractor", "semantic_validator"]


class ComponentConfig(Model):
    model: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    thinking: bool = False
    max_tokens: int = Field(default=4096, ge=1, le=32768)


class DeepSeekConfig(Model):
    base_url: Literal["https://api.deepseek.com", "https://api.deepseek.com/v1"] = "https://api.deepseek.com"
    signal_extractor: ComponentConfig = ComponentConfig(model="deepseek-flash")
    rule_extractor: ComponentConfig = ComponentConfig(model="deepseek-v4-pro")
    semantic_validator: ComponentConfig = ComponentConfig(model="deepseek-v4-pro")
    timeout_seconds: float = Field(default=30, ge=0.1, le=120)
    total_timeout_seconds: float = Field(default=90, ge=0.1, le=300)
    queue_timeout_seconds: float = Field(default=2, ge=0.01, le=30)
    max_retries: int = Field(default=2, ge=0, le=3)
    max_concurrency: int = Field(default=2, ge=1, le=16)
    retry_base_seconds: float = Field(default=0.5, ge=0, le=5)
    retry_max_seconds: float = Field(default=5, ge=0, le=30)

    @model_validator(mode="after")
    def valid_retry_bounds(self):
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("invalid_retry_bounds")
        return self

    @classmethod
    def from_env(cls):
        values = {}
        try:
            for name in ("timeout_seconds", "total_timeout_seconds", "queue_timeout_seconds", "max_retries",
                         "max_concurrency", "retry_base_seconds", "retry_max_seconds", "base_url"):
                value = os.getenv("DEEPSEEK_" + name.upper())
                if value is not None:
                    values[name] = value
            for component in ("signal_extractor", "rule_extractor", "semantic_validator"):
                defaults = cls.model_fields[component].default.model_dump()
                prefix = "DEEPSEEK_" + component.upper() + "_"
                for name in ("model", "max_tokens", "thinking"):
                    value = os.getenv(prefix + name.upper())
                    if value is not None:
                        if name == "thinking":
                            if value not in {"0", "1"}:
                                raise ValueError("invalid_boolean")
                            value = value == "1"
                        defaults[name] = value
                values[component] = defaults
            return cls.model_validate(values)
        except (ValidationError, ValueError):
            raise ValueError("invalid_deepseek_configuration") from None


def provider_name() -> Literal["mock", "deepseek"]:
    name = os.getenv("MGTS_PROVIDER", "mock")
    if name not in {"mock", "deepseek"}:
        raise ValueError("unsupported_llm_provider")
    return name
