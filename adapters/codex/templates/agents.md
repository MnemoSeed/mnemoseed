## MnemoSeed memory

A MnemoSeed daemon captures this session through Codex hooks into a persistent,
provenance-first memory store. Follow these rules:

- **Recall before answering.** When an answer depends on the user's preferences,
  past decisions, project constraints, or earlier context, call `memory.recall`.
- **Remember durable facts.** When the user states a preference, decision,
  constraint, or completed task that future sessions should know, call
  `memory.remember` once. Capture is automatic via hooks; remember only
  distilled, durable facts.
- **Per-turn injection.** Relevant memory is attached to every user prompt (up to
  2,500 tokens). Use it as context, and call `memory.recall` for deliberate,
  deeper searches.
- **Check freshness when unsure.** If a memory may be outdated, call
  `memory.audit` before relying on it.
