# MnemoSeed for Codex CLI

Persistent, provenance-first memory for Codex CLI. A running MnemoSeed daemon
(`mnemoseed up`) exposes localhost HTTP; this adapter bridges Codex CLI to it.

## What it does

- **Capture (FR-6.3c)** — `UserPromptSubmit` records every user prompt into the
  daemon (`/ingest`); `SessionEnd` reads the session transcript
  (`transcript_path`, rollout.jsonl) for bounded full-fidelity capture and then
  settles the session.
- **Injection (FR-6.3c, AC-7)** — `SessionStart` warms the session with the
  remembered context for the project (≤800 tokens); `UserPromptSubmit` injects a
  per-turn recall up to Codex's **2,500-token** `additionalContext` cap. Both
  share one 2s hook budget and fail open.
- **Standing guidance** — an `AGENTS.md` fragment tells the agent to
  `memory.recall` before answering memory-dependent questions and to
  `memory.remember` durable facts.
- **MCP recall** — the `mnemoseed` MCP server registration lives in
  `~/.codex/config.toml` (written by `mnemoseed install`), separate from these
  hooks, so the two config paths never mix.

**Trust review is required (FR-6.3c, AC-8).** Codex runs user-managed hooks only
after you trust them by hash. Without the review the hooks silently never
execute:

- run `codex`, issue `/hooks`, and approve the MnemoSeed hooks by their hash.

The installer prints this guidance whenever it plans the Codex hooks.

Every hook exits 0, shares one 2s deadline across its daemon calls, fails open
on any daemon problem, and never spends LLM tokens. When the daemon is down,
Codex works exactly as before — just without memory.

## Install

```
mnemoseed install       # detects ~/.codex; registers the MCP entry in
                        # config.toml (T2) and writes the hooks + AGENTS.md
                        # fragment as separate approvable items
```

writes `~/.codex/hooks.json`, `~/.codex/mnemoseed/*` (the hook scripts plus a
stdlib-only HTTP client), and appends the MnemoSeed guidance to
`~/.codex/AGENTS.md`. Each item is backed up, diffed, and confirmed before
writing; `mnemoseed uninstall` removes exactly what was written.

## Layout

```
templates/hooks.json              event wiring for the three supported hooks
templates/agents.md               AGENTS.md standing guidance fragment
templates/hooks/*.py              one thin entry point per hook event
templates/hooks/py.sh             shell interpreter-resolution shim
templates/hooks/mnemoseed_hook_client.py   stdlib-only shared client
```

The hook scripts are stdlib-only by design: Codex runs them in arbitrary
environments, so the adapter never depends on the `mnemoseed` package or a
prepared virtualenv. The `bash` shim used in `hooks.json` requires a POSIX shell
on PATH (Git Bash on Windows works).

## Hooks

| Event              | Capture                          | Injection                     |
| ------------------ | -------------------------------- | ----------------------------- |
| `SessionStart`     | —                                | warm-up recall ≤800 tokens    |
| `UserPromptSubmit` | prompt text                      | per-turn recall ≤2,500 tokens |
| `SessionEnd`       | transcript (rollout.jsonl) + settle | —                         |

## Environment

| Variable                    | Default                 | Meaning                         |
| --------------------------- | ----------------------- | ------------------------------- |
| `MNEMOSEED_BASE_URL`        | `http://localhost:7788` | daemon base URL                 |
| `MNEMOSEED_PROFILE_ID`      | `default`               | profile identity for hook calls |
| `MNEMOSEED_HOOK_BUDGET_SECONDS` | `2.0`               | per-hook wall-clock budget (s)  |
| `MNEMOSEED_TOOL_OUTPUT_CAP` | `100000`                | captured tool-output char cap   |
| `MNEMOSEED_SESSION_START_QUERY` | (built-in)         | warm-up recall query override   |
| `MNEMOSEED_ADAPTER_TEMPLATES` | (repo defaults)      | override the template source dir |

## License

MIT
