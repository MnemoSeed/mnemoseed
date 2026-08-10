"""``python -m mnemoseed.mcp`` — run the stdio memory gateway."""

from __future__ import annotations

import sys

from mnemoseed.mcp.server import run_server

if __name__ == "__main__":
    sys.exit(run_server())
