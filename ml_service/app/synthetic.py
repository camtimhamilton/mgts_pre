"""Fail-closed egress policy: only exact, reviewed synthetic SurveyPeriod inputs.

This policy is server configuration, never a user-controlled request flag.
It does not attempt to classify arbitrary personal data as anonymous.
"""
import json

from app.knowledge import ROOT
from app.llm import ProviderError
from app.schemas import SurveyPeriod
from app.serialization import digest


class SyntheticInputPolicy:
    def __init__(self):
        manifest = json.loads((ROOT / "tests/fixtures/synthetic_allowlist.json").read_text(encoding="utf-8"))
        self.hashes = frozenset(manifest["survey_hashes"])

    def require_allowed(self, survey: SurveyPeriod):
        if digest(survey.model_dump(mode="json")) not in self.hashes:
            raise ProviderError("synthetic_input_not_allowlisted")
