# PRD-07 · Management Console (MnemoSeed Console)

> Design doc: [07-console](../design/07-console.md)
> Milestone: read-only core in M1 (Dashboard / Memory Browser / Dream panel / Conflicts); M2 completes write operations and Graph View · M1 portion estimate 6 days

## 1. Goals

Provide a review surface for the "manual-before-automatic" discipline, making every memory's content, weight, provenance, versions, and usage records fully visible and manageable — transparency turning from slogan to interface.

## 2. Scope

- **In (M1)**: FastAPI-hosted static SPA, localhost implicit authentication, Dashboard / Profiles / Memory Browser / Memory Detail / Dream panel / Conflicts inbox
- **In (M2)**: Graph View visualization, write operations (forget/pin/adjust weight/resolve conflict/switch model), Audit Log, Settings
- **Out**: cloud multi-tenant console (reuses the same frontend with a baseurl switch at the PRD-05 stage)

## 3. Functional Requirements (M1 portion)

| ID | Requirement | Priority |
|---|---|---|
| FR-7.1 | daemon hosts the `/console` static SPA + `/api/v1/*` REST; `mnemoseed console` opens the browser | P0 |
| FR-7.2 | Dashboard: current state-machine state, score-pool level, watermark, pending-consolidation/needs_reconcile/pending counts, token usage grouped by model | P0 |
| FR-7.3 | Profiles: list/create/archive; token issuance and revocation; bound-agent list; memory-scale statistics | P0 |
| FR-7.4 | Memory Browser: short-term (shards)/long-term (nodes) dual tabs, filterable by time/project/tool/entity/cue/Tier/decay range | P0 |
| FR-7.5 | Memory Detail profile page: verbatim↔triple comparison, full provenance timeline, version-chain diff, full weights (decay-curve projection, the three S components, confidence, reinforcement count), recall-hit statistics, all flag bits | P0 |
| FR-7.6 | Dream panel: pending-settlement queue, run history (turn_range/model/tokens/cost/split counts/interruption marks), **distillation-quality review interface** (raw shards ↔ distilled products compared item by item; accept/reject/mark-hallucination), dream --once trigger button, automatic-trigger toggle (off by default) | P0 |
| FR-7.7 | Conflicts inbox: conflicting pairs displayed together + four-branch handling (reinforce/coexist-with-scopes/invalidate/suspend); handling is written back to the version chain | P1 (end of M1) |
| FR-7.8 | Graph View: interactive Cytoscape.js graph; node opacity = decay_weight (visualizing forgetting); click through to the profile page | P1 (M2) |
| FR-7.9 | All write operations are recorded in the Audit Log; Audit Log page (M2) | P1 |
| FR-7.10 | Anima panel (advanced module, not in the M1 launch): trait radar chart (axis count follows the schema and is not locked to six axes; vertices = mean, error band = width making uncertainty visible, with manual fine-tuning allowed); plain-language creation (natural-language description → quantized model-generated template); solid core line + dashed dye-layer current expression overlaid; cross-profile link/re-link entry + re-link triggers a re-dye confirmation; drift_history timeline playback (design/09 §7) | Advanced module |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-7.1 | By default, listen on localhost only; non-localhost access requires explicitly enabling it + an admin token |
| NFR-7.2 | At ~100k-memory scale, browsing pages' first paint < 1s; the graph view sustains fluid interaction with 5k nodes |
| NFR-7.3 | The console is a pure client — closing the page does not affect any daemon functionality |

## 5. Acceptance Criteria

- AC-1: The full dream --once flow (review queue → trigger → item-by-item comparison review → accept/reject) is completed entirely in the UI, without touching CLI/JSON;
- AC-2: Pick any long-term memory and answer: "where it came from, who wrote it, how many versions, what each change was, its current weight, how many times it was recalled";
- AC-3: Construct a pair of contradictory memories; they appear in the Conflicts inbox, complete the "situational coexistence" handling, and leave a record in the version chain;
- AC-4: Kill the console static service; daemon capture/retrieval/dreaming all continue normally.

## 6. Task Breakdown

1. `console/api` — REST endpoints (status/memory query/dream control/conflict handling) (2d)
2. `console/web` — SPA skeleton + Dashboard + Memory Browser + Detail (2d)
3. Dream panel + review interface (1d)
4. Conflicts inbox + e2e (1d)

## 7. Dependencies

- PRD-01/02/03/04 all complete (the console is their observation surface)
- PRD-06 (login/token identity model; admin token is reused)
