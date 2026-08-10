"""PRD-02 T7 boot-recovery crash tests (NFR-2.3; FR-2.4, FR-2.5).

Every scenario "crashes" the daemon by constructing the journaled state the
crashed process would have left on disk -- snapshot files with the right phase
markers -- and then boots the REAL daemon app (``daemon/app.py`` via
TestClient), which runs app.py's boot-recovery loop over that state. No
processes are killed; recovery is exercised through the exact production wiring
(trigger -> FileSnapshotter -> ReflectOrchestrator -> Merger -> consumed-ids
safe-clear), so the idempotency invariants hold against the real seam calls.

Covered crash points:
  1. crash after SNAPSHOT_DONE before REFLECT_DONE -> boot reflect+merge+purge
     exactly once, and the delta-overflow chunk ids survive the safe-clear.
  2. crash after REFLECT_DONE before the merge commit -> boot resumes at the
     merge boundary: the LLM seam is never called again, the journaled payload
     commits, purge fires exactly once.
  3. crash after the merge commit but BEFORE journal termination -> boot re-runs
     the merge idempotently (reinforce in place, never a duplicate node), then
     terminates; a boot whose journal is already merge-done never re-purges the
     stale source rows (MERGE_DONE marker guard).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mnemoseed.daemon.app import create_app
from mnemoseed.dream import (
    DreamState,
    DreamTrigger,
    Merger,
    ReflectOrchestrator,
    SnapshotPhase,
    StubReflectLLM,
    write_snapshot_file,
)
from mnemoseed.dream import FileSnapshotter as RealSnapshotter
from mnemoseed.dream.ledger import year_month_for
from mnemoseed.schema.stamp import ChunkStamp, CognitiveTier, Cues, Provenance
from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
from mnemoseed.storage.ports import AuditFilter, NodeFilter, Page, TurnRange
from mnemoseed.storage.registry import (
    EMBED_DRIVERS,
    GRAPH_DRIVERS,
    META_DRIVERS,
    VECTOR_DRIVERS,
    register,
)

_SESSION = "sess-boot-1"
_PROFILE = "prof-boot"

_LOW_BUDGET_TOKENS = 60
# An 1800-char chunk far exceeds a 60-token delta budget (its block alone is
# ~475 tokens), so the low-budget boot arm forces a partial overflow.
_GIANT_TEXT = "I prefer dark mode. " * 100


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _serving_config_toml(tmp_path: Path, token_budget_usd: float | None = None) -> Path:
    # as_posix(): Windows backslashes are invalid escapes in TOML strings
    cfg = tmp_path / "config.toml"
    body = (
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n'
    )
    if token_budget_usd is not None:
        body += f"[dream]\ntoken_budget_usd = {token_budget_usd}\n"
    cfg.write_text(body, encoding="utf-8")
    return cfg


def _shim_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, token_budget_usd: float | None = None
) -> None:
    """Point CONFIG_PATH and the snapshot CONFIG_DIR at the throwaway store."""
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.setattr(
        "mnemoseed.config.CONFIG_PATH",
        _serving_config_toml(tmp_path, token_budget_usd),
    )
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)


def _seed_into(
    lance_uri: Path,
    *rows: tuple[str, str, int, int],
) -> tuple[lancedb_embedded.LanceDbEmbeddedStore, SyntheticEmbedder]:
    """Insert verbatim chunk rows into the exact lance uri the boot config
    reserves, so the daemon's capture/purge seams see them."""
    store = lancedb_embedded.LanceDbEmbeddedStore(uri=lance_uri, dimensions=64)
    embed = SyntheticEmbedder(dimension=64)
    for chunk_id, text, turn_start, turn_end in rows:
        vec = embed.embed(text)
        store.upsert_chunk(
            ChunkStamp(
                chunk_id=chunk_id,
                profile_id=_PROFILE,
                text=text,
                cognitive_tier=CognitiveTier.TIER_1,
                model_id="test-model",
                cues=Cues(entities=["test"]),
                provenance=Provenance(asserted_by="user", session_id=_SESSION, source="manual"),
                turn_start=turn_start,
                turn_end=turn_end,
            ),
            vec.dense,
            vec.sparse,
        )
    return store, embed


class _BootCountingStub(StubReflectLLM):
    """The deterministic offline seam counting every boot-time reflect chat, so
    a merge-boundary recovery can PROVE reflect was never re-run across the
    restart. Counted on the class so multiple TestClient boots accumulate."""

    count = 0

    def chat(self, *, system: str, user: str) -> str:
        type(self).count += 1
        return super().chat(system=system, user=user)


