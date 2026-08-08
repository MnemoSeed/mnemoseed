"""OpenAICompatibleEmbedder: dense-only HTTP embedder. No network at
construction; embed / connectivity exercised via httpx MockTransport; the
appendix C gate surfaces the sparse_output loss as a warn-level degradation.
"""

import json

import httpx
import pytest

from mnemoseed.storage.drivers.openai_compatible import OpenAICompatibleEmbedder
from mnemoseed.storage.ports import Capability, validate_capabilities
from mnemoseed.storage.registry import EMBED_DRIVERS, register


@pytest.fixture(autouse=True)
def _ensure_registered():
    if not EMBED_DRIVERS.contains("openai_compatible"):
        register(EMBED_DRIVERS)(OpenAICompatibleEmbedder)
    yield


def _embedder(**over: object) -> OpenAICompatibleEmbedder:
    params: dict[str, object] = dict(
        base_url="https://embeddings.test/v1",
        api_key="k",
        model="text-embed-v1",
    )
    params.update(over)
    return OpenAICompatibleEmbedder(**params)  # type: ignore[arg-type]


def _with_transport(embedder: OpenAICompatibleEmbedder, handler: object) -> OpenAICompatibleEmbedder:
    embedder._client = httpx.Client(
        base_url="https://embeddings.test/v1",
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )
    return embedder


def test_no_network_at_construction():
    embedder = _embedder(base_url="http://127.0.0.1:1/v1")
    assert embedder.dimension == 0
    assert embedder.model == "text-embed-v1"


def test_requires_base_url_and_model():
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleEmbedder(base_url="", api_key="k", model="m")
    with pytest.raises(ValueError, match="model"):
        OpenAICompatibleEmbedder(base_url="http://x/v1", api_key="k", model="")


def test_capabilities_declared_dense_batch_only():
    caps = OpenAICompatibleEmbedder.info.capabilities
    assert caps == frozenset({Capability.EMBED_BATCH})
    assert Capability.EMBED_SPARSE_OUTPUT not in caps
    assert Capability.EMBED_LOCAL_INFERENCE not in caps


def _embed_handler(request):
    body = json.loads(request.content)
    inputs = body["input"]
    return httpx.Response(
        200,
        json={"data": [{"index": i, "embedding": [float(i) + 1.0, 0.0]} for i in range(len(inputs))]},
    )


def test_embed_single_text():
    embedder = _with_transport(_embedder(), _embed_handler)
    result = embedder.embed("hello")
    assert result.dense == [1.0, 0.0]
    assert result.sparse is None
    assert embedder.dimension == 2


def test_embed_batch_reorders_by_index():
    def handler(request):
        body = json.loads(request.content)
        data = [{"index": i, "embedding": [float(i) + 1.0]} for i in range(len(body["input"]))][::-1]
        return httpx.Response(200, json={"data": data})

    embedder = _with_transport(_embedder(), handler)
    results = embedder.embed_batch(["a", "b", "c"])
    assert [r.dense[0] for r in results] == [1.0, 2.0, 3.0]


def test_embed_batch_sends_full_input_list():
    sent: dict[str, object] = {}

    def handler(request):
        body = json.loads(request.content)
        sent["input"] = body["input"]
        sent["model"] = body["model"]
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    embedder = _with_transport(_embedder(), handler)
    embedder.embed_batch(["a", "b"])
    assert sent["input"] == ["a", "b"]
    assert sent["model"] == "text-embed-v1"


def test_embed_http_error_raises():
    def handler(request):
        return httpx.Response(500, json={"error": "boom"})

    embedder = _with_transport(_embedder(), handler)
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed("x")


def test_connectivity_lists_models():
    def handler(request):
        # base_url "…/v1" + "/models" joins to "/v1/models" on the wire
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "text-embed-v1"}, {"id": "text-embed-v2"}]})

    embedder = _with_transport(_embedder(), handler)
    result = embedder.connectivity()
    assert result["reachable"] is True
    assert result["models"] == ["text-embed-v1", "text-embed-v2"]


def test_connectivity_reports_http_error():
    def handler(request):
        return httpx.Response(503, json={})

    embedder = _with_transport(_embedder(), handler)
    result = embedder.connectivity()
    assert result["reachable"] is False
    assert "503" in result["error"]


def test_connectivity_reports_network_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    embedder = _with_transport(_embedder(), handler)
    result = embedder.connectivity()
    assert result["reachable"] is False
    assert "refused" in result["error"]


def test_gate_reports_sparse_degradation_only():
    embedder = _embedder()
    report = validate_capabilities({"embed": {"main": embedder}})
    assert report.ok is True
    assert report.hard_missing == []
    degraded = {i.capability for i in report.degradations}
    assert Capability.EMBED_SPARSE_OUTPUT in degraded
    assert Capability.EMBED_BATCH not in degraded
