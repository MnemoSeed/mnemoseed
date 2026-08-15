# PRD-08 · M0 Foundation (Skeleton + Storage Interfaces + Schema Freeze)

> Design docs: [03-storage-and-retrieval](../design/03-storage-and-retrieval.md) §1/§3, [06-host-integration](../design/06-host-integration.md) §3
> Milestone: M0 (m0a + m0b of roadmap PRD-00) · Estimate 16 days (revised by v2 blind review)
> Nature: pure foundation with zero user-visible functionality — but what it freezes (schema, interface contracts) hurts every time it changes later, so this PRD has the strictest review standard of all PRDs.
> v2 revision: after independent blind-reviewer review (15 findings), added the interface method list (Appendix B), the degradation behavior table (Appendix C), and completed the schema-freeze fields (profile isolation / structured turn boundaries / flag bits / usage counters / sparse-vector representation).
> v1.1 amendment (2026-08-13): Appendix B.2 adds GraphStore `list_edges(filter, page)` (bulk edge read for the console Graph View), the capability-flag set grows to 12 (FR-8.6) with the new `GRAPH_EDGE_LIST` member (capability VALUE `graph.edge_list`, following the layer-prefixed value convention of the other flags), and Appendix C gains the corresponding degrade row. This is a numbered amendment, not a rewrite — all previously frozen language stands unchanged.
> v1.2 amendment (2026-08-14): B.3 adds MetaStore `archive_profile(profile_id)` (soft-archive; `profiles.archived` column, migration v7) backing console Profiles FR-7.3. All previously frozen language stands unchanged.

## 1. Goals

1. core repo from zero to a developable state: package structure, CI, test framework, compose skeleton;
2. finalize the four storage-port interfaces (method-level list: see Appendix B), with each interface implementing an **embedded default + a second driver**, proving interface portability via contract tests;
3. **Schema v1 freeze** (list: see Appendix A): all table/field definitions land as code migration files; thereafter, changes may only go through the migration mechanism — manual database edits are not allowed.

## 2. Scope

- **In**: core repo skeleton and CI; the four VectorStore / GraphStore / MetaStore / Embedder interfaces + driver registry (supporting named multi-instance per layer); the four embedded default-stack drivers (LanceDB / SQLite-Graph / SQLite-Meta / bge-m3 ONNX); the three Postgres-family drivers (pgvector / pg-graph / pg-meta) + an OpenAI-compatible Embedder; capability-flag validation and the degradation behavior table; schema migration mechanism; docker-compose skeleton; embedded single-process **skeleton** (daemon start/stop + `/healthz`, with no business logic — the daemon business surface belongs to PRD-06 FR-6.2; the two are not built redundantly)
- **Out**: any five-stage pipeline logic (capture/dream/retrieve/reconcile/decay — PRD-01~04); the MCP gateway implementation (a Python module inside core, thin stdio shell, see PRD-03); host integration (PRD-06); console (PRD-07); driver performance tuning (performance acceptance belongs to the PRD-03 NFR; an M0 contract green light ≠ a performance green light)

## 3. Decisions Made

| # | Decision | Rationale |
|---|---|---|
| D1 | SQLite-Graph uses its **own adjacency tables** (two tables: nodes + edges), without pulling in an off-the-shelf graph library | Query patterns are fixed (1-2 hop traversal / co-occurrence edges / version chains); zero dependencies, schema autonomy; switching databases later is only a matter of adding a driver + one-time export/import, and since the verbatim channel is untouched, even the worst case is rebuildable |
| D2 | The Postgres graph side uses **pure relational table emulation** (isomorphic to the SQLite version), not Apache AGE | Runs on any managed PG, so the cloud is not picky about vendors; a single interface does not maintain two sets of query logic |
| D3 | capability flags are a **minimal viable set** (12, see FR-8.6) | What is frozen is the validation mechanism, not the list; no premature name-locking of features that haven't been designed |
| D4 | **MCP server lives inside this package** (Python stdio thin adapter, `mnemoseed mcp`; no Node, no second repo) — the earlier dual-repo decision is superseded: once the stdio shell just forwards to the daemon over HTTP, a separate Node package buys nothing |
| D5 (new in v2) | **Profile isolation goes through the stamp fields**: chunks add `profile_id`, filtered via `vector.metadata_filter`; without introducing the config complexity of "multi-instance vector store per layer" | Aligns with the `profile_id` on nodes; the PRD-06 identity model carries profile_id explicitly on every call, a natural match |
| D6 (new in v2) | **The Tier-3 isolated graph = a second-named GraphStore instance** (a separate SQLite file in embedded mode, a separate schema under PG); the registry supports named multi-instance per layer (`graph.main` / `graph.isolated`) | What design/02 §5 promises is physical isolation; a partition-key downgrade would break the "no back-contamination" narrative; the interface stays unchanged, just one more name in the registry |
| D7 (new in v2) | **Contract tests use a deterministic synthetic embedder** (fixed-dimension hash pseudo-vectors); real bge-m3 inference runs separately as a smoke test with model caching | CI cannot pull a ~543MiB model on every PR (NFR-8.2 ≤5min); the portability proof validates interface behavior, not vector quality |

