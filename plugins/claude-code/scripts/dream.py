"""/dream command: manual consolidation (FR-2.8).

- ``/dream``        -> consolidation status for the profile
- ``/dream once``   -> run exactly one manual dream cycle (snapshot, reflect,
                       merge, safe-clear) and report what happened
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mnemoseed_hook_client import HookBudget, resolve_profile_id  # noqa: E402


def _print_status(body: dict) -> None:
    print(f"profile: {body.get('profile_id', '?')}")
    print(f"state: {body.get('state', '?')}")
    print(f"pending manual: {body.get('pending_manual', 0)}")
    print(f"queued (during a run): {body.get('pending_queue', 0)}")
    event = body.get("last_event")
    if isinstance(event, dict):
        fired = event.get("fired_at")
        when = "?"
        if isinstance(fired, (int, float)) and not isinstance(fired, bool):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(fired)))
        print(f"last fired event: {event.get('kind', '?')} at {when}")


def main(argv: list[str]) -> int:
    subcommand = argv[0].lower() if argv and argv[0].strip() else "status"
    if subcommand == "once":
        body = HookBudget().post("/memory/dream_once", {"profile_id": resolve_profile_id()})
        if body is None:
            print("mnemoseed: daemon not reachable; start it with `mnemoseed up`")
            return 0
        if body.get("launched"):
            print("mnemoseed: dream consolidation launched (snapshot -> reflect -> merge).")
        else:
            print(f"mnemoseed: nothing to consolidate (state={body.get('state', '?')}).")
        _print_status(body)
        return 0
    if subcommand == "status":
        body = HookBudget().post("/memory/dream_status", {"profile_id": resolve_profile_id()})
        if body is None:
            print("mnemoseed: daemon not reachable; start it with `mnemoseed up`")
            return 0
        _print_status(body)
        return 0
    print("usage: /dream [once | status]")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
