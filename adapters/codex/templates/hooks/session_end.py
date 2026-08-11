"""Codex SessionEnd hook: transcript settle (FR-6.3c).

Reads the session transcript (``transcript_path`` rollout.jsonl) for bounded
full-fidelity capture, then settles the session by draining and ending it.
Fail-open by construction.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mnemoseed_hook_client import handle_session_end  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_session_end, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
