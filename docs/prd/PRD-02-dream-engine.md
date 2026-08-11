# PRD-02 · Dream Engine (Consolidate: Snapshot Isolation + Dual-Track Split + De-biasing)

> Design doc: [02-dream-engine](../design/02-dream-engine.md)
> Milestone: M1 (local track) · Estimate 15.5 days (v2 adds the LLM port and model configuration)

## 1. Goals

Implement asynchronous "dreaming" consolidation: score-pool trigger → read-only snapshot → reflective distillation → dual-track split write-back → safe clear. Zero user-perceived latency and zero lost text throughout; dream models by default use OAuth / bring-your-own API key (zero hardware barrier), with the fully-offline track (Ollama) as an advanced option.

## 2. Scope

- **In**: trigger state machine, snapshot management, reflective orchestration (incl. the De-biasing prompt), Tier split write-back, incremental Delta stubbing, failure downgrade, **LLM port and model-routing configuration** (FR-2.14)
- **Out**: cloud TEE deployment (PRD-05), dynamic model-routing gateway (PRD-05)

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-2.1 | Consume score-pool trigger events, create a read-only snapshot (bounded by turn_range), while the hot layer continues to accept appends | P0 |
| FR-2.2 | Reflective orchestrator: full snapshot → dedup folding → De-biasing → triple extraction → provenance determination | P0 |
| FR-2.3 | Dual-track split: Tier 1 purified content is written directly to the main base; Tier 3 is locked into the isolated graph; salvage channel (Tier 1 second-pass reflection) | P0 |
| FR-2.4 | Interruption protection: new conversation appends to the tail with 0 latency during dreaming/write-back; clearing only covers the snapshot range | P0 |
| FR-2.5 | Delta stubbing: only the increment is sent on cloud calls; the system instruction and the existing graph go through Prompt Cache; increments are packed by a **dynamic budget**: `budget = clamp(total token count of shards pending settlement, 5k, 32k)`, measured deterministically on each local run before dreaming, with no feedback loop and no persistent state (budget definition: design/02 §6) | P0 |
| FR-2.5b | **Monthly token ledger**: accumulates the current month's dream token consumption per profile and converts to US dollars, default quota $5/month (adjustable via config/console); once exceeded for the month, downgrade to "capture-only, no consolidation" mode, auto-restoring on day 1 of the next month; ledger and budget values are exposed via memory.status/console | P0 |
| FR-2.5c | **Guaranteed drain**: when backlog persistently exceeds 32k, do not enlarge the single-run budget; drain through multiple consecutive dreams at a fixed cadence (each ≤32k); backlog trend goes to the console | P1 |
| FR-2.6 | Downgrade matrix: model call failures back off and retry ×3 → snapshot persisted to disk; when the configured model endpoint is unavailable (OAuth expired / API out of credit / Ollama offline), enter "capture-only" mode | P1 |
| FR-2.7 | Offline track (advanced, optional): Ollama + ≤14B quantized model runs the whole flow offline; first-time setup shows an explicit "distillation quality is lower than cloud large models" warning; 70B-class local models are not assumed by default | P1 |
| FR-2.8 | `mnemoseed dream --once` manual consolidation CLI: during M1, trigger manually and review distillation quality by hand first; only enable the automatic trigger after it meets the bar (manual-before-automatic discipline) | P0 |
| FR-2.9 | Emotional desensitization: after EPISODE consolidation write-back, the emotion intensity of its shards decays at an accelerated λ (the gist persists, the charge fades; overnight therapy, design/02 §10) | P1 |
| FR-2.10 | Schema-accelerated assimilation: distilled content isomorphic with the existing graph (entities exist + relation patterns match) goes through the fast-solidification channel; outliers need more independent evidence to be admitted (Tse 2007, also serving as a noise gate) | P1 |
| FR-2.11 | anima re-dye batch processing: triggered by an anima switch; the new core asynchronously re-digests the profile's existing memories and grows new dye layer/preferences; the old instance's dye layers are fully preserved (lossless switch, design/09 §4) | Advanced module (not in M1) |
| FR-2.12 | Dye-layer / preference evidence boundary: updates consume only the user's raw input and never adopt agent-rendered output (prevents self-locking slow drift, design/02 §5) | P0 |
| FR-2.13 | De-biasing eval harness: the dye strip rate metric for dyed samples enters CI; a regression in strip rate fails the build (defense against the single-point failure surface, design/02 §5) | P1 |
| FR-2.14 | **LLM port and model-routing configuration**: define a `DreamLLM` Protocol (chat completion + usage accounting + connectivity self-check), with a driver registry isomorphic to the storage layer — drivers: `oauth` (reuses a subscription: Codex/ChatGPT; Chinese CLI providers such as MiniMax/Kimi are optional, with an explicit data-residency-exit notice shown when selected) / `openai_compatible` (bring-your-own-key endpoints such as Fireworks) / `anthropic` / `ollama` (advanced offline track, **non-default**); default recommended order OAuth > API key > offline; config.toml configures per **role**: `deep_reflection` (long-context deep-sleep reflection) / `short_increment` (short increments, dynamic budget ≤32k, FR-2.5) / `local_track` switch; default routing per design/02 (deep sleep → Kimi K3 (Fireworks), short increments → DeepSeek V4 Flash 0731 (Fireworks), local track → Ollama + ≤14B quantized model (e.g. Llama 3.1 8B, consistent with FR-2.7)); **keys are separated per role** — each role has its own default environment variable (`MNEMOSEED_DEEP_REFLECTION_API_KEY` / `MNEMOSEED_SHORT_INCREMENT_API_KEY`), falling back to the shared `FIREWORKS_API_KEY` when unset, allowing the two roles to attach different providers; each role can independently switch driver and model name, and changes are written to audit; the connectivity self-check interface is called by the console test button (design/07 §8) | P0 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-2.1 | Interruption-response latency = 0 (guaranteed by architecture, not a tuning target) |
| NFR-2.2 | Dynamic Delta budget: `clamp(pending-settlement token count, 5k, 32k)` — a typical session increment is ≈ 2–2.5k (runs near the lower bound); cloud billing ≤ $0.001 per run is the **typical-value** basis; single-run hard cap 32k ≈ $0.0045 (DeepSeek V4 Flash 0731 $0.14/M input); **monthly token ledger default $5/month** caps total cost, with over-quota downgrade to capture-only (FR-2.5b). Budget values and backlog size are observable end-to-end (memory.status/console) |
| NFR-2.3 | Snapshot→write-back is idempotent end-to-end: after a process crash and restart, recovery proceeds from the snapshot persisted to disk without duplicate writes |
| NFR-2.4 | Offline track (≤14B quantized model, ordinary dev machine): single consolidation < 10 minutes |

