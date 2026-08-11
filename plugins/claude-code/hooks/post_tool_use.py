"""PostToolUse hook: capture relevant tool results into the in-flight turn.

Fail-open by construction; emits an empty response for every captured tool.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemoseed_hook_client import handle_post_tool_use  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_post_tool_use, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
