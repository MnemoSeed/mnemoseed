"""Dream LLM port, built-in drivers, and role routing (PRD-02 T6; FR-2.14).

T6 rounds out the dream engine's cost layer: the DreamLLM port (narrow chat +
typed health probe + provider-reported usage), an LLM driver registry mirroring
the storage idiom, four payload drivers (openai_compatible / anthropic /
ollama / oauth — the last one reuses the host's Codex / Grok login state), the
[dream.llm] role router, and the ReflectLLM adapter that makes any DreamLLM
usable where T3 types strictly against str-returning chat. Importing this
package registers the built-in drivers (import side effect), as the storage
package does.

FR-2.14 route defaults follow design/02 §6: deep_reflection -> Claude Sonnet
(anthropic), short_increment -> an OpenAI-compatible chat class, local_track ->
a local Ollama model. See mnemoseed.config.DEFAULT_LLM_ROUTES.
"""

from __future__ import annotations

from mnemoseed.llm import drivers  # noqa: F401 - import side effect registers drivers
from mnemoseed.llm.adapters import ReflectLLMAdapter
from mnemoseed.llm.registry import LLM_DRIVERS, LLMRegistry, register
from mnemoseed.llm.routing import RoleRouter
from mnemoseed.llm.types import (
    ChatResult,
    DreamLLM,
    HealthReport,
    LLMDriverInfo,
    LLMError,
    LLMRouteError,
    LLMUnavailable,
    OAuthNotImplemented,
    UnknownLLMDriverError,
    Usage,
)

__all__ = [
    "ChatResult",
    "DreamLLM",
    "HealthReport",
    "LLMDriverInfo",
    "LLMError",
    "LLMRouteError",
    "LLMRegistry",
    "LLMUnavailable",
    "LLM_DRIVERS",
    "OAuthNotImplemented",
    "ReflectLLMAdapter",
    "RoleRouter",
    "UnknownLLMDriverError",
    "Usage",
    "register",
]
