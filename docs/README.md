# MnemoSeed Design Docs Index

> All diagrams are authored and rendered with Mermaid.js.

## Contents

### Design docs (design/)

| Doc | Contents |
|---|---|
| [00-overview-and-philosophy](design/00-overview-and-philosophy.md) | Positioning, neuroscience mapping table, top-level architecture |
| [01-memory-pipeline](design/01-memory-pipeline.md) | Capture / Consolidate / Retrieve / Reconcile / Decay + Provenance pipeline |
| [02-dream-engine](design/02-dream-engine.md) | Trigger state machine, snapshot isolation, interrupt protection, dual-track split writes, incremental dehydration |
| [03-storage-and-retrieval](design/03-storage-and-retrieval.md) | Hybrid dual-store bus, STORAGE_MODE routing matrix, hybrid retrieval, anti-dilution policy |
| [04-isolation-and-privacy](design/04-isolation-and-privacy.md) | Cognitive grading isolation, BYOK/TEE zero-knowledge architecture |
| [05-industry-landscape](design/05-industry-landscape.md) | Stanford/Microsoft/Anthropic/Nvidia lenses, Claude-Mem, MemPalace teardowns and adoption decisions |
| [06-host-integration](design/06-host-integration.md) | Three-layer adapter architecture (daemon/MCP/plugin), profile credential identity model (login/link), host capability matrix, 3-minute install flow, disconnect/uninstall semantics |
| [07-console](design/07-console.md) | MnemoSeed Console: profiles, memory browser, graph view, per-memory full dossier, dream panel and token usage, model routing settings |
| [08-sync-and-merge](design/08-sync-and-merge.md) | CRDT/CALM/HLC foundations: merge mechanics per data class, anti-entropy sync protocol, consolidation lease, tombstone deletes |
| [09-anima-and-preferences](design/09-anima-and-preferences.md) | **Advanced module (not in the M1 launch)**: anima soul model (three-layer unity / lossless switching), Bayesian preference updates, trait radar visualization |

### Development task PRDs (prd/)

| Doc | Module |
|---|---|
| [PRD-00 roadmap](prd/PRD-00-roadmap.md) | M0–M4 milestones, dependencies, priorities |
| [PRD-01 capture subsystem](prd/PRD-01-capture.md) | Local Stripper, importance scoring, watermark score pool |
| [PRD-02 dream engine](prd/PRD-02-dream-engine.md) | Async consolidation, snapshots, tier routing, de-biasing |
| [PRD-03 retrieval & MCP gateway](prd/PRD-03-retrieval-mcp.md) | Hybrid retrieval API, MCP tool definitions, context assembly |
| [PRD-04 decay, reconcile & provenance](prd/PRD-04-decay-reconcile.md) | Decay weights, Reconcile conflict protocol, Provenance schema |
| [PRD-05 cloud sync & TEE](prd/PRD-05-cloud-tee.md) | E2EE sync, Nitro Enclaves, billing arbitrage gateway |
| [PRD-06 host integration & install](prd/PRD-06-host-integration.md) | daemon embedded mode, installer, login/link identity binding, Claude Code plugin (hooks), MCP degraded mode, uninstall |
| [PRD-07 console](prd/PRD-07-console.md) | Console SPA: Dashboard/Profiles/memory browser/dream review/conflict inbox (M1 read-only core, M2 completes) |
| [PRD-08 M0 foundation](prd/PRD-08-m0-foundation.md) | Repo skeleton + CI, four storage interfaces with dual drivers, capability checks, Schema v1 freeze (blocks all later PRDs) |

## Theory & Literature Registry

**[REFERENCES.md](REFERENCES.md)** — full provenance and verification status for every cited theory (✅ Crossref-verified / 📕 classic monograph / ⚠️ pending spot-check). Iron rule: unverified information must be marked as such; nothing rests on guesswork or recall.

## External Theory Sources

1. **wast3, "Memory Engineering"** (X, 2026-08-04) — the five-stage memory pipeline framework (Capture/Consolidate/Retrieve/Reconcile/Decay) plus a Provenance addition from the comments. This project does not copy it; it re-derives the stages from complementary learning systems (CLS), reconsolidation, synaptic homeostasis (SHY) and other neuroscience mechanisms.
2. **N01ennn, "How to be a Memory Engineer"** (X, 2026-08-03) — the Stanford/Microsoft/Anthropic/Nvidia lenses, 15-step engineering discipline. Teardown and adoption decisions in design/05.
3. **Claude-Mem / MemPalace** — concept teardowns of two actually-running memory systems (design/05).
4. **MnemoSeed whitepaper v3.1 / PRD v3.0 / genesis whitepaper** (earlier discussion artifacts) — dual-store architecture, dream engine, cognitive grading, dehydration throttles, pricing and PLG strategy.
