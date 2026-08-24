"""Demo-seeding script tests (W3-3, PRD-07 T12).

The demo graph must be believable (mixed types/Tiers, decay variance with a
visibly fading region, a conflict pair, provenance variance), deterministic
for a seed, and isolated — the default home is a demo dir and pointing at the
real config home is refused without --force.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from mnemoseed.schema.graph import NodeType
from mnemoseed.storage.drivers.sqlite_graph import SqliteGraphDriver
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.ports import NodeFilter, Page

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "seed_demo_graph.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("seed_demo_graph", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo():
    return _load_script()


@pytest.fixture()
def seeded(demo, tmp_path):
    """Seed one demo graph per test into a throwaway home; closes the drivers
    at teardown so no test can read a closed store."""
    created: list[tuple[SqliteGraphDriver, SqliteMetaDriver]] = []

    def _make(nodes: int = 60, seed: int = 0):
        meta = SqliteMetaDriver(path=tmp_path / "meta.db")
        graph = SqliteGraphDriver(path=tmp_path / "graph.db")
        summary = demo.seed_graph(graph, meta, profile_id="demo-test", node_count=nodes, seed=seed)
        created.append((graph, meta))
        return summary, graph

    yield _make
    for graph, meta in created:
        asyncio.run(graph.close())
        asyncio.run(meta.close())


def _all_nodes(graph: SqliteGraphDriver) -> list:
    return graph.list_nodes(NodeFilter(profile_id="demo-test"), Page(limit=1000)).items


def test_seed_produces_requested_mixed_graph(seeded) -> None:
    summary, graph = seeded()
    assert summary["nodes"] == 60
    assert summary["relation_edges"] > 0
    assert summary["cooccurrence_edges"] > 0
    left, right = summary["conflict_pair"]
    assert left != right

    nodes = [graph.get_node(nid) for nid in (left, right)]
    assert all(n is not None for n in nodes)
    assert nodes[0].conflict_flag and nodes[1].conflict_flag
    assert nodes[0].conflict_group == nodes[1].conflict_group

    all_nodes = _all_nodes(graph)
    assert len(all_nodes) == 60
    types = {n.node_type for n in all_nodes}
    assert len(types) >= 4  # believable variety, not one type
    assert NodeType.PREFERENCE in types

    # provenance variance: more than one asserted_by across the graph
    assert len({n.provenance.asserted_by for n in all_nodes}) > 1


def test_decay_variance_has_fading_region_and_fresh_memories(seeded) -> None:
    """The decay showcase needs both a visibly fading region (old, faint) and
    fresh vivid memories — plus never-decay constraints at full opacity."""
    summary, graph = seeded(nodes=120)
    del summary
    nodes = _all_nodes(graph)
    weights = [n.decay_weight for n in nodes]
    assert min(weights) <= 0.2, f"no fading region (min decay {min(weights):.2f})"
    assert max(weights) >= 0.9, f"no vivid memories (max decay {max(weights):.2f})"
    constraints = [n for n in nodes if n.node_type is NodeType.CONSTRAINT]
    assert constraints and all(n.never_decay and n.decay_weight == 1.0 for n in constraints)
    # tiers span the range
    assert {n.cognitive_tier for n in nodes} == {1, 2, 3}


def test_seed_is_deterministic_for_a_seed(seeded) -> None:
    _, graph = seeded(nodes=60, seed=7)
    _, graph2 = seeded(nodes=60, seed=7)
    a = sorted((n.node_id, n.node_type.value, n.decay_weight) for n in _all_nodes(graph))
    b = sorted((n.node_id, n.node_type.value, n.decay_weight) for n in _all_nodes(graph2))
    assert a == b


def test_real_config_home_refused_without_force(demo, tmp_path, monkeypatch) -> None:
    """The demo must never silently mix into the live store."""
    monkeypatch.setattr(demo, "CONFIG_DIR", tmp_path)
    assert demo.main(["--home", str(tmp_path), "--nodes", "10"]) == 2
    forced = demo.main(["--home", str(tmp_path), "--nodes", "10", "--force"])
    assert forced == 0
    assert (tmp_path / "graph.db").exists()
    assert (tmp_path / "meta.db").exists()


def test_script_rejects_bad_node_count(demo, tmp_path) -> None:
    assert demo.main(["--home", str(tmp_path / "h"), "--nodes", "1"]) == 2
    assert demo.main(["--home", str(tmp_path / "h"), "--nodes", "999999"]) == 2
