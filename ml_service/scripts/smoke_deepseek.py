"""Explicit paid smoke on the fixed synthetic demo, never a quality benchmark."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from app.deepseek import DeepSeekProvider
from app.knowledge import ROOT, load_knowledge, load_mapping
from app.llm import ProviderError
from app.orchestrator import Pipeline
from app.schemas import SurveyPeriod
from app.storage import SQLiteSnapshots


def skip_reason() -> str | None:
    if os.getenv("MGTS_RUN_DEEPSEEK_SMOKE") != "1":
        return "MGTS_RUN_DEEPSEEK_SMOKE is not 1"
    if not os.getenv("DEEPSEEK_API_KEY", "").strip():
        return "DEEPSEEK_API_KEY is absent"
    if os.getenv("MGTS_ALLOW_SYNTHETIC_LLM") != "1":
        return "MGTS_ALLOW_SYNTHETIC_LLM is not 1"
    return None


async def run_smoke() -> dict:
    reason = skip_reason()
    if reason:
        return {"status": "skipped", "reason": reason}
    # Only this local reviewed file, no arbitrary --input or network-supplied data.
    survey = SurveyPeriod.model_validate_json((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        pipeline = Pipeline(load_knowledge(), load_mapping(), DeepSeekProvider(),
                            SQLiteSnapshots(Path(directory) / "synthetic_smoke.sqlite3"))
        result = await pipeline.analyze(survey)
    errors = [c.error_code for c in result.metadata.provider_calls if c.error_code]
    return {"status": "failed" if result.status == "partial" or errors else "passed",
        "scope": "synthetic transport/schema smoke; not NLP accuracy", "provider": result.metadata.provider,
        "model_id": result.metadata.model_id, "score_observed": result.risk.score,
        "coverage_observed": result.risk.data_coverage, "accepted_signals": result.metadata.accepted_count,
        "review_signals": result.metadata.review_count, "rejected_signals": result.metadata.rejected_count,
        "error_codes": errors, "calls": [c.model_dump(mode="json") for c in result.metadata.provider_calls]}


def main():
    try:
        report = asyncio.run(run_smoke())
    except ProviderError as error:
        report = {"status": "failed", "error_code": error.code,
                  "call": error.telemetry.model_dump(mode="json") if error.telemetry else None}
    except ValueError:
        report = {"status": "failed", "error_code": "invalid_provider_configuration"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
