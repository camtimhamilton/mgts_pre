"""Survey text interpretation for the pair API. Corporate records stay on the local path."""
import asyncio
import json
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Literal
from weakref import WeakKeyDictionary

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.config import DeepSeekConfig
from app.deepseek import DeepSeekProvider
from app.knowledge import ROOT
from app.llm import ProviderError
from app.serialization import canonical, digest

PIPELINE = "pair-text-2.1.0"
_work_locks = WeakKeyDictionary()


@lru_cache
def resources():
    rules = yaml.safe_load((ROOT / "knowledge_base/pulse_text_v2.yaml").read_text("utf-8"))
    prompts = [(ROOT / f"prompts/pair_{kind}_v2.md").read_text("utf-8")
               for kind in ("signals", "validation")]
    allowed = json.loads((ROOT / "tests/fixtures/pair_synthetic_text.json").read_text("utf-8"))
    policy = json.loads((ROOT / "knowledge_base/v2/policy.json").read_text("utf-8"))
    for role, factors in rules["rules"].items():
        if not set(factors) <= policy["roles"][role].keys():
            raise ValueError("invalid_pair_text_factor")
    return rules, prompts, digest(allowed), policy


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Candidate(Schema):
    factor_id: str
    question_id: str
    quote: str = Field(min_length=1, max_length=2000)
    assertion: str = Field(min_length=1, max_length=600)
    temporal_status: Literal["current", "historical", "resolved", "hypothetical", "uncertain"]
    polarity: Literal["negative", "positive", "neutral"]
    severity: Literal["none", "moderate", "high"]


class Extraction(Schema):
    signals: list[Candidate] = Field(max_length=40)


class Decision(Schema):
    index: int = Field(ge=0)
    status: Literal["accepted", "review", "rejected"]


class Review(Schema):
    decisions: list[Decision] = Field(max_length=40)


# Temporal/quoted language requires contextual review. Only explicit, present-tense
# descriptions can activate a local factor. A question or suggestion never suffices.
CAUTION = re.compile(r"\b(если|раньше|ранее|был[аои]?|прошл\w*|больше не|устран\w*|решен\w*|возможно|например|якобы|допустим|цитата|написал\w*|сказал\w*|отрицал\w*)\b|[?«»\"“”]|не (?:то чтобы|соглас\w*|думаю|считаю|испытываю)", re.I)
ATTRIBUTED = re.compile(r"\b(?:неправда|неверно|считает|считают|говорит|говорят|думает|утвержда\w*|отрица\w*|нельзя сказать|не могу сказать|сомневаюсь|не факт|по словам|предположим|вдруг|якобы|пример)\b", re.I)
EXIT = re.compile(r"^(?:я )?(?:решил[а]?|планирую|намерен[а]?) (?:досрочно (?:завершить|прекратить) стажировку|уйти со стажировки досрочно)[.!]?$", re.I)
RESOLVED = re.compile(r"(?:проблем[аы] (?:с |со )?(.{1,48}?) (?:полностью )?(?:решена|решены|устранена|устранены)|больше нет проблем с (.{1,48}))[.!]?$", re.I)
TOPICS = {"support": "обратн|поддержк", "workload": "нагрузк|загрузк", "clarity": "постановк|понятност",
          "team": "коммуникац|взаимодейств", "deadlines": "срок", "autonomy": "самостоятель"}


def local_signals(record, config):
    result = []
    role = record["role"]
    rules = config["rules"]
    for question, answer in record["texts"].items():
        if not answer:
            continue
        for match in re.finditer(r"[^.!?;\n]+[.!?]?", answer):
            quote = match.group().strip()
            if not quote:
                continue
            base = {"question_id": question, "quote": quote, "assertion": quote,
                    "temporal_status": "current", "polarity": "negative", "severity": "moderate"}
            for factor, pattern in config.get("restored", {}).items():
                if factor in rules[role] and re.fullmatch(pattern + r"[.!]?", quote, re.I):
                    result.append({**base, "factor_id": factor, "temporal_status": "resolved",
                                   "polarity": "positive", "severity": "none", "validation_status": "accepted"})
            if role == "intern" and EXIT.fullmatch(quote):
                result.append({**base, "factor_id": "satisfaction", "critical": True,
                               "severity": "none", "validation_status": "review"})
                continue
            resolved = RESOLVED.fullmatch(quote)
            if resolved:
                topic = resolved.group(1) or resolved.group(2)
                for factor, pattern in TOPICS.items():
                    if factor in rules[role] and re.search(pattern, topic, re.I):
                        result.append({**base, "factor_id": factor, "temporal_status": "resolved",
                                       "polarity": "positive", "severity": "none", "validation_status": "accepted"})
                continue
            cautious = CAUTION.search(quote) or ATTRIBUTED.search(quote)
            for factor, pattern in config.get("positive", {}).get(role, {}).items():
                positive = re.search(pattern, quote, re.I)
                if positive:
                    uncertain = cautious or re.search(r"\bне\s*$", quote[:positive.start()], re.I)
                    result.append({**base, "factor_id": factor, "polarity": "positive", "severity": "none",
                                   "validation_status": "review" if uncertain else "accepted"})
            for factor, patterns in rules[role].items():
                if any(re.search(pattern, quote, re.I) for pattern in patterns):
                    result.append({**base, "factor_id": factor,
                                   "temporal_status": "uncertain" if cautious else "current",
                                   "validation_status": "review" if cautious else "accepted"})
    return result


