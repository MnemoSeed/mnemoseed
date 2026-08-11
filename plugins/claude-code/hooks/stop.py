"""Stop hook: capture the final assistant message, never block the stop.

Honors the stop-hook loop guard (``stop_hook_active``): a Stop hook that runs
again within one turn fast-exits without touching the daemon. No decision is
ever returned — settlement belongs to SessionEnd.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemoseed_hook_client import handle_stop  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    print_json,
    read_stdin_json,
    run_hook,
)


def main() -> int:
    output = run_hook(handle_stop, read_stdin_json())
    print_json(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