## 4. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-8.1 | core repo skeleton: uv-managed src-layout package, pytest, ruff, mypy, GitHub Actions CI (lint + typecheck + test, must pass on every PR; the bge-m3 model file goes through CI caching) | P0 |
| FR-8.2 | The four port interfaces are defined as Protocols, **with the method list governed by Appendix B**; the interfaces come with a driver registry, config.toml selects drivers per layer and **supports named multi-instance per layer** (e.g. `graph.main` / `graph.isolated`); presets (embedded/docker/custom) + per-layer overrides, with `STORAGE_MODE` kept as a preset shortcut; the preset enum is extensible (cloud reserved) and not hard-coded | P0 |
| FR-8.3 | The four embedded drivers: `lancedb_embedded` / `sqlite_graph` (self-built adjacency tables) / `sqlite_meta` / `bge_m3_onnx` (model file downloaded on first run, ~543MiB (int8-quantized ONNX, as measured), with visible progress; plus a `synthetic` test embedder) | P0 |
| FR-8.4 | Second drivers: `pgvector` / `pg_graph` (pure relational tables, same schema and same queries as sqlite_graph) / `pg_meta`; the Embedder second driver = `openai_compatible` (any compatible endpoint, **dense-only output**, declaring the absence of `embed.sparse_output`) | P0 |
| FR-8.5 | **Interface contract test suite**: driver-agnostic behavior tests covering every method in Appendix B (with a "method ↔ contract test" mapping table), run once against each of the embedded and pg driver sets; also includes SQLite/PG **migration reconciliation assertions** (same schema_version sequence, same field definitions, byte-for-byte comparison with zero difference on both sides) | P0 |
| FR-8.6 | capability flags minimal set (12): `vector.hybrid_search` / `vector.metadata_filter` / `vector.snapshot` / `graph.traverse_2hop` / `graph.version_chain` / `graph.cooccurrence_edges` / `graph.edge_list` (member `GRAPH_EDGE_LIST`) / `meta.transaction` / `meta.concurrent_readers` / `embed.local_inference` / `embed.batch` / `embed.sparse_output`. Driver combinations missing a capability: refuse to start or go through **explicit degradation**, with the degradation behavior table governed by Appendix C (code and config docs kept in sync) | P0 |
| FR-8.7 | **Schema v1 freeze** (list: see Appendix A): all structures land as migration files (one set for SQLite and one for PG, under the same schema_version sequence); `schema_version` table + a purely forward-only `up` migration mechanism; stamp `cues.host` / `cues.task` as well as `profile_id` / `session_id` / `turn_start` / `turn_end` fields are **nullable but never absent** | P0 |
| FR-8.8 | docker-compose skeleton (docker preset: four services `core + vector(pgvector) + pg + embed`, with ollama as an optional profile), each service with `/healthz`; embedded single-process `mnemoseed up` starts the daemon skeleton with one command (all drivers embedded, no external dependencies, no business logic) | P0 |

## 5. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-8.1 | embedded mode cold start (**with bge-m3 already cached**) on an ordinary dev machine ≤ 10s, defined as boot → `/healthz` green + first embed completed (the model is lazily loaded; boot itself does not include model loading; both segmented timings must be reported); the first model download is not counted and is marked separately with visible progress; `/healthz` responds < 100ms |
| NFR-8.2 | Full contract-test suite (dual drivers, synthetic embedder) runs in CI ≤ 5min; the bge-m3 smoke test is a separate job using the cache |
| NFR-8.3 | All public code/comments/interface naming in English; no dates, names, or decision records enter the code |

