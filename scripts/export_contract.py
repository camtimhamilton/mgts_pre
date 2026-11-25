"""Export actual OpenAPI and one synthetic integration sample from a temporary DB."""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.knowledge import ROOT
from app.main import create_app
from app.llm import FixtureProvider


def main():
    destination = ROOT / "docs" / "integration"
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        api = create_app(Path(directory) / "contract.sqlite3", FixtureProvider())
        with TestClient(api) as client:
            survey = json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
            result = client.post("/v1/analyze", json=survey)
            result.raise_for_status()
            request = {"analysis_id": result.json()["analysis_id"], "action_ids": ["A01", "A11"]}
            scenario = client.post("/v1/scenario", json=request)
            scenario.raise_for_status()
            artifacts = {"openapi.json": api.openapi(), "analyze_request.json": survey,
                "analyze_response.json": result.json(), "scenario_request.json": request,
                "scenario_response.json": scenario.json()}
            for name, content in artifacts.items():
                (destination / name).write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("Exported OpenAPI and 4 synthetic request/response examples to docs/integration")


if __name__ == "__main__":
    main()
