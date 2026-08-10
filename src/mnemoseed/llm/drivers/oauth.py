"""OAuth driver: stub seam only (PRD-02 T6).

A provider that needs OAuth is not built yet. This driver registers the correct
name so config can route to it, but ``chat()`` always raises the typed
``OAuthNotImplemented`` (an ``LLMUnavailable`` subclass, FR-2.6) and ``check()``
reports ``not_configured`` — the console's live-check button shows a failed probe, never
a crash. No OAuth flow is attempted anywhere.
"""

from __future__ import annotations

from typing import Any

from mnemoseed.llm.registry import LLM_DRIVERS, register
from mnemoseed.llm.types import (
    ChatResult,
    HealthReport,
    LLMDriverInfo,
    OAuthNotImplemented,
)

STUB_MESSAGE = "oauth driver is a stub: the OAuth flow is not yet implemented"


@register(LLM_DRIVERS)
class OAuthLLM:
    """(Stub) OAuth-protected chat provider — the flow is not yet implemented."""

    info = LLMDriverInfo(
        name="oauth",
        description="(stub) OAuth-protected chat provider — flow not yet implemented",
    )

    def __init__(self, base_url: str = "", model: str = "", **kwargs: Any) -> None:
        self.base_url = base_url
        self.api_key = ""
        self.model = model
        self.params: dict[str, Any] = kwargs

    def chat(self, *, system: str, user: str) -> ChatResult:
        del system, user
        raise OAuthNotImplemented(STUB_MESSAGE)

    def check(self) -> HealthReport:
        return HealthReport(
            ok=False,
            detail={"status": "not_configured", "error": STUB_MESSAGE},
        )
