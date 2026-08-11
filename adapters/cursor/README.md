# MnemoSeed for Cursor

Persistent, provenance-first memory for Cursor. A running MnemoSeed daemon
(`mnemoseed up`) exposes localhost HTTP; this adapter bridges Cursor to it.

## Capabilities — and the honest limits

Cursor's hook surface is narrower than Claude Code's, and two capabilities are
**not available** on Cursor:

- **Per-turn injection is unavailable.** `beforeSubmitPrompt` can only block a
  prompt, never rewrite or append to it, so MnemoSeed cannot attach a per-turn
  recall next to every prompt the way the Claude Code plugin does.
- Reads therefore come from three paths: the **session-start warm-up**, the
  **standing rules file** (`.cursor/rules/mnemoseed.mdc`, always applied), and
  the agent's own **`memory.recall` MCP calls**.

What the adapter does provide:

- **Capture (FR-6.3b, AC-6)** — `afterAgentResponse` hands the full assistant
  response text to the daemon (`/ingest`); `postToolUse` captures tool calls.
  Both are exact, automatic, and zero-token.
- **Warm-up (FR-6.3b)** — `sessionStart` fetches a budgeted recall (≤800
  tokens) and injects it as `additional_context` when the daemon answers within
  the 2s budget.
- **Standing guidance** — `.cursor/rules/mnemoseed.mdc` (`alwaysApply: true`)
  tells the agent to `memory.recall` before answering memory-dependent
  questions and to `memory.remember` durable facts.
- **MCP recall** — register the `mnemoseed mcp` server (via `mnemoseed install`
  into `~/.cursor/mcp.json`, or however you configure MCP for Cursor) so the
  model can search memory on demand.

Every hook exits 0, shares one 2s deadline across its daemon calls, fails open
on any daemon problem, and never spends LLM tokens. When the daemon is down,
Cursor works exactly as before — just without memory.

## Install

From a project directory:

```
mnemoseed install --cursor-project .
```

writes `.cursor/hooks.json`, `.cursor/hooks/mnemoseed/*` (the hook scripts plus
a stdlib-only HTTP client), and `.cursor/rules/mnemoseed.mdc`. Each item is
backed up, diffed, and confirmed before writing; `mnemoseed uninstall` removes
exactly what was written. The `.cursor/` directory can be committed so every
clone of the project inherits the hooks.

## Layout

```
templates/hooks.json              event wiring for the three supported hooks
templates/hooks/*.py              one thin entry point per hook event
templates/hooks/py.sh             shell interpreter-resolution shim
templates/hooks/mnemoseed_hook_client.py   stdlib-only shared client
templates/rules/mnemoseed.mdc     alwaysApply standing read guidance
```

The hook scripts are stdlib-only by design: Cursor runs them in arbitrary
environments, so the adapter never depends on the `mnemoseed` package or a
prepared virtualenv. The `bash` shim used in `hooks.json` requires a POSIX
shell on PATH (Git Bash on Windows works).

## Hooks

| Event               | Capture                        | Injection                  |
| ------------------- | ------------------------------ | -------------------------- |
| `sessionStart`      | —                              | warm-up recall ≤800 tokens |
| `postToolUse`       | tool name / input / output (capped) | —                      |
| `afterAgentResponse`| full assistant response text   | —                          |

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
