"""Seed a believable demo memory graph for the Graph View decay showcase.

Generates a fresh profile with mixed node types / Tiers, a visibly fading
region (old, low-decay_weight memories), a conflict pair, and provenance
variance — all deterministic for a given seed. Writes through the real storage
ports (SqliteMetaDriver / SqliteGraphDriver) into an ISOLATED home that never
touches the dogfood profile.

Run (uv):

    uv run python scripts/seed_demo_graph.py --nodes 300 --home ~/.mnemoseed-demo

Then start the daemon against that home:

    $env:MNEMOSEED_HOME="$HOME\.mnemoseed-demo"; uv run mnemoseed daemon

Safety: the default home is ``~/.mnemoseed-demo``; pointing ``--home`` at the
real ``~/.mnemoseed`` is refused unless ``--force`` is passed.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from mnemoseed.config import CONFIG_DIR
from mnemoseed.schema.graph import GraphNode, NodeType, RelType, validate_node_payload
from mnemoseed.schema.stamp import Provenance
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.ports import StoredProfile

DEFAULT_HOME = Path.home() / ".mnemoseed-demo"
DEFAULT_PROFILE = "demo"
DEFAULT_NODES = 300
DEFAULT_SEED = 0
DEFAULT_EDGES_PER_NODE = 4

_DAY = 86400.0
_YEAR = 365 * _DAY

# believable per-type statement corpus for the demo (English, public-safe)
_STATEMENTS: dict[NodeType, tuple[str, ...]] = {
    NodeType.PREFERENCE: (
        "prefers tabs for indentation",
        "prefers quiet work hours in the morning",
        "likes short review meetings with a written agenda",
        "prefers postgres over managed alternatives",
        "likes dark mode and monospace fonts",
        "prefers incremental rollouts over big-bang releases",
    ),
    NodeType.HABIT: (
        "writes a failing test before each fix",
        "runs the full suite before every merge",
        "commits after every working step",
        "reads the changelog before upgrading",
        "starts each day by triaging the backlog",
    ),
    NodeType.EPISODE: (
        "debugged a flaky e2e suite by bisecting the seed data",
        "shipped the first console write surface after the design review",
        "migrated the storage layer to per-profile schemas",
        "rewrote the decay sweep to re-read config every tick",
        "added the audit actor attribution across every write path",
    ),
    NodeType.SKILL_SEQUENCE: (
        "diagnose: capture a failing case, bisect, isolate, fix, regression-test",
        "migrate: freeze schema, dual-write, backfill, verify, cut over",
        "release: build, smoke, stage, canary, general availability",
        "review: read the diff, run the suite, check the docs sync",
    ),
    NodeType.DECISION: (
        "adopted the hand-rolled three.js layer over the forcegraph library",
        "kept the version chain append-only for reconciliation",
        "chose the embedded preset as the default storage mode",
        "made the console a pure client over the daemon REST surface",
    ),
    NodeType.CONSTRAINT: (
        "audit log rows are append-only — never update or delete",
        "capture scoring never reads anima or preferences",
        "memory plaintext stays encrypted at rest",
        "a storage driver must declare its capability set honestly",
    ),
    NodeType.INTENTION: (
        "trigger: nightly batch complete; action: run the consolidation pass",
        "trigger: quarterly review; action: rebalance the decay lambdas",
    ),
}

_PREFERENCE_PROPS = {
    "domain": "workflow",
    "statement": "",
    "valence": 0.8,
    "prior_width": 0.3,
    "trait_anchor": "anima-1",
    "evidence_chain": [{"event": "seeded", "at": 0.0}],
}

_ASSERTED_BY = ("codex", "claude-code", "gemini", "dream-engine", "user")
_SOURCES = ("session://demo-s1", "session://demo-s2", "session://demo-s3", "manual")
_REL_TYPES = (RelType.EVIDENCED_BY, RelType.CONTAINS, RelType.HAS, RelType.USED_IN, RelType.MASTERED)


def _node_props(node_type: NodeType, statement: str) -> dict:
    """Per-type props that pass the frozen payload schema (schema/graph.py)."""
    if node_type is NodeType.PREFERENCE:
        props = dict(_PREFERENCE_PROPS)
        props["statement"] = statement
        return props
    if node_type is NodeType.HABIT:
        return {"statement": statement}
    if node_type is NodeType.EPISODE:
        return {"summary": statement, "session_ref": "demo-session"}
    if node_type is NodeType.SKILL_SEQUENCE:
        return {"task_type": "engineering", "tool_chain": ["uv", "pytest", "three.js"], "success_rate": 0.82}
    if node_type is NodeType.DECISION:
        return {"statement": statement}
    if node_type is NodeType.CONSTRAINT:
        return {"rule": statement, "severity": "hard"}
    if node_type is NodeType.INTENTION:
        return {"trigger_condition": "demo hook", "action": statement, "status": "pending"}
    return {}


def _pick_type(rng: random.Random, fading: bool) -> NodeType:
    """Weighted node-type draw; the fading region skews to episodic memories."""
    if fading:
        return rng.choices(
            (NodeType.EPISODE, NodeType.PREFERENCE, NodeType.HABIT),
            weights=(5, 3, 1),
            k=1,
        )[0]
    return rng.choices(
        (
            NodeType.PREFERENCE,
            NodeType.HABIT,
            NodeType.EPISODE,
            NodeType.SKILL_SEQUENCE,
            NodeType.DECISION,
            NodeType.CONSTRAINT,
            NodeType.INTENTION,
        ),
        weights=(30, 20, 20, 12, 10, 5, 3),
        k=1,
    )[0]


def build_demo_nodes(rng: random.Random, count: int, profile_id: str) -> list[GraphNode]:
    """Deterministic demo graph nodes.

    Decay variance: age drives decay (old memories are faint), a dedicated
    fading region clusters ~15% of the nodes at decay_weight 0.05..0.2, fresh
    memories sit at 0.75..1.0, and constraints never decay.
    """
    now = time.time()
    nodes: list[GraphNode] = []
    fading_size = max(3, int(count * 0.15))
    statements_by_type = {t: list(words) for t, words in _STATEMENTS.items()}
    for index in range(count):
        in_fading = index < fading_size
        node_type = _pick_type(rng, in_fading)
        if index < 2:
            # the first two nodes are always PREFERENCE so the demo conflict
            # pair has guaranteed members at any --nodes size
            node_type = NodeType.PREFERENCE
        pool = statements_by_type[node_type]
        statement = rng.choice(pool)
        if len(pool) > 1:
            pool.remove(statement)

        if in_fading:
            age_days = rng.uniform(220, 365)
            decay_weight = rng.uniform(0.05, 0.2)
            tier = 3
        elif index % 6 == 2:
            # a deterministic tier-2 band keeps every tier populated in the
            # demo (mixed Tiers is part of the believable-shape requirement)
            age_days = rng.uniform(90, 220)
            decay_weight = rng.uniform(0.45, 0.7)
            tier = 2
        else:
            age_days = rng.uniform(0, 365) ** 0.7  # recent-biased
            age_frac = age_days / 365
            decay_weight = max(0.25, min(1.0, 1.0 - age_frac * 0.75 + rng.uniform(-0.08, 0.06)))
            if node_type is NodeType.CONSTRAINT:
                decay_weight = 1.0
            tier = 1 if decay_weight > 0.72 else 2 if decay_weight > 0.45 else 3
        if node_type is NodeType.INTENTION:
            tier = 1
        created_at = now - age_days * _DAY

        props = _node_props(node_type, statement)
        validate_node_payload(node_type, props)

        provenance = Provenance(
            asserted_by=rng.choice(_ASSERTED_BY),
            session_id=rng.choice(_SOURCES),
            source="demo-seed",
            confidence=round(rng.uniform(0.6, 0.95), 2),
            asserted_at=created_at,
            history=[
                {
                    "at": created_at,
                    "action": "created",
                    "actor": "demo-seed",
                    "detail": {"seed": index},
                }
            ],
        )
        nodes.append(
            GraphNode(
                node_id=f"demo-{index:04d}",
                profile_id=profile_id,
                node_type=node_type,
                entities=[f"entity-{index % 12}"],
                props=props,
                confidence=provenance.confidence,
                decay_weight=round(decay_weight, 3),
                never_decay=node_type is NodeType.CONSTRAINT,
                cognitive_tier=tier,
                provenance=provenance,
                created_at=created_at,
                updated_at=created_at,
                valid_from=created_at,
            )
        )
    return nodes


def add_conflict_pair(nodes: list[GraphNode], rng: random.Random) -> tuple[str, str]:
    """Two contradictory PREFERENCE nodes sharing one conflict group."""
    pair = rng.sample([n for n in nodes if n.node_type is NodeType.PREFERENCE], 2)
    left, right = pair
    left.props["statement"] = "prefers tabs for indentation"
    right.props["statement"] = "prefers spaces for indentation"
    group = f"demo-conflict-{left.node_id[-4:]}-{right.node_id[-4:]}"
    left.conflict_flag = True
    right.conflict_flag = True
    left.conflict_group = group
    right.conflict_group = group
    return left.node_id, right.node_id


def seed_graph(
    graph: SqliteGraphDriver,
    meta: SqliteMetaDriver,
    *,
    profile_id: str,
    node_count: int,
    seed: int,
) -> dict:
    """Write the demo profile: nodes + relation/cooccurrence edges."""
    from mnemoseed.schema.graph import Edge

    rng = random.Random(seed)
    meta.upsert_profile(StoredProfile(profile_id=profile_id, display_name="Demo memory graph"))
    nodes = build_demo_nodes(rng, node_count, profile_id)
    conflict_ids = add_conflict_pair(nodes, rng)
    for node in nodes:
        graph.upsert_node(node)

    relation_edges = 0
    cooccurrence_edges = 0
    for index, node in enumerate(nodes):
        # hubs (top 10%) connect more; the fading region keeps a few weak
        # links so the fade is visible in context, not in isolation
        degree = rng.randint(3, 6) if index < node_count * 0.1 else rng.randint(1, 3)
        for _ in range(degree):
            other = rng.choice(nodes)
            if other.node_id == node.node_id:
                continue
            if rng.random() < 0.3:
                graph.bump_cooccurrence(node.node_id, other.node_id, profile_id)
                cooccurrence_edges += 1
            else:
                graph.add_edge(
                    Edge(
                        src=node.node_id,
                        dst=other.node_id,
                        rel=rng.choice(_REL_TYPES),
                        weight=round(rng.uniform(0.3, 1.0), 2),
                        profile_id=profile_id,
                        created_at=max(node.created_at, other.created_at),
                    )
                )
                relation_edges += 1
    return {
        "nodes": len(nodes),
        "relation_edges": relation_edges,
        "cooccurrence_edges": cooccurrence_edges,
        "conflict_pair": conflict_ids,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the demo memory graph (Graph View decay showcase).")
    parser.add_argument(
        "--home", type=Path, default=DEFAULT_HOME, help="demo config home (default ~/.mnemoseed-demo)"
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="fresh profile id to seed (default demo)")
    parser.add_argument("--nodes", type=int, default=DEFAULT_NODES, help="node count (default 300)")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="deterministic seed")
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow --home to point at the real config home (~/.mnemoseed)",
    )
    args = parser.parse_args(argv)

    home = args.home.expanduser().resolve()
    real_home = CONFIG_DIR.resolve()
    if home == real_home and not args.force:
        print(
            f"error: --home points at the real config home {real_home} — "
            "refusing to mix demo data into the live store. Use --home ~/.mnemoseed-demo "
            "or pass --force if you really mean it.",
            file=sys.stderr,
        )
        return 2
    if args.nodes < 2 or args.nodes > 10_000:
        print("error: --nodes must be within [2, 10000]", file=sys.stderr)
        return 2

    home.mkdir(parents=True, exist_ok=True)
    meta = SqliteMetaDriver(path=home / "meta.db")
    graph = SqliteGraphDriver(path=home / "graph.db")
    import asyncio

    try:
        summary = seed_graph(
            graph,
            meta,
            profile_id=args.profile,
            node_count=args.nodes,
            seed=args.seed,
        )
    finally:
        asyncio.run(graph.close())
        asyncio.run(meta.close())

    print(
        f"seeded {summary['nodes']} nodes / {summary['relation_edges']} relation edges / "
        f"{summary['cooccurrence_edges']} cooccurrence edges -> {home}"
    )
    left, right = summary["conflict_pair"]
    print(f"conflict pair: {left} vs {right} (flagged, shared group)")
    print("start the daemon against this home:")
    print(f"  $env:MNEMOSEED_HOME='{home}'; uv run mnemoseed daemon")
    print("then open http://localhost:7788/console/#/graph")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
