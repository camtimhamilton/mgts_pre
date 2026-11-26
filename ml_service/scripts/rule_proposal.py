"""Create and review separate versioned rule proposal artifacts."""
import argparse
import asyncio
import json
from pathlib import Path

from pydantic import ValidationError

from app.llm import ProviderError
from app.rule_proposals import ProposalArtifact, ProposalInput, create_draft, extract_synthetic, review_draft, write_artifact


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("draft")
    draft.add_argument("--input", type=Path, required=True)
    draft.add_argument("--author", required=True)
    extract = commands.add_parser("extract-synthetic")
    extract.add_argument("--author", required=True)
    review = commands.add_parser("review")
    review.add_argument("--input", type=Path, required=True)
    review.add_argument("--reviewer", required=True)
    review.add_argument("--decision", choices=["approved", "rejected"], required=True)
    review.add_argument("--version", required=True)
    review.add_argument("--note", required=True)
    for command in (draft, extract, review):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "draft":
            body = ProposalInput.model_validate_json(args.input.read_text("utf-8"))
            artifact = create_draft(body, args.author)
        elif args.command == "extract-synthetic":
            artifact = asyncio.run(extract_synthetic(args.author))
        else:
            draft_artifact = ProposalArtifact.model_validate_json(args.input.read_text("utf-8"))
            artifact = review_draft(draft_artifact, args.reviewer, args.decision, args.version, args.note)
        write_artifact(args.output, artifact)
    except (ValueError, OSError, ValidationError, ProviderError) as error:
        code = error.code if isinstance(error, ProviderError) else type(error).__name__
        print(json.dumps({"status": "failed", "error_code": code}))
        raise SystemExit(1) from None
    print(json.dumps({"status": artifact.status, "proposal_id": artifact.proposal_id,
        "artifact_version": artifact.artifact_version, "active": artifact.active,
        "runtime_activation": artifact.runtime_activation, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
