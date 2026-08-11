"""PreCompact hook: flush the in-flight turn, never settle the session.

Calls POST /flush after a context compaction so the buffered turn is rescued
before the transcript is rewritten. Fail-open by construction.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemoseed_hook_client import handle_pre_compact  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_pre_compact, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