## 6. Acceptance Criteria

- AC-1: Clean clone → `uv sync` → `uv run pytest` fully green; CI actually runs on PRs and has intercepted one deliberately-introduced failure (demonstrating CI effectiveness);
- AC-2: `docker compose up` brings up the full stack with one command, all services' `/healthz` green (measured response < 100ms); `mnemoseed up` starts the daemon in embedded single-process mode with the health check green; cached bge-m3 cold-start timing ≤ 10s;
- AC-3: The contract test suite passes **fully once against each of the embedded and pg driver sets**; every method in Appendix B has at least one contract test (the mapping table is output with the test report);
- AC-4: At least 3 real subset combinations from the degradation behavior table (Appendix C) are exercised: missing `embed.sparse_output` (openai_compatible) → retrieval degrades to dense-only + startup warning; missing `vector.snapshot` → dream snapshot semantics degrade to turn_range logical isolation + warning; missing `meta.transaction` → **refuse to start**. No silent paths anywhere;
- AC-5: Migration mechanism demo: the same migration (schema 1→2, adding one harmless column) executes **on both SQLite and PG** with identical version sequences, zero loss of existing data (incl. provenance.history and the version chain), and byte-for-byte row comparison with zero difference on both sides;
- AC-6: Check every item on the Appendix A freeze list — each field exists in the migration files with matching types (incl. the structured representation of `vector_sparse`), the reserved stamp fields are present (nullable), and each of the two named graph instances can create its own database.

## 7. Task Breakdown (each task = one programmer dispatch + verifier acceptance)

| # | Task | Estimate |
|---|---|---|
| 1 | repo skeleton + CI (FR-8.1) | 1.5d |
| 2 | four interface Protocols (per Appendix B) + registry (incl. named multi-instance) + preset parsing + capability validation (FR-8.2/8.6 interface side) | 2d |
| 3 | SQLite-Graph (two instances: main + isolated) + SQLite-Meta + schema v1 migration mechanism and reconciliation assertions (FR-8.3 half / FR-8.7) | 3d |
| 4 | LanceDB + bge-m3 ONNX + synthetic embedder (FR-8.3 half) | 2d |
| 5 | Postgres-family three drivers + openai_compatible embedder (FR-8.4) | 2.5d |
| 6 | docker-compose skeleton + health checks + embedded single-process skeleton (FR-8.8) | 1.5d |
| 7 | contract test suite (incl. migration reconciliation) + all AC demos (FR-8.5 / §6) | 3d |

Total 15.5d (+0.5d integration buffer ≈ **16d**, so roadmap m0b adjusted to 9d accordingly).

## 8. Dependencies

- No upstream dependencies (the first PRD to start work);
- **Blocks all subsequent PRDs** (01/02/03/04/06/07 all stand on the interfaces and schema).

## 9. Identified Residual Risks (not solved in M0, registered for later)

- **SQLite power-loss durability**: concurrent correctness of profile_score_pool/watermark is covered by contract tests, but embedded single-process power-loss crash recovery has no AC — add crash-recovery tests before the M1 capture chain (PRD-01) ships;
- **bge-m3 ONNX distribution as measured**: ~543MiB (measured 569.7MB, int8-quantized) + the real fit of the ONNX runtime dependency chain with TTFM < 3min needs verification via the PRD-06 install flow;
- **LanceDB ~100k-scale p95**: M0 contract tests do not expose real-scale performance; PRD-03 NFR-3.1 (300ms) is accepted independently at that time.

---

## Appendix A · Schema v1 Freeze List

> Freeze = from now on, changes may only be written as new migrations; manual edits are forbidden. Field-level details are governed by the migration files; this list is the review checklist.
> Sources: design/03 §3 erDiagram + design/01 §1 stamp + PRD-01 FR-1.6 + v2 blind-review completions.

### A.1 Hippocampus (VectorStore / LanceDB table `chunks`)

