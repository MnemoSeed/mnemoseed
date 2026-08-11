"""Gemini BeforeAgent hook: per-turn capture + context injection (FR-6.3d).

Capture the incoming request, then a short memory recall (≤200 tokens) within
the same 2s hook deadline. Fail-open by construction.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mnemoseed_hook_client import handle_before_agent  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_before_agent, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
