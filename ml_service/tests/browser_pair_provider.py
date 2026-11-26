"""Synthetic-only provider for the offline full-stack browser test."""
import json

from app import pulse_nlp
from app.llm import ProviderResult
from app.main import create_app as create_ml_app


class SyntheticProvider:
    def __init__(self, *_args, **_kwargs):
        pass

    async def generate_structured(self, _prompt, payload, _schema):
        visible = json.loads(payload)
        quote = visible["texts"]["barriers"]
        if visible["role"] != "intern" or not quote.startswith("Уже третий раз жду"):
            raise AssertionError("Browser test provider accepts only the explicit synthetic form")
        output = {"signals": [{
            "factor_id": "S02", "rule_id": "S02_FEEDBACK", "question_id": "barriers", "quote": quote,
            "assertion": "Обратной связи приходится ждать несколько дней", "assertion_type": "explicit",
            "semantic_type": "PROBLEM", "temporal_status": "current", "polarity": "negative",
            "severity": "high", "verdict": "accepted",
        }]}
        return ProviderResult(raw=json.dumps(output), provider="synthetic_test", model_id="browser-fixture")


def create_app():
    pulse_nlp.DeepSeekProvider = SyntheticProvider
    return create_ml_app()
