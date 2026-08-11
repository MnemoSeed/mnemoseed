"""/memory command: profile status — totals and recent activity.

Reads the daemon's timeline and export pages and prints a compact summary.
Fail-open: an unreachable daemon reports itself and returns 0.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))

from mnemoseed_hook_client import HookBudget, resolve_profile_id  # noqa: E402


def _fmt(entry: dict) -> str:
    kind = entry.get("kind") or "event"
    summary = entry.get("summary") or entry.get("text") or ""
    text = " ".join(str(summary).split())
    if entry.get("id"):
        version = entry.get("version")
        middle = f"{entry['id']}" + (f" v{version}" if version is not None else "")
        return f"- [{kind}] {middle} {text[:120]}"
    return f"- [{kind}] {text[:120]}"


def main(argv: list[str]) -> int:
    if any((part.strip().lower() in ("-h", "--help")) for part in argv):
        print("usage: /memory")
        return 0
    profile_id = resolve_profile_id()
    budget = HookBudget()
    timeline = budget.post("/memory/timeline", {"profile_id": profile_id})
    export = budget.post("/memory/export", {"profile_id": profile_id, "limit": 1})
    if timeline is None:
        print("mnemoseed: daemon not reachable; start it with `mnemoseed up`")
        return 0
    print(f"profile: {profile_id}")
    if isinstance(export, dict):
        paging = export.get("paging") if isinstance(export.get("paging"), dict) else {}
        print(f"chunks: {paging.get('chunk_total', '?')}  graph nodes: {paging.get('node_total', '?')}")
    events = timeline.get("events") if isinstance(timeline.get("events"), list) else []
    if not events:
        print("recent activity: none yet")
        return 0
    print(f"recent activity ({len(events)} events):")
    for entry in events[:10]:
        print(_fmt(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
