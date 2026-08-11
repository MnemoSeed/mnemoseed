# PRD-01 · Capture Subsystem (Capture: Local Stripper + Importance Scoring + Score Pool)

> Design doc: [01-memory-pipeline Stage ①](../design/01-memory-pipeline.md)
> Milestone: M1 · Estimate 10 days

## 1. Goals

Implement a three-stage filtering funnel at the daemon capture end to achieve "capture is rejection": 90% of the volume is stripped locally, and only durable information that passes the gate enters the hippocampus, each item carrying a complete metadata stamp.

## 2. Scope

- **In**: daemon `/ingest` capture endpoint (Tier 1 host hooks push directly, zero tokens, bypassing MCP; host-side hook scripts belong to PRD-06), Local Stripper, persistence classifier, three-vector scorer, Watermark score pool, ingest support for the explicit `memory.remember` path
- **Out**: the dream engine itself (PRD-02), retrieval (PRD-03), host hook scripts and MCP server interceptors (PRD-06 / mcp repo), the SKILL_SEQUENCE raw-material queue (deferred to M2, designed together with the muscle-memory pipeline consumer)

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-1.1 | daemon `/ingest` endpoint receives every Turn of every conversation pushed directly by host hooks (the 2s timeout fail-open is guaranteed by the caller side), extracts user/assistant messages and the tool_call sequence, and performs Turn splitting and structuring | P0 |
| FR-1.2 | Local Stripper rule engine: strips compilation logs, package-manager output, infinite-loop error stack traces, ANSI highlight codes, and host-injected system artifacts (session-compaction summary shells, `<task-notification>` forwarding blocks — any structural scaffolding that is not user speech); the rule set is hot-reloadable | P0 |
| FR-1.3 | Persistence classification (three-month test) v1: rules (time qualifiers / emotion word lists) + lexicon- and embedding-based heuristics (durable/disposable); the durable marker family covers preferences / decisions / habit rules / stances / open questions and design-discussion sentences (decision confirmations, open questions, etc.); before scoring, content first passes the F1 strip, and fully-stripped turns are judged disposable; **no-overfit boundary**: immediate task-action sentences stay disposable — one-off instructions and troubleshooting questions such as "how do I fix this bug / next we start deploying" are not flipped to durable by the surface form of decision/open-question words; only clearly modal milestone passes ("next, we can start…") and design/product-type open questions (how to view/ensure, discussion of non-code objects) count as durable; **v1 does not introduce an edge small model** — first measure precision empirically on a benchmark annotation set, and only if NFR-1.3 is not met, reinforce with a small model (model selection and calibration then based on the annotation set) | P0 |
| FR-1.4 | Scoring `S = w₁·min(arousal,θ_cap) + w₂·novelty + w₃·causal_chain` (arousal capped; valence kept only as cues; emotion never enters confidence — rationale: design/01 §1.6), weights configurable. v1 quantification: arousal/valence use a hand-curated seed lexicon (NRC VAD shape, several hundred entries each in EN+ZH; the NRC VAD ontology itself is an application-form-gated resource that cannot be auto-distributed, replaced later by a calibrated resource); novelty = distance between the bge-m3 embedding and recent shards; causal chain = rule features (connectors / decision sentence patterns) | P0 |
| FR-1.5 | Score pool: accumulates S; emits a dream-trigger event when `pool ≥ 10.0 and idle ≥ 5s`; hard cap 50.0 forces micro-consolidation | P0 |
| FR-1.6 | Write stamp: cognitive_tier / model_id / anima_id (the soul in office at that time) / cues (**including the entities field**, relied upon by Freshness Guard retrieval-side filtering; **including the host / task encoded-context fields**, relied upon by encoding-specificity retrieval — nullable but never absent, reserved in the schema before the M0 freeze) / provenance / decay_weight=1.0 (schema: see design doc §1) | P0 |
| FR-1.6b | **Capture-neutrality red line**: the F1–F3 scoring and filtering stages are forbidden from reading anima state and PREFERENCE nodes throughout (anima may only dye retrieval and rendering; capture must be neutral — red line in design/01 §1; add a static check in CI to prevent regression) | P0 |
| FR-1.7 | ~~structured storage of the tool_call sequence into the `SKILL_SEQUENCE` raw-material queue~~ **deferred to M2** (the queue table is designed together with the muscle-memory pipeline consumer, to avoid creating the table first and then altering the schema) | P1→M2 |
| FR-1.8 | Near-duplicate check, two branches: ≥0.9 and consistent → Hebbian `last_reinforced` bounce-back without creating a new shard; ≥0.85 but conflicting in polarity/value/time (rules + lightweight classifier, zero additional LLM calls) → set the matched graph node to `needs_reconcile=true` and add +2.0 to the score pool (prediction error accelerates consolidation, design/02 §9.1) | P0 |
| FR-1.9 | `memory.remember` supports the caller explicitly passing `importance_hint` (0–1), taking the max with the automatic S score — when the user says "remember this", explicit intention overrides algorithmic judgment (intentional encoding, R28 Craik & Lockhart 1972) | P1 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1.1 | Full interception + scoring path < 50ms, invisible to the user |
| NFR-1.2 | **Strippable-noise strip rate ≥ 90%** (benchmark: real Claude Code session logs; both numerator and denominator count only rule-matched noise-class content — compilation logs / package-manager output / progress bars / ANSI / host-injected artifacts, etc.). The overall byte compression ratio is demoted to an observational metric (it depends heavily on the input content population: heavy-coding logs ≈90%+, research/design-topic conversations measured at only 0.4% in practice, which is not a defect); the regression script reports both |
| NFR-1.3 | Persistence classification achieves precision ≥ 0.9 on the annotation set (better to reject than to over-accept) |
| NFR-1.4 | The entire chain runs offline (local mode has zero external-network dependency) |

## 5. Acceptance Criteria

- AC-1: Feed in a chunk of raw Claude Code logs containing 1M tokens; the hippocampus persists ≤ 100k tokens, and manual spot checks confirm no loss of valid signals;
- AC-2: Sentences like "this bug is so annoying" do not enter storage; sentences like "I like concise reviews" enter storage with complete cues;
- AC-3: Continuous conversation accumulates and triggers a score-pool event whose payload contains the correct turn_range.

## 6. Task Breakdown

1. `daemon/ingest` — `/ingest` endpoint + Turn splitting and structuring (user/assistant/tool_call sequence) (2d)
2. `core/stripper` — rule engine + rule set v1 (2d)
3. `core/scorer` — lexicon/embedding scorer + score-pool state machine (3d)
4. `core/capture` — stamp assembly + writer (near-duplicate two branches, needs_reconcile set, Hebbian bounce-back) (2d)
5. Benchmark test set and compression-ratio regression script (based on real Claude Code session logs; persistence annotation set v1) (1d)

> Each task = one programmer dispatch + verifier acceptance, TDD: write failing tests per FR/AC before implementing (collaboration flow: see `.claude/agents/`).

## 7. Dependencies

- M0 complete (docker-compose skeleton, schema base)
- bge-m3 ONNX embeddings ready
