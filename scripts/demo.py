"""Run the canonical synthetic end-to-end API flow without starting a server."""
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.knowledge import ROOT
from app.main import create_app
from app.llm import FixtureProvider


def main():
    with tempfile.TemporaryDirectory() as directory:
        with TestClient(create_app(Path(directory) / "demo.sqlite3", FixtureProvider())) as client:
            payload = json.loads((ROOT / "tests/fixtures/survey_demo.json").read_text(encoding="utf-8"))
            response = client.post("/v1/analyze", json=payload)
            response.raise_for_status()
            result = response.json()
            request = {"analysis_id": result["analysis_id"], "action_ids": ["A01", "A11"]}
            scenario = client.post("/v1/scenario", json=request)
            scenario.raise_for_status()
            assert result["risk"]["score"] == 17 and result["risk"]["data_coverage"] == 0.22
            assert result["risk"]["priority"] == "insufficient_data"
            assert scenario.json()["scenario_score"] == 5
            assert client.get("/v1/analysis/" + result["analysis_id"]).json() == result
            print(json.dumps({"synthetic": True, "provider": result["metadata"]["provider"],
                "score": result["risk"]["score"], "coverage": result["risk"]["data_coverage"],
                "priority": result["risk"]["priority"], "gap": result["discrepancies"][0]["normalized_gap"],
                "scenario": scenario.json(), "status": result["status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