| Field | Type | Description |
|---|---|---|
| chunk_id | uuid PK | |
| text | string | verbatim original text, never lossily processed |
| vector_dense | float[] | bge-m3 dense output |
| vector_sparse | **struct {indices: int[], values: float[]}** | bge-m3 sparse output (~250k dimensions with only few nonzero); storing a dense array is forbidden |
| profile_id | string | profile namespace (D5); mandatory filter at retrieval |
| session_id / turn_start / turn_end | string / int / int | structured turn boundaries (snapshot bounding, safe clear, and score-pool events all depend on them; nullable but never absent); provenance.source_ref is only a human-readable string |
| cognitive_tier | int | 1 / 3 |
| model_id / anima_id | string | model and soul-in-office at write time (anima_id is an advanced-module field, nullable but never absent) |
| cues | struct | `project / host* / task* / tools_used[] / time_bucket / emotion_valence / entities[]` (* = reserved for encoded context, nullable but never absent) |
| provenance | struct | `asserted_by / source / source_ref / confidence / asserted_at / history[]` (append-only) |
| score | struct | `emotion / novelty / causal / total` |
| decay_weight | float | default 1.0 |
| last_reinforced / ingested_at | datetime | Freshness Guard relies on ingested_at filtering |
| consolidated | bool | set after dream clears a snapshot, accelerating decay |
| peripheral_gaps | bool | high-arousal peripheral information gap (design/01 §1.6) |
| needs_reconcile | bool | set in the near-duplicate 0.85–0.9 band (PRD-01 FR-1.8) |
| hit_count / last_hit_at / reinforce_count | int / datetime / int | usage counters (console Detail "usage" section; not derived from audit_log, to prevent event-stream inflation) |

### A.2 Cortex (GraphStore; SQLite and PG isomorphic; the `graph.main` and `graph.isolated` instances share one schema)

- **nodes**: `id PK / type / profile_id / payload JSON / decay_weight / conflict_flag / conflict_group / needs_reconcile / pending_consolidation / peripheral_gaps / valid_from / valid_to / last_reinforced / hit_count / last_hit_at / reinforce_count / provenance JSON / created_at`
  - `conflict_group`: conflicting parties share the same group ID — paired returns (FR-3.6) and the Conflicts inbox locate each other through it; a single bool is not enough;
  - the three flow flags (needs_reconcile / pending_consolidation / peripheral_gaps) are payload fields jointly depended on by PRD-01/02/03 and the console Detail page and must be inside the freeze.
- **edges**: `id PK / src / dst / rel / weight / provenance JSON` (co-occurrence edge = `rel='co_occurred'` + weight counter; cooccurrence and relation edges share the same table)
- **node_versions**: append-only version chain (`node_id / version / payload snapshot / changed_at / superseded_by`) — the physical basis for as_of bitemporal queries
- **Node-type enum (v1 frozen)**: `USER / HABIT / PREFERENCE / ANIMA / INTENTION / CONSTRAINT / EPISODE / SKILL_SEQUENCE / DECISION / PROJECT / TOOL`
- **Promotion-status field (v5)**: graph nodes gain `promotion_status` (`pending / promoted / quarantined / scrapped`, default promoted for back-compat) — the carrier of the promotion quality gate (design/02 §11); cheap to add now, expensive later. Read-side rerank gains a ζ·confidence term (design/03 §2)
- Per-type payload fields follow the design/03 §3 erDiagram; PREFERENCE includes `valence / prior_width / trait_anchor / evidence_chain`; ANIMA includes `core_traits / dye_layer / idiographic_notes / drift_history`

### A.3 MetaStore (SQLite and PG isomorphic)

- **schema_version** (the migration mechanism itself), **profiles**, **tokens** (credential issuance/revocation), **profile_score_pool** (per-profile score pool: `profile_id` primary key with no foreign key, `balance / watermark_start / watermark_end / last_event_start / last_event_end`, atomic transactional updates, events carrying turn_range; introduced by migration v3 and **replacing** the legacy single-row `score_pool`, which remains with no data migration), **config** (versioned + rollback-able), **audit_log** (append-only read/write event stream; retention and aggregation policy: 90-day rolling detail + permanent aggregate counters — **usage counters are not derived from audit_log**, they use the A.1/A.2 counter fields), **dream_runs** (dream run history: turn_range/model/tokens/cost/split counts/interruption marks; depended on by the console Dream panel and idempotent recovery), **dream_token_ledger** (monthly dream token ledger: composite UNIQUE(profile_id, year_month), atomic upsert accumulation; introduced by migration v4 with no data backfill — PRD-02 FR-2.5b)

