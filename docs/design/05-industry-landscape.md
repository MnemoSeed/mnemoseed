# 05 · Industry Benchmarking & Essence Extraction

> Analysis subjects: N01ennn's "How to be a Memory Engineer" (X, 2026-08-03, aggregating the perspectives of four labs — Stanford / Microsoft / Anthropic / Nvidia), Claude-Mem, MemPalace, Mem0, Letta/MemGPT.
> Principle: **every borrowing must start from cognitive neuroscience / human psychology theory as the point of derivation**; engineering concepts are only evidence, not axioms.

---

## 1. N01ennn's Four-Lens Article: Full Breakdown & Trade-offs

The article's core thesis: *"Your agent's problem is not that it forgets, but that it never deliberately forgets."* A memory engineer optimizes not "what to remember" but "what to let go of".

### Lens 1: Stanford — The Cost of Remembering (Cost Perspective)

| Original finding | Our mapping | Adoption |
|---|---|---|
| The real cost is not on the query side but on the **write path** (write path/construction) | Exactly why our "two dehydration valves" exist | ✅ Covered (Stripper + Delta staking). **Add**: list "cost per correct answer" as a first-class metric in the PRD NFR |
| Two systems of equal accuracy differ **47×** in energy consumption; accuracy hides its own bill | Ammunition for both marketing and engineering | ✅ Written into internal promo material. The original paper was verified first-hand (arXiv:2606.06448, see REFERENCES I2a) |
| Four families of memory systems: raw context / flat retrieval / structured extraction / fully agentic — **no universal champion, only a deliberate choice of which cost to pay** | We are in the structured extraction family | ✅ Whitepaper positioning updated: not claiming "the best", claiming "a deliberate choice" |

### Lens 2: Microsoft — What Is Worth Keeping (Content Perspective)

