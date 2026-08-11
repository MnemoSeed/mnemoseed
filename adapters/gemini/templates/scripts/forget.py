"""/forget command: remove one specific memory from the profile.

Targets a provenance unit exactly: a chunk id, a graph node id, or an entity
name (every chunk/node tagged with it is removed — see design/05 forget_this).
Removal is request-only; the daemon owns the write.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))

from mnemoseed_hook_client import HookBudget, resolve_profile_id  # noqa: E402


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="/forget",
        description="Remove one memory (chunk, node, or entity) from the profile.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--chunk", help="chunk id to delete")
    group.add_argument("--node", help="graph node id to tombstone")
    group.add_argument("--entity", help="entity name; every chunk/node tagged with it is removed")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1
    budget = HookBudget()
    body = budget.post(
        "/memory/forget_this",
        {
            "profile_id": resolve_profile_id(),
            "chunk_id": args.chunk,
            "node_id": args.node,
            "entity": args.entity,
        },
    )
    if body is None:
        print("mnemoseed: daemon not reachable; start it with `mnemoseed up`")
        return 0
    removed = body.get("removed") if isinstance(body.get("removed"), dict) else {}
    chunks = removed.get("chunks") if isinstance(removed.get("chunks"), list) else []
    nodes = removed.get("nodes") if isinstance(removed.get("nodes"), list) else []
    if not chunks and not nodes:
        print("mnemoseed: nothing matched that target.")
    else:
        print(f"mnemoseed: removed {len(chunks)} chunk(s) and {len(nodes)} node(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