### A.4 Explicitly Not Frozen

- Each driver's internal index structures (LanceDB index parameters, PG indexes) — implementation details may evolve;
- the capability flags list — may be extended later; the validation mechanism is the object of the freeze;
- the preset enum — cloud preset reserved as an extension point.

---

## Appendix B · Interface Method List (contract-test mapping basis)

> Every method has at least one contract test (AC-3). Signature details are governed by code; what is frozen here is the **method surface and semantic requirements**.

### B.1 VectorStore

| Method | Semantics | Consumer |
|---|---|---|
| upsert_chunk / get_chunk / delete_chunk | write / get single shard by id / forget_this deletion | capture, console Detail, PRD-03 |
| search(dense, sparse?, filter, top_k) | hybrid retrieval + metadata filtering (profile_id / decay_weight lower bound / time range) | retrieval |
| near_duplicate(vector, threshold, profile_id) | near-duplicate detection, supporting the 0.9 / 0.85 twin thresholds; profile_id required (D5 isolation) | capture FR-1.8 Hebbian reinforcement |
| snapshot_read(filter) | dream read-only snapshot; when snapshot capability is absent, degrades to turn_range logical read | dream engine |
| mark_consolidated(chunk_ids) | batch-set consolidated; the dream clear marks (never deletes) its consumed chunks, which stay as the evidence scene and decay at λ × 3 (design/03 §4) | dream clear |
| purge_range(session_id, turn_start, turn_end) | storage-level range clear; the two ends do not interfere | contract / legacy callers (the dream path clears via mark_consolidated) |
| update_weights(updates[]) | batch-write decay_weight / last_reinforced / reinforce_count | decay and reinforcement bounce-back |
| update_chunk_state(chunk_ids, hit_increment?, needs_reconcile?) | batch-write usage counters (hit_count / last_hit_at) and needs_reconcile set/clear; when hit_increment>0, also refresh last_hit_at | retrieval-hit counting, capture FR-1.8 suspected-contradiction marking |
| list_chunks(filter, page) | filtered + paginated listing | console Browser |
| capabilities() | self-reported capability set | startup validation |

### B.2 GraphStore

| Method | Semantics | Consumer |
|---|---|---|
| upsert_node / get_node / list_nodes(filter, page) | node write/read / console filtered pagination | dream, console |
| add_edge / bump_cooccurrence(a, b) | relation edges / co-occurrence edge +1 | dream, retrieval reinforcement |
| traverse(node_id, depth ≤ 2, filter) | entity subgraph traversal | retrieval |
| find_same_predicate(subject, predicate) | detection of existing same-subject/same-predicate facts | Reconcile write side |
| set_flags / clear_flags(nodes, flags) | set and clear needs_reconcile / pending_consolidation / conflict_group | capture, retrieval, reconcile |
| invalidate(node_id, valid_to) + append_version | invalidate the old version + append to the version chain (atomic) | Reconcile reconsolidation |
| versions(node_id) / diff(v1, v2) / timeline(node_id) | version-chain query / diff of any two versions / timeline playback | console Detail |
| as_of(timestamp, filter) | point-in-time replay query (bitemporal) | retrieval FR-3.9 |
| batch_update_weights(updates[]) | batch decay recompute (~100k < 60s, NFR-4.1) | Decay |
| query_intentions(status, due_before) | due pending INTENTION query | scheduler FR-3.15 |
| list_edges(filter, page) | **bulk edge listing** (v1.1 amendment, 2026-08-13; shipped canonical form) backing the console Graph View — filter = `EdgeFilter` (profile_id / node_types / tier / created_after / created_before / min_weight): `node_types` and `tier` restrict the edge's endpoints and require **both endpoints to be current-revision nodes** of the matching type / tier, while the time window and `min_weight` apply to the edge row itself; each `EdgeEntry` returns edge_id / src / dst / kind (`relation` / `cooccurrence`) / weight / created_at; paginated with the stable order `created_at DESC, id ASC` | console Graph View |
| capabilities() | self-reported capability set | startup validation |

