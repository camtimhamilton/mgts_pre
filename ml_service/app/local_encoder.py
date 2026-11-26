"""Optional local sentence encoder for provisional evidence topic matching.

Weights and tokenizer are explicitly prepared and hash-checked locally. Inference
never downloads files or sends text to a remote service. Matches need review and
carry zero severity.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from threading import Lock

MODEL_ID = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
ARTIFACTS = {
    "model_quint8_avx2.onnx": {
        "remote": "onnx/model_quint8_avx2.onnx", "size": 118453870,
        "sha256": "98a01d88b7de996cdea58c32ca71208c09968d143798814b2ea09d3439dc334f",
    },
    "tokenizer.json": {
        "remote": "tokenizer.json", "size": 9081518,
        "sha256": "2c3387be76557bd40970cec13153b3bbf80407865484b209e655e5e4729076b8",
    },
}
MIN_SIMILARITY = 0.35
MIN_MARGIN = 0.03
MATCHING_VERSION = "provisional-topic-v1"


class EncoderUnavailable(RuntimeError):
    """The explicitly selected local encoder cannot run with available assets."""


def model_directory() -> Path:
    root = Path(__file__).resolve().parents[2]
    return Path(os.getenv("PULSE_ENCODER_DIR", str(root / "data/models/pair-encoder"))).resolve()


def encoder_version() -> str:
    configuration = [MODEL_ID, REVISION, ARTIFACTS, MATCHING_VERSION, MIN_SIMILARITY, MIN_MARGIN, "masked_mean_l2", 128]
    fingerprint = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()[:20]
    return f"{MATCHING_VERSION}:{fingerprint}"


def check_artifacts(directory: Path) -> None:
    for name, metadata in ARTIFACTS.items():
        path = directory / name
        if not path.is_file() or path.stat().st_size != metadata["size"]:
            raise EncoderUnavailable("encoder_artifact_missing_or_size_mismatch")
        checksum = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
        if checksum.hexdigest() != metadata["sha256"]:
            raise EncoderUnavailable("encoder_artifact_hash_mismatch")


@lru_cache(maxsize=2)
def _runtime(directory: str):
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError:
        raise EncoderUnavailable("encoder_optional_dependencies_missing") from None
    check_artifacts(Path(directory))
    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(Path(directory) / "model_quint8_avx2.onnx"),
                                   sess_options=options, providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(str(Path(directory) / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=128)
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    return np, session, tokenizer, Lock()


def embed(sentences: list[str]):
    np, session, tokenizer, lock = _runtime(str(model_directory()))
    if not sentences:
        return np.zeros((0, 384), dtype=np.float32)
    with lock:
        encoded = tokenizer.encode_batch(sentences)
        arrays = {
            "input_ids": np.asarray([item.ids for item in encoded], dtype=np.int64),
            "attention_mask": np.asarray([item.attention_mask for item in encoded], dtype=np.int64),
            "token_type_ids": np.asarray([item.type_ids for item in encoded], dtype=np.int64),
        }
        tokens = session.run(None, {item.name: arrays[item.name] for item in session.get_inputs()})[0]
    mask = arrays["attention_mask"][..., None].astype(np.float32)
    means = (tokens * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1e-9)
    return means / np.maximum(np.linalg.norm(means, axis=1, keepdims=True), 1e-9)


@lru_cache(maxsize=32)
def _topics(directory: str, names: tuple[str, ...]):
    return embed(list(names))


def rank(record: dict, role_policy: dict) -> list[dict]:
    """Return review-only topical matches with exact excerpts from saved text."""
    factors = [(key, value["name"]) for key, value in role_policy.items()]
    if not factors:
        return []
    excerpts = []
    for question_id, text in record.get("texts", {}).items():
        if not text:
            continue
        for match in re.finditer(r"[^.!?\n]+[.!?]?", text):
            quote = match.group().strip()
            if 5 <= len(quote) <= 2000:
                excerpts.append((question_id, quote))
    excerpts = excerpts[:32]
    if not excerpts:
        return []
    topics = _topics(str(model_directory()), tuple(name for _, name in factors))
    similarities = embed([quote for _, quote in excerpts]) @ topics.T
    signals = []
    for (question_id, quote), values in zip(excerpts, similarities):
        ordered = values.argsort()[::-1]
        index = int(ordered[0])
        similarity = float(values[index])
        margin = similarity - float(values[ordered[1]]) if len(ordered) > 1 else similarity
        if similarity < MIN_SIMILARITY or margin < MIN_MARGIN:
            continue
        factor_id, name = factors[index]
        signals.append({
            "factor_id": factor_id, "question_id": question_id, "quote": quote,
            "assertion": f"Возможная тема комментария — {name}. Требуется проверка смысла и временного статуса.",
            "polarity": "neutral", "severity": "none", "temporal_status": "uncertain",
            "validation_status": "review", "similarity": round(similarity, 6), "margin": round(margin, 6),
            "encoder_version": encoder_version(), "method": "local_neural_topic_matching",
            "calibration_status": "provisional_synthetic_smoke_only",
        })
    return signals
