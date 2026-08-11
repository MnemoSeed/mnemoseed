# PRD-03 · Hybrid Retrieval & MCP Gateway (Retrieve + Context Assembly)

> Design doc: [03-storage-and-retrieval](../design/03-storage-and-retrieval.md)
> Milestone: M1 · Estimate 10 days

## 1. Goals

Expose a standard MCP tool set outward, and implement concurrent dual-path hybrid retrieval + anti-dilution assembly inward. Any MCP Host (Cursor / Cline / Windsurf) connects with zero changes.

## 2. Scope

- **In**: MCP tool definitions, cue extractor, dual-path concurrent retrieval, fused rerank, token budget gate, context assembly
- **Out**: decay-weight calculation itself (PRD-04), cloud multi-Profile isolation (PRD-05)

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-3.1 | MCP tool set: `memory.recall` / `memory.remember` (explicit pin) / `memory.audit` (provenance query) / `memory.timeline` (timeline) / `memory.export` (full export in a readable format) / `memory.forget_this` (explicit user deletion right, GDPR right-to-be-forgotten compliance) | P0 |
| FR-3.1b | SessionStart warm-up injection: on a new session, proactively push a "recent-memory summary" opening context rather than passively waiting for recall (borrowed from the Claude-Mem lifecycle hook) | P1 |
| FR-3.2 | Cue extraction: parse entities/projects/tools/intentions from the current conversation's preceding context and generate retrieval cues | P0 |
| FR-3.3 | Concurrent dual path: VectorStore semantic neighbors (with cue filtering, LanceDB by default in embedded) + graph entity subgraph 2-hop traversal | P0 |
| FR-3.4 | Fused rerank formula `α·semantic + β·cue_overlap + γ·decay_weight + δ·graph_centrality`, weights configurable | P0 |
| FR-3.5 | Anti-dilution hard gate: top-k ≤ 5, token budget ≤ 800 (default, adjustable); over-budget, drop the tail and return dropped_count | P0 |
| FR-3.6 | conflict_flag memories returned in pairs + explicitly annotated | P0 |
| FR-3.7 | Retrieval hits automatically report "usage events" for Decay reinforcement bounce-back (consumed by PRD-04) | P1 |
| FR-3.8 | Freshness Guard (pending-consolidation marker): after assembly, check hippocampal shards with `ingested_at > watermark` that overlap entities; on a hit, mark the related triples `pending_consolidation`, rerank ×0.8, and attach ≤2 truncated recent_evidence entries; mechanism and parameters: see design/02 §9 / design/03 §2 | P0 |
| FR-3.9 | `memory.recall` supports the `as_of` point-in-time parameter: replay the facts in effect at that time via the provenance version chain (point-in-time query) | P1 |
| FR-3.10 | `memory.diff`: memory-diff view between two versions/points in time (natively supported by the version chain; demo material) | P2 |
| FR-3.11 | The MCP initialize response carries `instructions` behavioral guidance (MCP-only downgrade mode: nudge the model to recall at session start and call remember for important facts; pairs with the FR-1.8 idempotent-dedup backstop, see design/06 §2; the copy is self-contained at ≤512 characters) | P1 |
| FR-3.12 | Diversity and exploration quota: rerank-side duplicate suppression of same-type items (MMR-style) + every N retrievals, admit one low-weight, high-uncertainty memory (breaks the recall-reinforcement positive feedback loop, preventing memory monoculture, design/01 §3 Principle 5) | P1 |
| FR-3.13 | Honest empty: when no qualified candidate exists, return an explicit "no relevant memory" structure with dropped_count and a self-reported coverage figure; never pad with low-quality filler (metamemory, design/01 §3 Principle 6) | P0 |
| FR-3.14 | Contextual weak cues: the current host/project/time-band participate in reranking as weak-weighted cues (encoding-specificity retrieval side, depends on the PRD-01 FR-1.6 stamp context fields) | P1 |
| FR-3.15 | `memory.intend`: create an INTENTION node (trigger_condition + action), evaluated for triggering by the daemon scheduler (prospective memory, design/03 §3) | P2 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-3.1 | recall p95 latency < 300ms (local mode, ~100k-memory scale) |
| NFR-3.2 | Recall precision on mixed Chinese/English with code-interleaved queries is no lower than on pure English (bge-m3 baseline) |
| NFR-3.3 | Switching STORAGE_MODE only changes .env, zero code changes |

## 5. Acceptance Criteria

- AC-1: A preference accumulated in Cursor is correctly recalled in a new Cline session (cross-client inheritance);
- AC-2: Bury 1 key constraint + 30 weakly-related noise items; recall returns ≤5 items and the key constraint is among them;
- AC-3: When two contradictory preferences coexist, results appear in pairs with conflict annotation;
- AC-4: Every retrieval result is auditable for provenance;
- AC-5: Write a new fact contradictory to an existing preference during the consolidation gap (no dream run); on the next recall, the old preference carries a `pending_consolidation` marker with the new evidence quoted verbatim;
- AC-6: `as_of` with a past point in time returns the old-version facts in effect then, not the current values.

## 6. Task Breakdown

1. `core/mcp` — Python implementation of the six FR-3.1 MCP tools (official MCP SDK, thin stdio shell forwarding to daemon localhost HTTP; includes daemon-side retrieval/write HTTP endpoints) (2d)
2. `core/retrieve/cues` — cue extractor (2d)
3. `core/retrieve/hybrid` — dual-path concurrency + rerank (3d)
4. `core/retrieve/budget` — budget gate and assembler (2d)
5. Two-client e2e integration (1d)

## 7. Dependencies

- PRD-01 (stamp and cues schema)
- M0 (dual-store containers)