async def deepseek_signals(visible, prompts, factors, cfg, audit):
    providers = {name: DeepSeekProvider(cfg, name, limiter=_limiter(cfg.max_concurrency))
                 for name in ("signal_extractor", "semantic_validator")}
    async def call(component, prompt, payload, schema):
        result = await providers[component].generate_structured(prompt, canonical(payload), schema.model_json_schema())
        if result.telemetry:
            audit["calls"].append(result.telemetry.model_dump(mode="json"))
        return schema.model_validate_json(result.raw)
    extraction = await call("signal_extractor", prompts[0], {**visible, "factors": factors}, Extraction)
    output = []
    valid = []
    for candidate in extraction.signals:
        signal = candidate.model_dump()
        signal["validation_status"] = "rejected"
        if (candidate.factor_id in factors and candidate.question_id in visible["texts"]
                and candidate.quote.strip() and candidate.quote in (visible["texts"][candidate.question_id] or "")):
            valid.append(len(output))
        output.append(signal)
    if valid:
        decisions = await call("semantic_validator", prompts[1], {**visible, "factors": factors,
            "candidates": [output[index] for index in valid]}, Review)
        if sorted(item.index for item in decisions.decisions) != list(range(len(valid))):
            raise ProviderError("incomplete_semantic_validation")
        for item in decisions.decisions:
            output[valid[item.index]]["validation_status"] = item.status
    return output


@lru_cache
def _limiter(size):
    return asyncio.Semaphore(size)


def configuration():
    rules, prompts, _, policy = resources()
    mode = os.getenv("PULSE_TEXT_MODE", "local_rules")
    if mode not in ("local_rules", "local_encoder", "deepseek"):
        raise ValueError("invalid_pulse_text_mode")
    cfg = DeepSeekConfig.from_env() if mode == "deepseek" else None
    encoder_id = None
    if mode == "local_encoder":
        from app.local_encoder import encoder_version
        encoder_id = encoder_version()
    version = digest([PIPELINE, mode, rules, prompts, policy, encoder_id, cfg.model_dump() if cfg else None,
                      os.getenv("PULSE_NLP_RUN_ID", "default")])
    return mode, cfg, version


async def enrich(record: dict) -> dict:
    mode, cfg, version = configuration()
    rules, prompts, allowlisted, policy = resources()
    payload = {"role": record["role"], "texts": record["texts"]}
    identity = [record[k] for k in ("pair_id", "intern_id", "month", "source_id", "version")]
    key = digest([version, identity, payload])
    loop = asyncio.get_running_loop()
    if loop not in _work_locks:
        _work_locks[loop] = [asyncio.Lock() for _ in range(32)]
    async with _work_locks[loop][int(key[:2], 16) % 32]:
        return await _cached_extract(record, key, mode, cfg, version, payload, rules, prompts, allowlisted, policy)


async def _cached_extract(record, key, mode, cfg, version, payload, rules, prompts, allowlisted, policy):
    path = Path(os.getenv("MGTS_DATABASE", "data/demo.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS pair_text_cache(key TEXT PRIMARY KEY,result TEXT NOT NULL)")
        saved = conn.execute("SELECT result FROM pair_text_cache WHERE key=?", (key,)).fetchone()
    if saved:
        return {**record, **json.loads(saved[0])}
    audit = {"mode": mode, "calls": [], "pipeline": PIPELINE, "config_hash": version}
    signals = []
    status = "completed"
    try:
        if not any(payload["texts"].values()):
            status = "empty"
        elif mode in ("local_rules", "local_encoder"):
            signals = local_signals(record, rules)
            if mode == "local_encoder":
                from app.local_encoder import EncoderUnavailable, rank
                try:
                    topics = await asyncio.to_thread(rank, record, policy["roles"][record["role"]])
                except EncoderUnavailable:
                    raise ProviderError("local_encoder_unavailable") from None
                validated = {(s["question_id"], s["factor_id"], s["quote"]) for s in signals
                             if s["validation_status"] == "accepted"}
                signals.extend(topic for topic in topics
                               if (topic["question_id"], topic["factor_id"], topic["quote"]) not in validated)
                audit["topic_candidates"] = len(topics)
        elif digest(payload) != allowlisted:
            raise ProviderError("external_processing_not_authorized")
        else:
            signals = await deepseek_signals(payload, prompts, list(policy["roles"][record["role"]]), cfg, audit)
    except (ProviderError, ValueError) as exc:
        status = "error"
        audit["error"] = exc.code if isinstance(exc, ProviderError) else "invalid_provider_output_or_config"
        if getattr(exc, "telemetry", None):
            audit["calls"].append(exc.telemetry.model_dump(mode="json"))
    for i, signal in enumerate(signals):
        signal.update({k: record[k] for k in ("role", "pair_id", "intern_id", "month", "source_id", "version")})
        signal["rule_id"] = f'text.{record["role"]}.{signal["factor_id"]}'
        signal["signal_id"] = digest([key, i, signal])[:24]
    result = {"signals": signals, "nlp_version": version, "text_analysis_status": status, "nlp_audit": audit}
    with sqlite3.connect(path, timeout=10) as conn:
        conn.execute("INSERT OR IGNORE INTO pair_text_cache VALUES(?,?)", (key, canonical(result)))
        winner = conn.execute("SELECT result FROM pair_text_cache WHERE key=?", (key,)).fetchone()
    return {**record, **json.loads(winner[0])}
