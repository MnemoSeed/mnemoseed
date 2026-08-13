# PRD-07 · Management Console (MnemoSeed Console)

> Design doc: [07-console](../design/07-console.md)
> Version: v2.0 · 2026-08-13
> Milestone: console-COMPLETE (read + write, pages ①–⑩) is a **hard pre-marketing gate** — no marketing demo-video production until console-COMPLETE + CLI parity + onboard all pass. Sequencing: W1 ∥ W2 ∥ PRD-04; W3 after PRD-04 lands · v2.0 estimate 20d (W1 8d, W2 7d, W3 5d)

## 1. Goals

Provide a review surface for the "manual-before-automatic" discipline, making every memory's content, weight, provenance, versions, and usage records fully visible and manageable — transparency turning from slogan to interface. v2.0 completes the write side: every console action is live, audited, and scriptable through the CLI (capability parity).

## 2. Scope

- **In (M1 read core)**: FastAPI-hosted static SPA, localhost implicit authentication, Dashboard / Profiles / Memory Browser / Memory Detail / Dream panel / Conflicts inbox
- **In (console-COMPLETE)**: write operations (forget (tombstone) / pin (never_decay) / weight adjust / conflict resolve / dream --once / auto-trigger toggle / profile create-rename-archive / token issue-revoke), Audit Log, Settings, Graph View, ConfigWriteService-backed writes (FR-7.11), CLI parity (FR-7.12), onboard shared backend (FR-7.13)
- **Out**: Anima panel ⑪ (FR-7.10, advanced module), multi-seat/license Users features (design/06 §2.7 — activation-locked), cloud admin plane, cloud multi-tenant console (reuses the same frontend with a baseurl switch at the PRD-05 stage)

## 3. Functional Requirements (console-COMPLETE)

| ID | Requirement | Priority |
|---|---|---|
| FR-7.1 | daemon hosts the `/console` static SPA + `/api/v1/*` REST; `mnemoseed console` opens the browser | P0 |
| FR-7.2 | Dashboard: current state-machine state, score-pool level, watermark, pending-consolidation/needs_reconcile/pending counts, token usage grouped by model | P0 |
| FR-7.3 | Profiles: list/create/rename/archive; token issuance and revocation; bound-agent list; memory-scale statistics | P0 |
| FR-7.4 | Memory Browser: short-term (shards)/long-term (nodes) dual tabs, filterable by time/project/tool/entity/cue/Tier/decay range | P0 |
| FR-7.5 | Memory Detail profile page: verbatim↔triple comparison, full provenance timeline, version-chain diff, full weights (decay-curve projection, the three S components, confidence, reinforcement count), recall-hit statistics, all flag bits | P0 |
| FR-7.6 | Dream panel: pending-settlement queue, run history (turn_range/model/tokens/cost/split counts/interruption marks), **distillation-quality review interface** (raw shards ↔ distilled products compared item by item; accept/reject/mark-hallucination), dream --once trigger button, automatic-trigger toggle (off by default) | P0 |
| FR-7.7 | Conflicts inbox: conflicting pairs displayed together + four-branch handling (reinforce/coexist-with-scopes/invalidate/suspend); handling is written back to the version chain | P1 (end of M1) |
| FR-7.8 | Graph View: **hand-rolled three.js instanced layer** — one `THREE.Points` custom-shader draw for nodes, one `InstancedMesh` of quads for edges, canvas-sprite labels for the top-60 centrality nodes, `Raycaster` picking, precomputed clustered layout (approved 2026-08-12; benchmark evidence [docs/bench/graphview-three-results.md](../bench/graphview-three-results.md), runnable artifact `.bench/graphview-three/` (local, gitignored), 2026-08-13); node opacity = decay_weight (visualizing forgetting), color = type, size = centrality, edge thickness = weight; filters profile/type/time/Tier; click through to the profile page; holds ≥30 fps @5k nodes on min-spec hardware (NFR-7.2 v2) | P0 (console-COMPLETE) |
| FR-7.9 | All write operations are recorded in the Audit Log (actor ∈ console\|cli\|mcp); Audit Log page with filtered pagination | P0 (console-COMPLETE) |
| FR-7.10 | Anima panel (advanced module, not in the launch): trait radar chart (axis count follows the schema and is not locked to six axes; vertices = mean, error band = width making uncertainty visible, with manual fine-tuning allowed); plain-language creation (natural-language description → quantized model-generated template); solid core line + dashed dye-layer current expression overlaid; cross-profile link/re-link entry + re-link triggers a re-dye confirmation; drift_history timeline playback (design/09 §7) | Advanced / Out |
| FR-7.11 | Every console write and settings change is backed by the **daemon-owned ConfigWriteService** (single config writer): registry → validate → surgical toml patch → versioned meta-store record (the existing `set_config`/`rollback_config` ports) → audit with actor attribution (console\|cli\|mcp) → live-apply or restart-required flag; hand-edited config.toml is detected by mtime/hash, rebaselined at next boot, and recorded as a `config_rebaseline` audit entry | P0 |
| FR-7.12 | **CLI capability parity**: every console action is scriptable via the `mnemoseed` CLI (JSON/table output); interactive visuals are console-only with CLI data-equivalents (e.g. `graph export --json`); `mnemoseed config set\|get\|rollback` round-trips console↔CLI; new `mnemoseed audit` verb; the parity matrix is checked into docs; CLI config ops are a REST client (REST-only; `--force` offline escape prints "not audited (daemon down)", loopback baseurl only) | P0 |
| FR-7.13 | **Onboard shared backend**: the console setup wizard and the CLI `mnemoseed onboard` verb are two frontends over one backend service (details land in PRD-06); the console never implements its own onboarding flow | P0 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-7.1 | By default, listen on localhost only; non-localhost access requires explicitly enabling it + an admin token |
| NFR-7.2 (v2) | At ~100k-memory scale, browsing pages' first paint < 1s; the Graph View sustains **≥30 fps @5k nodes** with minimum hardware = **WebGL2 + discrete-GPU class** (iGPU unmeasured); benchmark evidence in design/07 §④ (2026-08-13) |
| NFR-7.3 | The console is a pure client — closing the page does not affect any daemon functionality |