## 5. Acceptance Criteria

- AC-1: Insert a new conversation when dreaming is 50% through; the new messages are fully preserved with no user-side lag;
- AC-2: Mix Tier 1 / Tier 3 conversation for 20 turns in the same scenario; after dreaming, verify the main base contains no nodes sourced from Tier 3 (full audit by provenance);
- AC-3: A single preference mentioned 10 times results in only 1 high-confidence entry in the graph (dedup folding effective);
- AC-4: After dreaming on a conversation where the anima scripted strong verbal tics, no tic words are retrievable from the base (De-biasing effective; eval-harness strip rate meets the bar).

## 6. Task Breakdown

1. `core/dream/trigger` — state machine (2d)
2. `core/dream/snapshot` — snapshot and idempotent recovery (2d)
3. `core/dream/reflect` — reflective orchestration + De-biasing prompt template (4d)
4. `core/dream/splitter` — Tier split and salvage queue (2d)
5. `core/dream/delta` — incremental stubbing + Prompt Cache adapter layer (2d)
6. `core/llm` — DreamLLM port + three drivers + role-routing configuration (FR-2.14) (1.5d)
7. Integration testing (interruption injection, contamination audit) (2d)

## 7. Dependencies

- PRD-01 (score-pool events, stamp schema)
- Graph write layer (M0 schema)
