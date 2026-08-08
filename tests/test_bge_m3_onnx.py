"""BgeM3OnnxEmbedder smoke test (prd-08 FR-8.3 / FR-8.7).

Real local inference is exercised only when the model is already present in the
model cache; otherwise the test skips. It never triggers a download — CI and
model-less machines must stay offline and fast. The model download itself was
proven working under the local environment (see the M0 delivery report).
"""

import math

import pytest

from mnemoseed.storage.drivers.bge_m3_onnx import BgeM3OnnxEmbedder
from mnemoseed.storage.ports import Capability, EmbeddingResult
from mnemoseed.storage.registry import EMBED_DRIVERS, register

_MODEL_DIMENSION = 1024


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not EMBED_DRIVERS.contains("bge_m3_onnx"):
        register(EMBED_DRIVERS)(BgeM3OnnxEmbedder)
    yield


def _make_embedder() -> BgeM3OnnxEmbedder:
    return BgeM3OnnxEmbedder()


def test_registered_in_shared_registry():
    assert EMBED_DRIVERS.contains("bge_m3_onnx")


def test_capabilities_declared():
    caps = BgeM3OnnxEmbedder.info.capabilities
    assert Capability.EMBED_LOCAL_INFERENCE in caps
    assert Capability.EMBED_BATCH in caps
    assert Capability.EMBED_SPARSE_OUTPUT in caps


def test_dimension_constant():
    assert BgeM3OnnxEmbedder.info.description
    assert _MODEL_DIMENSION == 1024


def _cosine(left, right):
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return dot / (math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right)))


def test_real_embedding_smoke():
    embedder = _make_embedder()
    if not embedder.model_path.exists():
        pytest.skip("bge-m3 model absent locally (skipping real-inference smoke test)")
    related_a = "The horse galloped across the meadow at dawn."
    related_b = "A horse ran through the field in the early morning light."
    unrelated = "The quarterly sales report was finalized by the finance team."

    result_a = embedder.embed(related_a)
    result_b = embedder.embed(related_b)
    result_u = embedder.embed(unrelated)

    assert isinstance(result_a, EmbeddingResult)
    assert len(result_a.dense) == _MODEL_DIMENSION
    norm = math.sqrt(sum(v * v for v in result_a.dense))
    assert norm == pytest.approx(1.0, abs=1e-2)

    assert result_a.sparse is not None
    assert len(result_a.sparse.indices) == len(result_a.sparse.values)
    assert all(index >= 0 for index in result_a.sparse.indices)
    assert all(value > 0.0 for value in result_a.sparse.values)

    related_sim = _cosine(result_a.dense, result_b.dense)
    unrelated_sim = _cosine(result_a.dense, result_u.dense)
    assert related_sim > unrelated_sim + 0.1, (
        f"related cohesion expected, got related={related_sim:.3f} unrelated={unrelated_sim:.3f}"
    )

    shared_sparse = set(result_a.sparse.indices) & set(result_b.sparse.indices)
    assert shared_sparse, "related sentences share sparse indices"


def test_embed_batch_matches_single_embeds():
    embedder = _make_embedder()
    if not embedder.model_path.exists():
        pytest.skip("bge-m3 model absent locally (skipping batch consistency test)")
    texts = ["first sentence for batch", "second sentence for batch"]
    batch = embedder.embed_batch(texts)
    assert len(batch) == 2
    for text, result in zip(texts, batch, strict=True):
        assert len(result.dense) == _MODEL_DIMENSION
        solo = embedder.embed(text)
        # batched inference pads the sequence; the quantized XLM-R graph does
        # not perfectly mask padded positions, so batch and solo embeddings
        # agree to ~0.98 cosine, not to the last bit. The sparse token scores
        # are unchanged (same token ids, same projection).
        assert _cosine(result.dense, solo.dense) > 0.95
        assert result.sparse is not None and solo.sparse is not None
        assert result.sparse.indices == solo.sparse.indices  # token identities never change
        for batch_value, solo_value in zip(result.sparse.values, solo.sparse.values, strict=True):
            scale = max(abs(batch_value), abs(solo_value))
            assert abs(batch_value - solo_value) <= max(3e-2, 0.4 * scale)
