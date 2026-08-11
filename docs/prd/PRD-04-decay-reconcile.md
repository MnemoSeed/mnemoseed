# PRD-04 · Decay, Reconcile & Provenance (Decay + Reconcile + Provenance)

> Design doc: [01-memory-pipeline Stages ④⑤ⓟ](../design/01-memory-pipeline.md)
> Milestone: M2 · Estimate 14 days — **differentiation core, moat of memory quality**

## 1. Goals

Keep the memory base "still trustworthy after a year of use": unreinforced memories sink naturally, fact changes take over correctly, conflicts become explicit, every memory is auditable.

## 2. Scope

- **In**: decay engine (weight calculation / layered λ / soft archival), reinforcement bounce-back, the Reconcile twin protocols (write-side detection + extraction-side reconsolidation), historical version chain, audit interface
- **Out**: domain-aware auto-tuning of decay parameters (post-v4.x)

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-4.1 | Decay calculation: `w = base_confidence × exp(-λ_eff × days)`, `λ_eff = λ_base × (1 + κ × interference_load)` — the more similar neighbors, the faster the decay (interference theory, Wixted 2004; distinctive memories are naturally decay-resistant); λ_base is layered by memory type (fact 0.01 / preference 0.005 / episode 0.03) | P0 |
| FR-4.2 | Reinforcement bounce-back: retrieval usage events trigger `last_reinforced=now` and `w` bounce-back; **spacing-effect cooldown** — repeated recalls within a short window yield diminishing returns (Cepeda 2006), preventing concentrated weight farming | P0 |
| FR-4.3 | Soft-decay ladder: w<0.4 sinks (excluded from top-k) → w<0.1 freezes (not retrievable) → w<0.05 with 90 days of no access is archived (moved out of the index); explicit queries can resurrect it (w→0.5) | P0 |
| FR-4.4 | Never-decay whitelist: provenance, user pins, compliance/safety constraints | P0 |
| FR-4.5 | Write-side conflict detection: compare same-subject/same-predicate → identical entries reinforce / **if cues can delineate, coexist under situational scopes** / if adjudicable, invalidate takes over / if not adjudicable, flag_conflict (four branches; situational coexistence takes priority over adjudication) | P0 |
| FR-4.5b | Conflict-confirmation rendering interface: the engine only outputs structured conflict objects (old/new + provenance); the wording is dramatized by the anima in office (personality core + dye layer → tone of voice), and the engine must not carry its own phrasing; when no anima is present (M1), fall back to outputting the structured object directly (anima model: see design/09, advanced module) | P0 |
| FR-4.5c | **Preference-reconciliation branch**: PREFERENCE nodes do not go through the four-branch contradiction logic — old and new preferences coexist in the version chain by drift semantics ("the me then in effect"); update rule `Δvalence = learning_rate(∝prior_width) × evidence_strength × type_weight(behavior > statement > emotional cooccurrence > exposure)`; evidence comes only from the user's raw input (design/09 §3, 02 §5). Basic preference entries (ordinary PREFERENCE-node coexistence semantics) take effect in M1; the extended fields (valence/prior_width/evidence_chain) ship with the advanced module | P0 (extended fields are an advanced module) |
| FR-4.1b | Dynamic λ self-calibration: adjust each layer's λ by feedback from the resurrection rate of sunken memories (interface reserved; initial values hand-set) | P2 |
| FR-4.3b | Source-invalidation downweighting: memories whose provenance.source has become invalid are automatically downweighted further (MemPalace sync mode) | P1 |
| FR-4.2b | Hebbian reinforcement at capture: a near-duplicate hit bounces back immediately, without waiting for a dream run (coordinates with the PRD-01 pre-write dedup check) | P0 |
| FR-4.6 | Extraction-side reconsolidation: a retrieval hit opens a labile window; a new contradictory fact rewrites the old slot, and the old version enters the history chain (valid_to); never physically deleted | P0 |
| FR-4.7 | Adjudication criteria: timestamp clarity + source-authority difference (explicit user > Tier 1 inference > Tier 3 inference) | P0 |
| FR-4.8 | Audit interface: any memory returns its full provenance.history (creation/rewrite chain) | P0 |
| FR-4.9 | Conflict surfacing: when flag_conflict reaches a threshold or involves a high-severity constraint, proactively ask the user for a two-way confirmation | P1 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-4.1 | Decay batch runs once daily; full recompute over ~100k memories < 60s |
| NFR-4.2 | Historical version-chain query (timeline) p95 < 500ms |
| NFR-4.3 | Every rewrite operation is idempotent and traceable (replaying history can reconstruct the state at any point in time) |

## 5. Acceptance Criteria

- AC-1: Fast-forward simulated time by 60 days: unvisited memories decay their w correctly, while visited memories stay high;
- AC-2: The user first says "I use Neovim", then 30 days later "I switched back to VSCode" — recall returns only VSCode, and the timeline shows the full change chain;
- AC-3: When a low-confidence source tries to overwrite a high-confidence fact, it does not overwrite but generates flag_conflict;
- AC-4: Construct 50k memories and run a 90-day simulation; recall's top-5 hit rate drops < 10% relative to day 1 (resistance to the garbage-dump effect).

## 6. Task Breakdown

1. `core/decay/engine` — weight calculation + layered λ + batch processing (3d)
2. `core/decay/reinforce` — usage-event consumption and bounce-back (1d)
3. `core/reconcile/detector` — write-side conflict detection and adjudication (3d)
4. `core/reconcile/reconsolidate` — labile window and version chain (3d)
5. `core/audit` — provenance queries and timeline API (2d)
6. Time fast-forward simulation test framework (2d)

## 7. Dependencies

- PRD-02 (attach detection hooks on the write-back path)
- PRD-03 (retrieval-side usage events, paired conflict returns)
