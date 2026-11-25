"""Load a fresh validated snapshot; no process-global mutable policy."""
from pathlib import Path
import os

import yaml

from app.schemas import KnowledgeSnapshot, Mapping
from app.serialization import canonical, digest

ROOT = Path(__file__).resolve().parents[1]


def read_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ValueError("configuration must be an object")
    return value


def load_knowledge(root: Path = ROOT / "knowledge_base" / "v0.1.0") -> KnowledgeSnapshot:
    manifest = read_yaml(root / "manifest.yaml")
    content = {key: read_yaml(root / f"{key}.yaml")[key]
               for key in ("sources", "factors", "rules", "actions")}
    content.update(version=manifest["version"], status=manifest["status"],
                   policy=read_yaml(root / "scoring_policy.yaml"))
    snapshot = KnowledgeSnapshot.model_validate({**content, "content_hash": digest(content)})
    validate_knowledge(snapshot)
    return snapshot


def validate_knowledge(kb: KnowledgeSnapshot) -> None:
    collections = (kb.factors, kb.rules, kb.actions)
    for items in collections:
        if len({item.id for item in items}) != len(items):
            raise ValueError("duplicate knowledge ID")
    sources = {s.source_id for s in kb.sources}
    if len(sources) != len(kb.sources):
        raise ValueError("duplicate source ID")
    factors = {f.id: f for f in kb.factors}
    actions = {a.id: a for a in kb.actions}
    if set(factors) != set(kb.policy.weights) or kb.policy.weight_origin not in sources:
        raise ValueError("invalid factor/weight/source references")
    if kb.version != kb.policy.version:
        raise ValueError("policy version mismatch")
    for action in kb.actions:
        if not action.targets or not set(action.targets) <= factors.keys():
            raise ValueError("invalid action targets")
        if not action.completion_criteria or not action.resolution_criteria:
            raise ValueError("action criteria required")
    for rule in kb.rules:
        if rule.factor_id not in factors or not rule.id.startswith(f"RULE_{rule.factor_id}_"):
            raise ValueError("rule factor mismatch")
        if not rule.applies_to or not set(rule.applies_to) <= set(factors[rule.factor_id].allowed_roles):
            raise ValueError("invalid rule role")
        if rule.factor_id == "R10" and set(rule.applies_to) != {"intern"}:
            raise ValueError("R10 intern only")
        if not {"moderate", "high"} <= rule.severity.keys() or not rule.exclusions:
            raise ValueError("rule needs severity criteria and exclusions")
        if not rule.action_ids or not set(rule.action_ids) <= actions.keys():
            raise ValueError("invalid rule actions")
        if any(rule.factor_id not in actions[a].targets for a in rule.action_ids):
            raise ValueError("rule action does not target its factor")
        if not rule.source_refs or any(ref.source_id not in sources for ref in rule.source_refs):
            raise ValueError("invalid rule sources")
    for factor in factors:
        if not any(r.factor_id == factor and r.status == kb.status for r in kb.rules):
            raise ValueError("factor needs active rule")


def load_mapping(path: Path | None = None) -> Mapping:
    selected = path or Path(os.getenv("MGTS_MAPPING_PATH", str(ROOT / "mappings" / "demo.yaml")))
    return Mapping.model_validate(read_yaml(selected))
