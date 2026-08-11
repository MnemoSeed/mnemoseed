"""SessionEnd hook: settle the session (drain + end) so buffered turns run the
F2-F4 funnel. Fail-open by construction.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
