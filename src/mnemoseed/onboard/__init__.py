"""Shared onboard backend (PRD-07 FR-7.13 / design/06 3.1).

``mnemoseed onboard`` and the console setup wizard are two frontends over one
backend service. The backend drives the existing primitives in order and is
skippable + resumable (state persists under the config dir); it never carries a
parallel implementation of any step:

- owner account setup (POST /api/v1/setup, exact-once)
- storage preset choice (config set)
- dream LLM wizard (POST /api/v1/llm/test then /api/v1/llm/routes/{role},
  connectivity-test-before-persist; skippable -> capture-only daemon)
- host link (installer plan + apply, backup + diff + confirmation)
- autostart (installer startup.enable)
- doctor all-green (installer doctor)
"""

from mnemoseed.onboard.service import OnboardService

__all__ = ["OnboardService"]
