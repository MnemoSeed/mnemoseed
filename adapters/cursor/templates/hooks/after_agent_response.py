"""Cursor afterAgentResponse hook: full-text assistant response capture (FR-6.3b).

Reads the Cursor hook JSON object on stdin and emits the JSON response on
stdout. Fail-open by construction: on any daemon problem the response is an
empty object, the hook still exits 0, and the agent is unaffected.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mnemoseed_hook_client import handle_after_agent_response  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_after_agent_response, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