def _graph_rows(path: Path, profile: str = _PROFILE) -> list[Any]:
    """Read graph nodes through a fresh test-thread connection: the daemon's
    sqlite connections are bound to the TestClient portal thread and refuse
    cross-thread use, but WAL lets a reader coexist with the closed app."""
    driver = sqlite_graph.SqliteGraphDriver(path=path)
    try:
        return [n for n in driver.list_nodes(NodeFilter(profile_id=profile), Page(limit=10)).items]
    finally:
        asyncio.run(driver.close())


# ---------------------------------------------------------------- scenario 1
# Crash after SNAPSHOT_DONE before REFLECT_DONE: boot runs the reflect boundary
# over the ORIGINAL scope, purges exactly the consumed ids, and the delta-
# overflow chunk ids survive for a later dream.


def test_boot_crash_after_snapshot_reflects_merges_purges_and_preserves_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot journaled only at SNAPSHOT_DONE is resumed at the reflect
    boundary on boot. Under a forced low delta budget the reflect pass covers
    only the small chunk; after commit the small chunk is purged exactly once
    and the giant overflow chunk is STILL in the vector store (FR-2.5 never
    drop, NFR-2.3 idempotent recovery)."""
    store, _embed = _seed_into(
        tmp_path / "chunks.lance",
        ("seed-1", "I prefer dark mode", 0, 1),
        ("seed-2", _GIANT_TEXT, 2, 3),
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=store, meta=seed_meta, directory=tmp_path / "dreams")
    assert seeder.request(_PROFILE, TurnRange(0, 3)).ok  # the crashed process captured, no reflect
    asyncio.run(store.close())
    asyncio.run(seed_meta.close())

    _BootCountingStub.count = 0
    monkeypatch.setattr("mnemoseed.daemon.app.StubReflectLLM", _BootCountingStub)

    def low_budget_packer(*_args: object, **_kwargs: object) -> object:
        # any budget override still honors an explicit budget (T5 seam)
        from mnemoseed.dream.delta import DeltaPacker

        return DeltaPacker(budget_tokens=_LOW_BUDGET_TOKENS)

    monkeypatch.setattr("mnemoseed.dream.reflect.DeltaPacker", low_budget_packer)
    _shim_config(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        assert isinstance(trigger, DreamTrigger)
        # boot completed the whole chain: reflect ran exactly once on the seam
        assert _BootCountingStub.count == 1
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        assert trigger.status(_PROFILE).current_range is None
        # the covered scope was purged; the overflow chunk survived
        assert client.app.state.stores.vector.get_chunk("seed-1") is None
        assert client.app.state.stores.vector.get_chunk("seed-2") is not None
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1

    rows = _graph_rows(tmp_path / "cortex.db")
    assert len(rows) == 1  # the small-chunk triple committed
    assert rows[0].props["object"] == "dark mode"


# ---------------------------------------------------------------- scenario 2
# Crash after REFLECT_DONE before the merge commit: boot resumes at the merge
# boundary ONLY; the LLM seam is never called again.


def test_boot_crash_after_reflect_resumes_at_merge_never_reruns_reflect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crashed process's journal carries REFLECT_DONE + the result payload.
    Boot must resume at the MERGING stage (never DREAMING): the counting seam
    proves reflect's LLM call count stays zero, the journaled payload commits,
    the safe-clear purges exactly once, and the journal terminates so a second
    boot finds nothing to recover."""
    _BootCountingStub.count = 0
    monkeypatch.setattr("mnemoseed.daemon.app.StubReflectLLM", _BootCountingStub)

    store = lancedb_embedded.LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=64)
    embed = SyntheticEmbedder(dimension=64)
    vec = embed.embed("I prefer dark mode")
    store.upsert_chunk(
        ChunkStamp(
            chunk_id="seed-1",
            profile_id=_PROFILE,
            text="I prefer dark mode",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["test"]),
            provenance=Provenance(asserted_by="user", session_id=_SESSION, source="manual"),
            turn_start=0,
            turn_end=1,
        ),
        vec.dense,
        vec.sparse,
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=store, meta=seed_meta, directory=tmp_path / "dreams")
    captured = seeder.request(_PROFILE, TurnRange(0, 1)).snapshot
    assert captured is not None
    # crash-safe journal entry: reflect said done, the daemon died pre-merge
    assert ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path / "dreams").reflect(captured).ok
    asyncio.run(store.close())
    asyncio.run(seed_meta.close())

    _shim_config(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        assert _BootCountingStub.count == 0  # reflect NEVER re-ran across the restart
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        assert client.app.state.stores.vector.get_chunk("seed-1") is None  # purged exactly once
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1

    rows = _graph_rows(tmp_path / "cortex.db")
    assert len(rows) == 1  # the journaled payload committed exactly once
    assert rows[0].props["object"] == "dark mode"

    with TestClient(create_app()) as client:
        assert client.app.state.dream.status(_PROFILE).state is DreamState.IDLE
    assert len(_graph_rows(tmp_path / "cortex.db")) == 1  # second boot: no-op


# ---------------------------------------------------------------- scenario 3
# Crash after the merge commit but BEFORE the journal terminated: the graph row
# exists, the file is still REFLECT_DONE. Boot re-runs the merge-boundary, which
# REINFORCES the existing node in place -- never a duplicate graph write -- then
# terminates the journal. A boot whose journal is already merge-done is a no-op:
# the MERGE_DONE guard blocks any re-purge of the stale source rows.


def test_boot_crash_between_merge_commit_and_journal_termination_reinforces_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent write-back across restarts: re-running a committed merge
    reinforces the exact (subject, predicate, object) node in place
    (reinforce_count 2), never duplicates the row, and the safe-clear fires
    exactly once as the journal terminates."""
    store = lancedb_embedded.LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=64)
    embed = SyntheticEmbedder(dimension=64)
    vec = embed.embed("I prefer dark mode")
    store.upsert_chunk(
        ChunkStamp(
            chunk_id="seed-1",
            profile_id=_PROFILE,
            text="I prefer dark mode",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["test"]),
            provenance=Provenance(asserted_by="user", session_id=_SESSION, source="manual"),
            turn_start=0,
            turn_end=1,
        ),
        vec.dense,
        vec.sparse,
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=store, meta=seed_meta, directory=tmp_path / "dreams")
    captured = seeder.request(_PROFILE, TurnRange(0, 1)).snapshot
    assert captured is not None
    outcome = ReflectOrchestrator(llm=StubReflectLLM(), directory=tmp_path / "dreams").reflect(captured)
    assert outcome.ok and outcome.result is not None

    # the crashed process committed the merge but died BEFORE the purge could
    # mark the journal merge-done (on_committed=None => no safe-clear fired)
    crashed_graph = sqlite_graph.SqliteGraphDriver(path=tmp_path / "cortex.db")
    committer = Merger(graph_main=crashed_graph, graph_isolated=None, meta=seed_meta)
    assert committer.merge(captured, outcome.result).committed
    asyncio.run(crashed_graph.close())
    asyncio.run(store.close())
    asyncio.run(seed_meta.close())

    _BootCountingStub.count = 0
    monkeypatch.setattr("mnemoseed.daemon.app.StubReflectLLM", _BootCountingStub)
    _shim_config(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        assert _BootCountingStub.count == 0  # merge-boundary, reflect not re-run
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        assert client.app.state.stores.vector.get_chunk("seed-1") is None  # safe-clear fired once
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1

    rows = _graph_rows(tmp_path / "cortex.db")
    assert len(rows) == 1  # NO double graph write
    assert rows[0].props["object"] == "dark mode"
    assert rows[0].reinforce_count == 2  # the re-run reinforced in place, never duplicated

    with TestClient(create_app()) as client:
        assert client.app.state.dream.status(_PROFILE).state is DreamState.IDLE
    assert len(_graph_rows(tmp_path / "cortex.db")) == 1  # a further boot is a no-op


def test_boot_merge_done_marker_guard_never_repurges_stale_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The MERGE_DONE marker is the idempotency guard: a journal that already
    carries merge_done (the crash hit mid-purge, after the marker) boots into a
    no-op -- the stale source row is neither re-purged nor re-written, exactly
    once per the design/02 leftover-rows rule (NFR-2.3)."""
    store = lancedb_embedded.LanceDbEmbeddedStore(uri=tmp_path / "chunks.lance", dimensions=64)
    embed = SyntheticEmbedder(dimension=64)
    vec = embed.embed("I prefer dark mode")
    store.upsert_chunk(
        ChunkStamp(
            chunk_id="seed-1",
            profile_id=_PROFILE,
            text="I prefer dark mode",
            cognitive_tier=CognitiveTier.TIER_1,
            model_id="test-model",
            cues=Cues(entities=["test"]),
            provenance=Provenance(asserted_by="user", session_id=_SESSION, source="manual"),
            turn_start=0,
            turn_end=1,
        ),
        vec.dense,
        vec.sparse,
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=store, meta=seed_meta, directory=tmp_path / "dreams")
    captured = seeder.request(_PROFILE, TurnRange(0, 1)).snapshot
    assert captured is not None
    # the crashed process marked merge-done (purge started) but died before the
    # per-row deletes finished
    merged = captured.with_phase(SnapshotPhase.REFLECT_DONE.value).with_phase(SnapshotPhase.MERGE_DONE.value)
    write_snapshot_file(tmp_path / "dreams", merged)
    asyncio.run(store.close())
    asyncio.run(seed_meta.close())

    _BootCountingStub.count = 0
    monkeypatch.setattr("mnemoseed.daemon.app.StubReflectLLM", _BootCountingStub)
    _shim_config(tmp_path, monkeypatch)
    with TestClient(create_app()) as client:
        trigger = client.app.state.dream
        assert _BootCountingStub.count == 0  # nothing to reflect: journal terminated
        assert trigger.status(_PROFILE).state is DreamState.IDLE
        # the stale source row is NOT re-purged and NOT re-written
        assert client.app.state.stores.vector.get_chunk("seed-1") is not None
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1
    assert len(_graph_rows(tmp_path / "cortex.db")) == 0  # no graph write happened at all


# ---------------------------------------------------------------- FR-2.5b wiring
# The daemon boot must build the REAL TokenLedger from config (not a stub) and
# inject it into the orchestrator, so the monthly budget gate and the ledger
# meter apply end-to-end on the production seam.


def test_boot_dream_budget_cap_defers_capture_only_and_audits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config token_budget_usd below the projected spend turns the reflect
    boundary into capture-only mode at boot: the counting LLM seam never fires,
    the snapshot stays journaled (nothing purged), no graph write happens, and
    the refusal lands in the audit trail as a token_budget_cap entry."""
    store, _embed = _seed_into(
        tmp_path / "chunks.lance",
        ("seed-1", "I prefer dark mode", 0, 1),
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=store, meta=seed_meta, directory=tmp_path / "dreams")
    assert seeder.request(_PROFILE, TurnRange(0, 1)).ok
    asyncio.run(store.close())
    asyncio.run(seed_meta.close())

    _BootCountingStub.count = 0
    monkeypatch.setattr("mnemoseed.daemon.app.StubReflectLLM", _BootCountingStub)
    _shim_config(tmp_path, monkeypatch, token_budget_usd=1e-9)
    with TestClient(create_app()) as client:
        # capture-only: the budget gate short-circuits BEFORE any cloud call
        assert _BootCountingStub.count == 0
        # the trigger still owns the dream, paused at the reflect boundary
        # (DREAMING, never completed -- on_reflect_complete did not fire)
        assert client.app.state.dream.status(_PROFILE).state is DreamState.DREAMING
        # the snapshot stays journaled at the reflect boundary; nothing purged
        assert client.app.state.stores.vector.get_chunk("seed-1") is not None
        assert len(list((tmp_path / "dreams").glob("*.json"))) == 1
        # the refusal is on the audit trail
        page = client.app.state.stores.meta.audit_query(
            AuditFilter(action="token_budget_cap"), Page(limit=10)
        )
        assert len(page.items) == 1
        entry = page.items[0]
        assert entry.actor == "dream"
        assert entry.detail["profile_id"] == _PROFILE
        assert entry.detail["year_month"] == year_month_for(time.time())
    assert len(_graph_rows(tmp_path / "cortex.db")) == 0  # capture-only: never reached the merger


def test_boot_dream_records_ledger_usage_survives_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the default budget the daemon-built ledger meters the reflect pass
    into the meta DB, and a restart boots a FRESH TokenLedger that reads the
    SAME persisted current-month usage back — proof the ledger writes survive
    the daemon lifecycle and are keyed by the real UTC month."""
    store, _embed = _seed_into(
        tmp_path / "chunks.lance",
        ("seed-1", "I prefer dark mode", 0, 1),
    )
    seed_meta = sqlite_meta.SqliteMetaDriver(path=tmp_path / "meta.db")
    seeder = RealSnapshotter(store=store, meta=seed_meta, directory=tmp_path / "dreams")
    assert seeder.request(_PROFILE, TurnRange(0, 1)).ok
    asyncio.run(store.close())
    asyncio.run(seed_meta.close())

    _BootCountingStub.count = 0
    monkeypatch.setattr("mnemoseed.daemon.app.StubReflectLLM", _BootCountingStub)
    _shim_config(tmp_path, monkeypatch)  # default token_budget_usd = 5.0
    month = year_month_for(time.time())
    with TestClient(create_app()) as client:
        assert _BootCountingStub.count == 1  # within budget: the reflect pass ran
        used = client.app.state.stores.meta.token_usage(_PROFILE, month)
        assert used > 0
    # restart against the same store: a fresh ledger reads the persisted usage
    with TestClient(create_app()) as client:
        assert client.app.state.stores.meta.token_usage(_PROFILE, month) == used
