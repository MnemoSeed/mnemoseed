# MnemoSeed for Claude Code

Persistent, provenance-first memory for Claude Code. A running MnemoSeed daemon
(`mnemoseed up`) exposes localhost HTTP; this plugin bridges Claude Code to it.

## What it does

- **Capture (AC-6, FR-6.3)** — hooks record every user prompt, tool result, and
  assistant message as timestamped, host-tagged events into the daemon, where
  they are segmented into turns and drained into the store.
- **Injection (AC-7)** — `SessionStart` warms the session with the remembered
  context for the project (≤800 tokens); `UserPromptSubmit` injects a short
  per-turn recall (≤200 tokens). Both share one 2s hook budget and fail open.
- **PreCompact rescue** — a compaction flush closes the in-flight turn without
  settling the session, so a mid-session compact never loses it.
- **Settlement** — `SessionEnd` drains and settles the session; `Stop` captures
  the final assistant message but never blocks the stop.
- **Slash commands** — `/recall`, `/memory`, `/dream`, `/forget` talk to the
  daemon directly.

## Layout

```
.claude-plugin/plugin.json   plugin manifest (commands, hooks, MCP server)
hooks/hooks.json             event wiring for the six supported hooks
hooks/*.py                   one thin entry point per hook event
hooks/py.sh                  shell interpreter-resolution shim
mnemoseed_hook_client.py     stdlib-only shared client (HTTP, budget, context)
scripts/*.py                 slash-command backends
commands/*.md                slash-command definitions
```

The hook scripts are stdlib-only by design: Claude Code runs them in arbitrary
environments, so the plugin never depends on the `mnemoseed` package or a
prepared virtualenv.

## Hooks

| Event           | Capture                                  | Injection                |
| --------------- | ---------------------------------------- | ------------------------ |
| `SessionStart`  | —                                        | warm-up recall ≤800 tok  |
| `UserPromptSubmit` | prompt text (AC-6)                    | per-turn recall ≤200 tok |
| `PostToolUse`   | tool name / input / capped output        | —                        |
| `PreCompact`    | flush signal (in-flight turn rescued)    | —                        |
| `Stop`          | final assistant message (once per turn)  | —                        |
| `SessionEnd`    | settle (drain + end)                     | —                        |

Every hook exits 0, shares one 2s deadline across its daemon calls, fails open
on any daemon problem, and never spends LLM tokens.

## Environment

| Variable                    | Default                | Meaning                         |
| --------------------------- | ---------------------- | ------------------------------- |
| `MNEMOSEED_BASE_URL`        | `http://localhost:7788`| daemon base URL                 |
| `MNEMOSEED_PROFILE_ID`      | `default`              | profile identity for hook calls |
| `MNEMOSEED_HOOK_BUDGET_SECONDS` | `2.0`              | per-hook wall-clock budget (s)  |
| `MNEMOSEED_TOOL_OUTPUT_CAP` | `100000`               | captured tool-output char cap   |
| `MNEMOSEED_SESSION_START_QUERY` | (built-in)         | warm-up recall query override   |

## MCP server

The plugin registers a `mnemoseed` MCP server (`mnemoseed mcp`) so tools such
as `/memory/remember` and `/memory/audit` are available alongside the hooks. It
inherits the session's `MNEMOSEED_PROFILE_ID` and `MNEMOSEED_TOKEN` through
`${VAR}` expansion in the manifest's `env` block. Requires the `mnemoseed` CLI
on PATH.

If the same daemon was also registered by `mnemoseed install`, its commands
live in a different tool namespace (`mcp__plugin_mnemoseed_mnemoseed__*` for the
plugin, `mcp__mnemoseed__*` for the installer) so the two never collide, but
there is no need to keep both: prefer the plugin tools while the plugin is
enabled, or the install-managed MCP server when it is not.

## License

MIT