## 5. Acceptance Criteria (gate AC set)

| ID | Acceptance Criterion |
|---|---|
| G-AC1 | Console write-complete: forget (tombstone) / pin (never_decay) / weight adjust / conflict resolve / dream --once / auto-trigger toggle / profile create-rename-archive / token issue-revoke — all live and audited in-session |
| G-AC2 | ⑧ configures all three dream roles (deep_reflection / short_increment / local_track); connectivity test passes before persist; the UI exposes env-var NAMES only (a literal key shown in the UI is a test failure); versioned + rollbackable |
| G-AC3 | ⑨ Settings (w₁/w₂/w₃, λ per type, top-k, token budget, pool threshold) validate / persist / audit / rollback; the live-vs-restart classification is documented per key; storage driver switch is disabled while data exists |
| G-AC4 | Graph View (three.js) holds ≥30 fps @5k nodes on min-spec hardware; opacity = decay_weight, color = type, size = centrality, edge thickness = weight; click → Memory Detail; filters profile/type/time/Tier |
| G-AC5 | CLI parity matrix checked in; every console action has a CLI counterpart with identical state transitions; audit actor attribution is correct (cli/console/mcp); `config set/get/rollback` round-trips console↔CLI |
| G-AC6 | Onboard on a clean machine: owner → preset → LLM wizard (connectivity-tested) → host link (backup + diff + confirm) → autostart → doctor green; the console wizard uses the identical backend; LLM-skip → capture-only daemon |
| G-AC7 | Audit integrity: a scripted sequence (forget + weight adjust + model switch + conflict resolve + auto-trigger flip) appears correctly attributed in the Audit Log |

## 6. Task Breakdown

### W1 · ConfigWriteService + console write ops + Audit + Settings + ⑧ (8d) — parallel with PRD-04

1. `core/configwrite` — daemon-owned single config writer: registry → validate → surgical toml patch → versioned meta-store record (`set_config`/`rollback_config` ports) → audit (actor ∈ console|cli|mcp) → live-apply/restart-required flag; hand-edit mtime/hash detection → next-boot rebaseline + `config_rebaseline` audit entry (2d)
2. `console/write` — forget (tombstone) / pin (never_decay) / weight adjust / conflict resolve / dream --once / auto-trigger toggle / profile create-rename-archive / token issue-revoke, all in-session audited (2d)
3. `console/audit` — Audit Log page + filtered pagination (0.5d)
4. `console/settings` + `console/models` — ⑨ Settings (w₁/w₂/w₃, λ per type, top-k, token budget, pool threshold; per-key live-vs-restart table; driver switch disabled with data) + ⑧ Models & Routing (three roles, env-var names only, connectivity test before persist, versioned + rollbackable) (1.5d)
5. `console/integration` — cross-surface audit-integrity sweep (G-AC7 script) + gate dry-run alongside the CLI suite (2d)

### W2 · CLI parity verbs (7d) — parallel with PRD-04

6. `cli/parity` — core verbs over the daemon REST client: `console` / `status` / `link` / `unlink` / `recall` / `remember` / `dream --once` / `export` / `diff` / `forget` / `audit`; JSON/table output (3d)
7. `cli/config` — `config set|get|rollback` over the ConfigWriteService REST (FR-7.11); REST-only, `--force` offline escape prints "not audited (daemon down)", loopback baseurl only (1.5d)
8. `cli/onboard` — `mnemoseed onboard` guided aggregate over the shared onboard backend (skippable + resumable; FR-6.10) (1.5d)
9. `cli/matrix` — parity matrix checked into docs + console↔CLI round-trip gate checks (G-AC5) (1d)

### W3 · Graph View + demo-seeding (5d) — after PRD-04 lands

10. GraphStore port extension: `list_edges(filter, page)` + `GRAPH_EDGE_LIST` capability (per PRD-08 Appendix B amendment v1.1) (1d)
11. `console/graph` — hand-rolled three.js instanced layer Graph View (THREE.Points nodes / InstancedMesh edges / canvas-sprite top-60 labels / picking / precomputed clustered layout) + filters (profile/type/time/Tier) + click → Detail (2d)
12. demo-seeding for the fading-graph showcase (decay-weight variance across types/Tiers for the marketing demo) + min-spec GPU re-benchmark (NFR-7.2 v2) with fixes surfaced by the port-extension e2e (2d)

Total ≈ 20d.

## 7. Dependencies

- PRD-01/02/03/04 all complete (the console is their observation surface); W1/W2 run in parallel with PRD-04, W3 after PRD-04 lands
- PRD-06 (login/token identity model; admin token is reused; onboard shared backend service)
- PRD-08 Appendix B amendment v1.1 ([PRD-08](../prd/PRD-08-m0-foundation.md) GraphStore `list_edges(filter, page)` + `GRAPH_EDGE_LIST` capability) — W3 prerequisite