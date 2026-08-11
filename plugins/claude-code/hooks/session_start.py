"""SessionStart hook: warm-up context injection (FR-6.3).

Reads the Claude Code hook JSON object on stdin and emits the JSON response on
stdout. Fail-open by construction: on any daemon problem the response is an
empty object, the hook still exits 0, and the agent is unaffected.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemoseed_hook_client import handle_session_start  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_session_start, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
