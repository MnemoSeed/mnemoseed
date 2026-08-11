"""/recall command: query MnemoSeed memory for the relevant entries.

Prints a terse, budgeted context block on stdout. Never raises; a quiet failure
reports the daemon as unreachable and returns 0 so the agent can continue.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))

from mnemoseed_hook_client import COMMAND_BUDGET_TOKENS  # noqa: E402
from mnemoseed_hook_client import (  # noqa: E402
    HookBudget,
    build_context,
    post_recall,
    resolve_profile_id,
)


def main(argv: list[str]) -> int:
    query = " ".join((part for part in argv if part.strip())).strip()
    if not query:
        print("usage: /recall <query>")
        return 1
    recall = post_recall(
        HookBudget(),
        resolve_profile_id(),
        query,
        budget_tokens=COMMAND_BUDGET_TOKENS,
    )
    if recall is None:
        print("mnemoseed: daemon not reachable; start it with `mnemoseed up`")
        return 0
    context = build_context(recall, budget_tokens=COMMAND_BUDGET_TOKENS)
    print(context if context else "mnemoseed: no matching memory found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
