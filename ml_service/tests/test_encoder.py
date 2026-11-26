"""Optional encoder invariants without downloading models or using external AI."""
import hashlib
import os

import pytest

from app import local_encoder


def test_encoder_version_is_stable_and_pins_artifacts():
    assert local_encoder.encoder_version() == local_encoder.encoder_version()
    assert len(local_encoder.REVISION) == 40
    assert all(len(item["sha256"]) == 64 for item in local_encoder.ARTIFACTS.values())


def test_missing_and_corrupted_assets_fail_closed(tmp_path, monkeypatch):
    metadata = {"small.onnx": {"size": 4, "sha256": hashlib.sha256(b"good").hexdigest()}}
    monkeypatch.setattr(local_encoder, "ARTIFACTS", metadata)
    with pytest.raises(local_encoder.EncoderUnavailable, match="missing_or_size"):
        local_encoder.check_artifacts(tmp_path)
    (tmp_path / "small.onnx").write_bytes(b"evil")
    with pytest.raises(local_encoder.EncoderUnavailable, match="hash_mismatch"):
        local_encoder.check_artifacts(tmp_path)
    (tmp_path / "small.onnx").write_bytes(b"good")
    local_encoder.check_artifacts(tmp_path)


def fake_embeddings(monkeypatch, scores):
    np = pytest.importorskip("numpy")
    monkeypatch.setattr(local_encoder, "_topics", lambda *args: np.eye(2))
    monkeypatch.setattr(local_encoder, "embed", lambda texts: np.asarray([scores for _ in texts]))


def test_matches_are_exact_review_only_and_zero_severity(monkeypatch):
    fake_embeddings(monkeypatch, [.8, .1])
    text = "  Нагрузка вернулась к комфортному уровню.  "
    matches = local_encoder.rank({"texts": {"difficulties": text}}, {
        "workload": {"name": "Нагрузка"}, "support": {"name": "Обратная связь"}})
    assert len(matches) == 1
    signal = matches[0]
    assert signal["quote"] in text
    assert signal["question_id"] == "difficulties"
    assert signal["factor_id"] == "workload"
    assert signal["severity"] == "none"
    assert signal["polarity"] == "neutral"
    assert signal["temporal_status"] == "uncertain"
    assert signal["validation_status"] == "review"


@pytest.mark.parametrize("scores", [[.2, .1], [.6, .59]])
def test_weak_or_ambiguous_topics_abstain(monkeypatch, scores):
    fake_embeddings(monkeypatch, scores)
    assert local_encoder.rank({"texts": {"comment": "Синтетический неоднозначный текст."}}, {
        "clarity": {"name": "Задачи"}, "quality": {"name": "Качество"}}) == []


def test_empty_input_does_not_initialize_optional_runtime(monkeypatch):
    def unexpected(*args):
        raise AssertionError("runtime must not be initialized")

    monkeypatch.setattr(local_encoder, "embed", unexpected)
    assert local_encoder.rank({"texts": {}}, {"clarity": {"name": "Задачи"}}) == []
    assert local_encoder.rank({"texts": {"comment": "Текст"}}, {}) == []


@pytest.mark.skipif(os.getenv("PULSE_RUN_ENCODER_SMOKE") != "1", reason="requires explicitly prepared local optional model")
def test_real_local_encoder_masked_pooling_and_replay(monkeypatch):
    import socket

    def forbid_network(*args, **kwargs):
        raise AssertionError("local inference must not connect to the network")

    monkeypatch.setattr(socket, "create_connection", forbid_network)
    np = pytest.importorskip("numpy")
    texts = ["Нужна обратная связь наставника.", "Слишком высокая нагрузка."]
    values = local_encoder.embed(texts)
    assert values.shape == (2, 384)
    assert np.allclose(np.linalg.norm(values, axis=1), 1, atol=1e-5)
    assert np.allclose(values, local_encoder.embed(texts), atol=1e-6)