Note: graph centrality (the δ term in the rerank formula) is computed client-side by the retrieval side from traverse results; M0 does not introduce a standalone centrality query.

Note (v1.1 amendment, 2026-08-13; shipped): `list_edges` is required in **both** `sqlite_graph` and `pg_graph`, each with contract tests (AC-3), and needs **no migration** — cooccurrence lives in the same `edges` table (`rel = 'co_occurred'`). The new capability flag (member `GRAPH_EDGE_LIST`, VALUE `graph.edge_list`) follows the Appendix C degrade semantics; the startup gate matches capabilities by layer prefix (this one belongs to the `graph` layer), so a graph driver lacking it degrades the console graph to per-node edge fetching via `traverse()` (bulk edge view unavailable) with an explicit startup warning.

### B.3 MetaStore

| Method | Semantics | Consumer |
|---|---|---|
| pool_add(profile_id, points, turn_range) | atomic per-profile score accumulation (records last_event) | capture FR-1.5 |
| pool_credit(profile_id, balance, turn_range) | set a single profile row wholesale to balance + watermark (upsert; absolute overwrite, not accumulation) | capture FR-1.5 |
| pool_state(profile_id) / pool_states() | single profile / all balances and watermarks read | capture FR-1.5, daemon startup recovery |
| advance_watermark(profile_id, turn_range) | monotonically advance a single profile's watermark | dream |
| profiles CRUD / tokens issue / revoke | identity and credentials | PRD-06 |
| archive_profile(profile_id) | **profile soft-archive** (v1.2 amendment, 2026-08-14): sets `profiles.archived` (migration v7); archived profiles stay queryable but are excluded from default active lists; distinct from delete — tokens/data survive until an explicit purge | console Profiles (FR-7.3) |
| config get / set (versioned + rollback) | versioned configuration | console Settings |
| audit_append / audit_query(filter, page) | append-only audit write / filtered paginated read | global |
| dream_runs record / list | dream run history | dream, console |
| add_token_usage(profile_id, year_month, tokens) / token_usage(profile_id, year_month) | monthly dream token ledger: atomic accumulation (upsert) / single-month read; year_month is a UTC year-month key, auto-zeroing across months (a new key is a new row) | dream FR-2.5b |
| schema_version get / migrate(up) | migration mechanism | install and upgrade |
| capabilities() | self-reported capability set | startup validation |

### B.4 Embedder

| Method | Semantics |
|---|---|
| embed(text) → {dense, sparse?} | single-item vectorization; sparse omitted when the sparse capability is absent |
| embed_batch(texts[]) | batch |
| capabilities() | `local_inference` / `batch` / `sparse_output` |

---

## Appendix C · Degradation Behavior Table (startup-validation criteria)

| Missing capability | Behavior | Level |
|---|---|---|
| `meta.transaction` | **refuse to start** (score-pool/watermark atomicity is a hard requirement) | hard |
| `graph.version_chain` | **refuse to start** (Reconcile/as_of depend on it) | hard |
| `vector.metadata_filter` | **refuse to start** (profile isolation and Freshness Guard depend on it) | hard |
| `embed.sparse_output` | hybrid retrieval degrades to dense-only, retrieval-quality warning | degrade + startup warning |
| `vector.hybrid_search` | same as above (dense-only) | degrade + startup warning |
| `vector.snapshot` | dream snapshot degrades to turn_range logical isolation, isolation-strength downgrade warning | degrade + startup warning |
| `graph.cooccurrence_edges` | rerank drops the ε co-occurrence term, retrieval-quality warning | degrade + startup warning |
| `graph.edge_list` (`GRAPH_EDGE_LIST`) | console Graph View bulk edge list degrades to per-node edge fetching via `traverse()`, console-graph performance warning | degrade + startup warning |
| `meta.concurrent_readers` | console reads serialize, concurrency-performance warning | degrade + startup warning |
| `embed.batch` | vectorization runs item by item, throughput warning | degrade + startup warning |

Rule: hard absence = refuse to start and print the list of missing capabilities; degradation = start is allowed but an explicit warning must be printed and written to the startup log. No path may be silent.
