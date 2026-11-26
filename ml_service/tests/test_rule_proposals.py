"""Offline proposal and human review behavior, including fixed-source provider transport."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.knowledge import ROOT
from app.llm import ProviderResult
from app import rule_proposals as proposals


def fixture_input():
    return proposals.ProposalInput.model_validate_json((ROOT / "tests/fixtures/rule_proposal_synthetic.json").read_text("utf-8"))


def test_manual_draft_and_human_review_are_separate_inactive_artifacts(tmp_path):
    original_policy = (ROOT / "knowledge_base/v2/policy.json").read_bytes()
    body = fixture_input()
    draft = proposals.create_draft(body, "local-author")
    reviewed = proposals.review_draft(draft, "reviewer-1", "approved", "2.0.2-review.1", "Reviewed synthetic demonstration")
    proposals.write_artifact(tmp_path / "draft.json", draft)
    proposals.write_artifact(tmp_path / "reviewed.json", reviewed)
    assert draft.status == "draft" and draft.review is None
    assert reviewed.status == "approved" and reviewed.active is False
    assert reviewed.runtime_activation == "disabled"
    assert reviewed.review.reviewed_draft_hash == draft.artifact_hash
    assert reviewed.artifact_version != draft.artifact_version
    assert [event["event"] for event in reviewed.audit] == ["draft_created", "human_review"]
    assert reviewed.input.source.quote in reviewed.input.source.content
    assert original_policy == (ROOT / "knowledge_base/v2/policy.json").read_bytes()
    assert proposals.ProposalArtifact.model_validate_json((tmp_path / "reviewed.json").read_text("utf-8")) == reviewed


def test_quotes_references_and_unknown_payload_fields_fail_validation():
    body = fixture_input().model_dump(mode="json")
    body["proposal"]["quote"] = "Invented quotation"
    with pytest.raises(ValidationError):
        proposals.ProposalInput.model_validate(body)
    body = fixture_input().model_dump(mode="json")
    body["proposal"]["weights"] = {"support": 100}
    with pytest.raises(ValidationError):
        proposals.ProposalInput.model_validate(body)
    body = fixture_input().model_dump(mode="json")
    body["source"]["quote"] = body["proposal"]["quote"] = "Invented quotation"
    with pytest.raises(ValidationError):
        proposals.ProposalInput.model_validate(body)


@pytest.mark.parametrize("target", [
    {"role": "intern", "factor_id": "unknown", "action_id": "action_intern_unknown"},
    {"role": "intern", "factor_id": "support", "action_id": "action_mentor_support"},
    {"role": "intern", "factor_id": "support", "action_id": "action_pair_support"},
])
def test_unknown_or_incompatible_targets_cannot_create_draft(target):
    body = fixture_input().model_dump(mode="json")
    body["target"] = target
    with pytest.raises(ValueError):
        proposals.create_draft(proposals.ProposalInput.model_validate(body), "author")


def test_scientific_classification_requires_human_source_verification():
    body = fixture_input().model_dump(mode="json")
    body["source"]["classification"] = "verified_scientific"
    with pytest.raises(ValidationError):
        proposals.ProposalInput.model_validate(body)


def test_corporate_manual_input_is_local_and_requires_no_api_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    body = fixture_input().model_dump(mode="json")
    body["source"]["classification"] = "corporate"
    draft = proposals.create_draft(proposals.ProposalInput.model_validate(body), "corporate-author")
    assert draft.generation == {"mode": "manual_local"}
    assert draft.input.source.classification == "corporate"


def test_integrity_staleness_review_and_output_protections(tmp_path, monkeypatch):
    draft = proposals.create_draft(fixture_input(), "author")
    corrupted = draft.model_dump(mode="json")
    corrupted["patch"]["rule"]["title"] = "Tampered rule"
    with pytest.raises(ValidationError):
        proposals.ProposalArtifact.model_validate(corrupted)
    with pytest.raises(ValueError):
        proposals.write_artifact(ROOT / "knowledge_base/v2/proposal-test.json", draft)
    proposals.write_artifact(tmp_path / "draft.json", draft)
    with pytest.raises(FileExistsError):
        proposals.write_artifact(tmp_path / "draft.json", draft)
    with pytest.raises(ValidationError):
        proposals.review_draft(draft, " ", "approved", "review-v1", "checked")
    policy = deepcopy(proposals.load_policy())
    policy["version"] = "changed-version"
    monkeypatch.setattr(proposals, "load_policy", lambda: policy)
    with pytest.raises(ValueError, match="stale_base_policy"):
        proposals.review_draft(draft, "reviewer", "approved", "review-v1", "checked")


def test_optional_extractor_uses_only_pinned_synthetic_source_and_independent_profile(monkeypatch):
    calls = []
    body = fixture_input()
    class FakeProvider:
        def __init__(self, component):
            assert component == "rule_extractor"
        async def generate_structured(self, system_prompt, user_prompt, response_schema):
            calls.append(json.loads(user_prompt))
            return ProviderResult(raw=json.dumps({"status": "proposed", "proposal": body.proposal.model_dump()}),
                                  provider="deepseek", model_id="test-model")
    monkeypatch.setattr("app.deepseek.DeepSeekProvider", FakeProvider)
    draft = asyncio.run(proposals.extract_synthetic("author"))
    assert draft.active is False and draft.status == "draft"
    assert calls[0]["source"] == body.source.model_dump(mode="json")
    assert draft.generation["mode"] == "deepseek_reviewed_synthetic"
    monkeypatch.setattr(proposals, "SYNTHETIC_INPUT_HASH", "0" * 64)
    with pytest.raises(ValueError, match="integrity_mismatch"):
        asyncio.run(proposals.extract_synthetic("author"))
    assert len(calls) == 1


def test_cli_draft_review_and_rejection_history(tmp_path):
    draft = tmp_path / "draft.json"
    reviewed = tmp_path / "reviewed.json"
    base = [sys.executable, "-m", "scripts.rule_proposal"]
    first = subprocess.run(base + ["draft", "--input", str(ROOT / "tests/fixtures/rule_proposal_synthetic.json"),
        "--author", "demo-author", "--output", str(draft)], cwd=ROOT, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["status"] == "draft"
    second = subprocess.run(base + ["review", "--input", str(draft), "--reviewer", "demo-reviewer",
        "--decision", "rejected", "--version", "2.0.2-review.1", "--note", "More evidence required", "--output", str(reviewed)],
        cwd=ROOT, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    artifact = proposals.ProposalArtifact.model_validate_json(reviewed.read_text("utf-8"))
    assert artifact.status == "rejected" and artifact.active is False
    assert artifact.review.note == "More evidence required"