| Original finding | Neuroscience basis | Adoption |
|---|---|---|
| PlugMem: stores **facts and skills**, not logs; more raw memory actually makes the agent worse | Episodic-to-semantic memory transformation (Tulving): the brain does not replay events; it keeps only the distilled facts and skills | ✅ Covered (triples + SKILL_SEQUENCE muscle memory) — original paper verified first-hand (arXiv:2603.03296, see REFERENCES I2b) |
| Utility metric: **how much decision-relevant information each token delivers** — density beats volume | Working-memory capacity ceiling (the modern revision of Miller's 7±2 ≈ 4 chunks) | ✅ Covered (top-k ≤ 5 anti-dilution) — theoretical anchor strengthened |
| Memento: the model writes its own dense note, then deletes the original reasoning; **forgetting ≠ deletion** — the erased reasoning leaves a "shadow" inside the model | Memory trace theory: forgetting is retrieval failure, not trace disappearance | ⚠️ **Original paper could not be located on arXiv** (REFERENCES I2c) — the "forgetting ≠ deletion" conclusion is independently supported by memory trace theory and kept; the specific numbers (2–3x, 15 min) are dropped |

### Lens 3: Anthropic — Who Controls What It Keeps (Control Perspective)

| Original finding | Adoption |
|---|---|
| Memory lives in **openable, editable, deletable files/storage**; storage you cannot open = storage you do not own | ⚠️ **Our gap**. Add: `memory.export` (full export to a readable format) + `memory.forget_this` (explicit user deletion right) to [PRD-03](../prd/PRD-03-retrieval-mcp.md)/[PRD-04](../prd/PRD-04-decay-reconcile.md) — this is also a compliance necessity for the GDPR right to erasure |
| scope (who reads/writes), audit trail, rollback | ✅ Covered (Tier write protection + provenance.history + version-chain rollback). ⚠️ "First-pass error rate down 97%" is a restatement from product docs, not independently verified (REFERENCES I2d) |
| A wrong memory is not one failure — it **continually poisons every subsequent session** | ✅ This is the quantitative argument for our Reconcile red-line rule |

### Lens 4: Nvidia — The Hardware Landing Spot (Systems Perspective)

| Original finding | Adoption |
|---|---|
| Every memory decision ultimately lands on KV cache / HBM bandwidth; putting the full history into context is **quadratic** cost | ✅ Marketing ammunition: long-context approaches lose at the physical layer |
| construction is a pure prefill-type load and **must be treated as a background batch task**: rate-limited, batched, deferred to idle periods, never blocking the latency-sensitive path | ✅ Covered (idle-5s trigger + async dreaming) — the Nvidia perspective independently validates our scheduling decision |

### The Article's Step 13/15: Build Order (adopted directly as a development discipline)

> "Run it once by hand before scheduling any automation. If the output cannot genuinely change a decision, it does not deserve a schedule."

✅ **Adopted**: [PRD-02](../prd/PRD-02-dream-engine.md) gains the `dream --once` manual consolidation CLI — during the M1 phase, dreams are run manually first to verify distillation quality, and the automatic trigger is only turned on at the end of M1. This prevents "the system hallucinating a nonexistent connection across three notes, then training you to ignore it".

### The One Place Where the Article Corrects Us

> "Never auto-merge contradictions: two memories that disagree may both have been right in different contexts."

Our v4.0 first draft allowed automatic adjudication when "timestamps are clear / the source-authority gap is large enough". The article reminded us of a case we had missed: **two contradictory memories may both be right in different contexts** ("Go uses tabs" vs "Python uses spaces" — not a conflict, but different scopes).

✅ **Adopted as the third Reconcile branch: Context-Scoped Coexistence** — first try to resolve the conflict with cues: if the two contradicting sides can be delimited into their own scopes by project/situation/time cues, that is not a conflict but two memories with different applicability conditions, coexisting with each carrying its scope annotation; only when cues cannot delimit the scopes does it proceed to adjudication/flag_conflict. Neuroscience basis: **context-dependent memory** (Godden & Baddeley 1975) — memory is inherently bound to its retrieval context; a globally unique "fact" is an engineering illusion.

---

## 2. Claude-Mem Breakdown

**What it is**: a Claude Code plugin, hook-driven — automatically captures tool-call observations during the session, an AI compresses them into semantic summaries, and the next session injects them automatically. Apache-2.0, multi-host (Claude Code / Codex / Gemini / Copilot…).

**What's worth borrowing**:

| Concept | Neuroscience basis | Adoption |
|---|---|---|
| **Zero-friction capture via host lifecycle hooks** (SessionStart injection / in-session observation / compressed into storage) — zero user action | Automated encoding: hippocampal encoding needs no conscious effort (automatic encoding) | ✅ Our hooks pushing directly to the daemon `/ingest` is fundamentally the same, but **add**: proactively inject a "summary of last week's memories" as opening context at SessionStart (cold-start warm-up), not merely passively waiting for recall |
| **Progressive compression ladder** (raw → observation → summary, multi-level) | Multi-level representation of memory: episode → semantics → schema | ⚠️ Partially adopted: we already have the raw chunk → triple two-level structure; **not adopting** the intermediate levels — multi-level summarization accumulates distortion (each summary is a lossy compression; the telephone-game effect) |

**Its blind spots (our attack surface)**: no conflict reconciliation, no decay, flat summary-style storage, no causal graph, no cognitive grading — it belongs to the "flat retrieval + summary" family; of the four lenses, it only did "store" one.

---

## 3. MemPalace Breakdown (the system we use daily — best qualified for a detailed critique)

**What it is**: a local memory system prototyped on the "memory palace (method of loci)" — wing/room/drawer spatial organization, **verbatim storage (never summarized)**, semantic dedup on write (0.9 similarity threshold), a temporal knowledge graph (valid_from/valid_to + invalidate), hallways (entity co-occurrence links) / tunnels (cross-domain associations), and sync that cleans drawers whose source has been deleted.

**What's worth borrowing**:

| Concept | Neuroscience basis | Adoption |
|---|---|---|
| **Verbatim first, derived second**: the original text is never summarized; derived facts enter the KG separately — lossy operations are allowed only at the derived layer | Memory reproduction experiments: humans store verbatim and gist in **dual parallel channels** (Fuzzy-Trace Theory, Brainerd & Reyna); gist extraction does not erase the original | ✅ Covered (LanceDB raw chunks + graph triples, dual-track) — Fuzzy-Trace Theory gave us the theoretical name |
| **Semantic dedup on write** (check_duplicate; reject if similar) | Hebbian law (Hebbian): repeatedly activated synapses are strengthened rather than copied into a new synapse | ✅ **Adopted into Capture**: run a near-duplicate check before capture; on a hit against an existing memory, `last_reinforced` bounces and increments by 1 and no new chunk is created — **reinforcement happens at encode time, not waiting for the dream** |
| **Temporal KG**: invalidate rather than delete; valid_from/valid_to | Reconsolidation — old traces retained as historical versions | ✅ Covered (version chain) — independently verified |
| **hallways/tunnels association traversal**: entity co-occurrence and cross-domain associative retrieval | Spreading activation (Collins & Loftus): recalling one concept activates neighboring concepts | ⚠️ **Lightweight adoption**: our 2-hop graph traversal already carries the germ of spreading activation; add **co-occurrence edges** (two memories activated in the same session → edge weight +1) as a secondary re-ranking signal at retrieval — pure vector similarity cannot catch "frequently recalled together" |
| **sync source decay**: memories whose source file disappears are automatically demoted | Memory and source live and die together (the physical form of source monitoring) | ✅ Adopted into Decay: memories whose `provenance.source` is invalidated (e.g. the referenced session is deleted) are automatically down-weighted |
| **The memory palace spatial metaphor itself** | Place cells / cognitive maps (O'Keefe; Tolman): space is the oldest index structure in human memory | ❌ Not adopting wing/room namespaces — our index is semantic + situational cues; the spatial metaphor is friendly for a personal assistant, but for code memory it is a redundant indirect layer |

---

## 4. Consolidated Borrowing Table (Decision Record)

| Source | Concept | Decision | Landing point |
|---|---|---|---|
| N01ennn/Stanford | cost per correct answer metric | ✅ Adopted | PRD NFR + marketing |
| N01ennn/Anthropic | user-exportable/deletable (export + forget_this) | ✅ Adopted | [PRD-03](../prd/PRD-03-retrieval-mcp.md) FR-3.1 tool-set expansion |
| N01ennn | run manually first, then automate | ✅ Adopted | [PRD-02](../prd/PRD-02-dream-engine.md) `dream --once` |
| N01ennn | contradictions may each hold → context-scoped coexistence | ✅ Adopted | [design 01 §4](01-memory-pipeline.md), third Reconcile branch |
| N01ennn | no champion among the four families; deliberately choose the cost | ✅ Adopted as positioning | whitepaper/marketing |
| Claude-Mem | proactive warm-up injection at SessionStart | ✅ Adopted | [PRD-03](../prd/PRD-03-retrieval-mcp.md) |
| Claude-Mem | multi-level progressive summarization | ❌ Rejected | telephone-game distortion |
| MemPalace | near-duplicate reinforcement at capture (Hebbian encode-time reinforcement) | ✅ Adopted | [design 01 §1](01-memory-pipeline.md) Capture |
| MemPalace | co-occurrence edges (spreading activation) | ✅ Lightweight adoption | [design 03](03-storage-and-retrieval.md) retrieval re-ranking signal |
| MemPalace | automatic down-weighting on source invalidation | ✅ Adopted | [design 01 §5](01-memory-pipeline.md) Decay |
| MemPalace | verbatim/gist dual channels | ✅ Already existing — gained a theoretical name | Fuzzy-Trace Theory |
| MemPalace | spatial organization of the memory palace | ❌ Rejected | redundant indirect layer |
