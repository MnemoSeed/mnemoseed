# MnemoSeed for Gemini CLI

Persistent, provenance-first memory for Gemini CLI. A running MnemoSeed daemon
(`mnemoseed up`) exposes localhost HTTP; this extension bridges Gemini CLI to
it.

## What it does

- **Capture (FR-6.3d)** — `BeforeAgent` records every incoming agent request into
  the daemon (`/ingest`); `AfterTool` captures tool calls. Both are exact,
  automatic, and zero-token.
- **Injection (FR-6.3d)** — `SessionStart` warms the session with the remembered
  context for the project (≤800 tokens, injected as the first history turn);
  `BeforeAgent` injects a short per-turn recall (≤200 tokens) on every agent
  turn — the strongest per-turn read path alongside Claude Code. Both share one
  2s hook budget and fail open.
- **Standing guidance** — `GEMINI.md` tells the agent to `memory.recall` before
  answering memory-dependent questions and to `memory.remember` durable facts.
- **Commands** — `/recall`, `/memory`, `/dream`, `/forget` talk to the daemon
  directly.
- **MCP recall** — the extension declares the `mnemoseed` MCP server (`mnemoseed
  mcp`) so the model can search memory on demand.

Every hook exits 0, shares one 2s deadline across its daemon calls, fails open
on any daemon problem, and never spends LLM tokens. When the daemon is down,
Gemini works exactly as before — just without memory.

## Install

Gemini CLI distributes extensions as packages, not as files you drop into a
user-level directory, so this repo ships the extension as a self-contained
template directory. Install it with the Gemini CLI extension mechanism:

```
gemini extensions install <path>/adapters/gemini/templates
```

anywhere the folder lives (a clone of this repo, or a copy you keep elsewhere).
Exactly one extension is served per install; its package granularity is the
whole `templates/` folder.

## Layout

```
extension.json                 extension manifest (hooks, commands, MCP server)
GEMINI.md                      standing context guidance
hooks/*.py                     one thin entry point per hook event
hooks/py.sh                    shell interpreter-resolution shim
hooks/mnemoseed_hook_client.py stdlib-only shared client
commands/*.md                  slash-command definitions
scripts/*.py                   slash-command backends
```

The hook and command scripts are stdlib-only by design: Gemini runs them in
arbitrary environments, so the extension never depends on the `mnemoseed`
package or a prepared virtualenv. The `bash` shim used in `extension.json`
requires a POSIX shell on PATH (Git Bash on Windows works).

## Hooks

| Event            | Capture                       | Injection                     |
| ---------------- | ----------------------------- | ----------------------------- |
| `SessionStart`   | —                             | warm-up recall ≤800 tokens    |
| `BeforeAgent`    | incoming agent request        | per-turn recall ≤200 tokens   |
| `AfterTool`      | tool name / input / capped output | —                         |

## Environment

| Variable                    | Default                 | Meaning                         |
| --------------------------- | ----------------------- | ------------------------------- |
| `MNEMOSEED_BASE_URL`        | `http://localhost:7788` | daemon base URL                 |
| `MNEMOSEED_PROFILE_ID`      | `default`               | profile identity for hook calls |
| `MNEMOSEED_HOOK_BUDGET_SECONDS` | `2.0`               | per-hook wall-clock budget (s)  |
| `MNEMOSEED_TOOL_OUTPUT_CAP` | `100000`                | captured tool-output char cap   |
| `MNEMOSEED_SESSION_START_QUERY` | (built-in)         | warm-up recall query override   |

## License

MIT
