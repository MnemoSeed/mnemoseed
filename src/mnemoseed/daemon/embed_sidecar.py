"""Embedding sidecar: a dev/stub OpenAI-compatible embeddings endpoint.

The docker preset maps the embed layer to ``openai_compatible``, which needs a
reachable ``/v1/embeddings`` endpoint to be a real stack (AC-2). This sidecar
serves that protocol with deterministic hash-based dense vectors so the compose
stack boots with zero external accounts or model downloads. It is explicitly a
development stub: no sparse output (matching the driver's capability
declaration) and no learned model. Swapping in a production endpoint (TEI,
vLLM, a hosted /v1/embeddings API) is a config change only — point
``storage.embed.base_url`` elsewhere and leave the service down.
"""

from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Sequence
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from mnemoseed import __version__

MODEL_NAME = "text-embedding-synthetic"
DIMENSION = 1024


def _synthetic_vector(text: str) -> list[float]:
    """Deterministic L2-normalized vector from the input text (stub quality)."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [float(digest[i % len(digest)]) / 255.0 for i in range(DIMENSION)]
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def create_app() -> FastAPI:
    """Build the sidecar ASGI app (healthz + OpenAI-compatible endpoints)."""
    app = FastAPI(title="MnemoSeed embedding sidecar (dev stub)", version=__version__)
    app.state.started_at = time.perf_counter()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        elapsed_ms = (time.perf_counter() - app.state.started_at) * 1000.0
        return {
            "status": "ok",
            "service": "embed",
            "model": MODEL_NAME,
            "uptime_ms": round(elapsed_ms, 3),
        }

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "mnemoseed"}],
        }

    def _invalid_input() -> JSONResponse:
        # OpenAI-compatible error envelope for a malformed `input` payload.
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "message": "input must be a string or a non-empty list of strings",
                    "type": "invalid_request_error",
                    "param": "input",
                    "code": None,
                }
            },
        )

    @app.post("/v1/embeddings")
    async def embeddings(payload: dict[str, Any]) -> JSONResponse:
        raw = payload.get("input")
        if isinstance(raw, str):
            texts: Sequence[str] = [raw]
        elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            texts = raw
        else:
            return _invalid_input()
        entries = [
            {"object": "embedding", "index": index, "embedding": _synthetic_vector(text)}
            for index, text in enumerate(texts)
        ]
        return JSONResponse(
            content={"object": "list", "data": entries, "model": payload.get("model", MODEL_NAME)}
        )

    return app


def run_sidecar(host: str, port: int) -> int:
    """Serve the sidecar on the given host/port until shutdown."""
    config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    server.run()
    return 0
