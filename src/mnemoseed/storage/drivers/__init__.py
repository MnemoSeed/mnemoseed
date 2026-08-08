"""Built-in storage drivers. Importing the package registers each driver into
the per-layer registry (import side effect)."""

from mnemoseed.storage.drivers import (
    bge_m3_onnx,  # noqa: F401
    lancedb_embedded,  # noqa: F401
    openai_compatible,  # noqa: F401
    pg_graph,  # noqa: F401
    pg_meta,  # noqa: F401
    pgvector,  # noqa: F401
    sqlite_graph,  # noqa: F401
    sqlite_meta,  # noqa: F401
    synthetic_embedder,  # noqa: F401
)

__all__ = [
    "bge_m3_onnx",
    "lancedb_embedded",
    "openai_compatible",
    "pg_graph",
    "pg_meta",
    "pgvector",
    "sqlite_graph",
    "sqlite_meta",
    "synthetic_embedder",
]
