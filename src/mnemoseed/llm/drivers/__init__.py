"""Built-in DreamLLM drivers (PRD-02 T6; FR-2.14 / FR-2.7).

Importing this package registers every driver into ``mnemoseed.llm.registry``
(import side effect, as the storage package does): openai_compatible
(short-increment cloud class), anthropic (deep-reflection Claude), ollama
(local offline track), oauth (stub seam — the OAuth flow is not yet built).
"""

from __future__ import annotations

from mnemoseed.llm.drivers import (  # noqa: F401 - import side effect registers drivers
    anthropic,
    oauth,
    ollama,
    openai_compatible,
)

__all__ = ["anthropic", "oauth", "ollama", "openai_compatible"]
