# PRD-06 · Host Integration & Install Experience (daemon + installer + Tier 1 host adapters)

> Design doc: [06-host-integration](../design/06-host-integration.md)
> Milestone: M1 · Estimate 9.5 days (revised after the 2026-08-08 empirical host research) · v1.1 · 2026-08-13

## 1. Goals

One command, three minutes, zero accounts: install MnemoSeed into every AI host the user already has, and automatically produce the first "it remembers me" experience in the next session.

## 2. Scope

- **In**: mnemoseed-daemon (embedded mode), installer (probe/register/doctor), Tier 1 host adapters (Claude Code plugin / Cursor hooks / Codex CLI hooks / Gemini CLI extension), MCP instructions downgrade mode (Tier 2 floor), uninstall
- **Out**: cloud-sync login (PRD-05), the docker-compose family (skeleton already exists in M0), **Tier 2 desktop Chat deepening** (.mcpb packaging, MCP Apps memory UI, ChatGPT hosted endpoints/tunnel — included only as a handy by-product of the MCP server, not part of the M1 acceptance; OpenCode/Windsurf adapters deferred to P2)

> Host tiering and the empirically-tested capability matrix: see design/06 §2 (2026-08-08, tested against official docs).

## 3. Functional Requirements

| ID | Requirement | Priority |
|---|---|---|
| FR-6.1 | installer: host probing (~/.claude.json, ~/.cursor/, codex config) → user confirms the integration list → pre-registration backup + diff preview + item-by-item confirmation | P0 |
| FR-6.1b | `mnemoseed login [--baseurl]`: local passwordless confirmation / cloud-account authentication; profile creation and token issuance (identity model: see design/06 §2.6) | P0 |
| FR-6.1c | `mnemoseed link/unlink`: select a profile per agent and write `profile_id + token` as env into each agent's native configuration (MCP entries / OpenClaw and Hermes's respective configs / Claude Code user+project scope / Codex `shell_environment_policy.set`); the host UI always shows exactly one mnemoseed entry | P0 |
| FR-6.1d | `mnemoseed whoami`: proves the current environment's identity (profile/daemon/token validity); `mnemoseed status` reads back all host configurations and produces a binding summary table | P0 |
| FR-6.1e | First-time registration flow: daemon first start with no owner → setup state (memory APIs return 503 with guidance; only `/console/setup` is available); setup creates the owner (argon2 password hash) and is allowed exactly once, after which the endpoint is permanently closed; `mnemoseed auth reset` resets the password locally | P0 |
| FR-6.1f | Open-source single-user hard limit: user management belongs to the owner only; "add user" is locked with the activation path noted (official cloud / commercial license); license activation entry (Ed25519 offline verification; entitlements include multi_user/seats/validity period; 30-day grace after expiry, and data is never touched) | P0 |
| FR-6.2 | daemon embedded mode: a single process embedding LanceDB + SQLite-Graph + bge-m3 ONNX (~543MiB int8-quantized model download with resumable transfer), zero Docker dependency (default stack selection: see design/03 §1) | P0 |
| FR-6.3 | Claude Code plugin (marketplace-distributed; a single bundle containing hooks + MCP + commands + skills): SessionStart warm-up injection (additionalContext ≤800 tokens, well under the 10,000-character limit) / **UserPromptSubmit per-turn injection** (daemon returns highly relevant memories within 2s; timeout or empty results fail open without injecting) / UserPromptSubmit+PostToolUse automatic capture / PreCompact flush / SessionEnd settlement (the Stop hook respects the contiguous-8-block cap and the stop_hook_active check); slash commands: /memory /dream /forget /recall | P0 |
| FR-6.3b | Cursor adapter: `.cursor/hooks.json` (project-level) — afterAgentResponse full-text capture + postToolUse capture + sessionStart warm-up injection; `.cursor/rules/*.mdc` (alwaysApply) as persistent reading guidance; **per-turn injection unavailable** (beforeSubmitPrompt can only block), so reading relies on warm-up + rules + MCP recall; verify how much of the Claude Code hooks compatibility layer can be reused | P0 |
| FR-6.3c | Codex CLI adapter: `~/.codex/hooks.json` — SessionStart warm-up + UserPromptSubmit per-turn injection (≤2,500 tokens) and capture + SessionEnd transcript settlement; AGENTS.md as persistent guidance; **the installer must guide the user through the `/hooks` trust review** (by hash, otherwise hooks silently won't run) | P1 |
| FR-6.3d | Gemini CLI adapter: single extension package (MCP + GEMINI.md + hooks + commands) — SessionStart warm-up + BeforeAgent per-turn injection + AfterTool capture | P1 |
| FR-6.4 | Hooks connect directly to the daemon's localhost HTTP, bypassing MCP, with a 2s timeout fail-open and zero token consumption | P0 |
| FR-6.5 | MCP initialize `instructions` downgrade-mode behavioral guidance (Tier 2 hosts), **self-contained copy ≤512 characters** (the official Codex recommended limit; Claude Code truncates at 2KB, so the stricter one applies), paired with idempotent remember de-duplication | P1 |
| FR-6.6 | `mnemoseed doctor`: daemon alive / port / embedding load / round-trip read-write test / host registration in effect; each failed item gets a one-line fix command | P0 |
| FR-6.7 | `mnemoseed uninstall`: per-host deregistration (backup restoration or precise removal), stop the daemon, keep data by default with the path made explicit, delete only with --purge | P1 |
| FR-6.8 | Single source of truth for configuration: `~/.mnemoseed/config.toml`; host side keeps only a thin registration | P0 |
| FR-6.9 | First-time LLM setup wizard (a post-setup step of `mnemoseed onboard`, FR-6.10): guides dream-model configuration in the recommended order ① OAuth (reuse the host's local login state: Codex / Grok, both ToS-allowed; Anthropic subscriptions not reused; Chinese users may choose CLI providers such as MiniMax/Kimi, with an explicit data-residency-exit notice shown when selected) ② bring-your-own API key (any OpenAI-compatible endpoint, e.g. Fireworks) ③ the advanced offline track (Ollama, ≤14B quantized model, with a distillation-quality warning); config.toml is written only after connectivity passes; implements the PRD-02 FR-2.14 role routing | P0 |
| FR-6.10 | `mnemoseed onboard`: a guided, step-by-step aggregate over the existing primitives — ① owner account setup → ② storage preset choice → ③ dream LLM wizard (FR-6.9) → ④ host link → ⑤ autostart → ⑥ doctor all-green. Rules: ① shares **one** backend onboard service with the console setup wizard — no parallel logic (the `/api/v1/setup` endpoint stays exact-once); ② the LLM wizard is a POST-setup step (the wizard keeps its connectivity-test-before-persist behavior); ③ every step is skippable and resumable — skipping the LLM step yields a bootable capture-only daemon, documented in the wizard; ④ the host-link step reuses the install backup + diff preview + per-item confirmation discipline unchanged (FR-6.1); ⑤ TTFM < 3 min remains the happy-path target and each step is timeboxed; ⑥ config operations are loopback-only — against a non-loopback baseurl they fail with a clear error | P0 |

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-6.1 | Time-to-First-Memory < 3 minutes (fresh machine, normal network speed, embedded mode) |
| NFR-6.2 | hook injection/capture p95 < 50ms; host experience is zero-impact when the daemon is unreachable (fail-open) |
| NFR-6.3 | All modifications to the user's existing configuration are rollback-able (timestamped backup files kept for 30 days) |

## 5. Acceptance Criteria

- AC-1: A fresh Windows/Mac machine completes installation with a single command; doctor is fully green; total time < 3 minutes;
- AC-2: After installation, without opening any new window, the next Claude Code session opening automatically shows the recent-memory summary injection;
- AC-3: Say "from now on I use pnpm" in a session without calling any tool; the next day's new-session warm-up summary contains that preference;
- AC-4: After uninstall, each host's configuration is restored to its pre-install state, with an empty diff;
- AC-5: After the daemon process is killed, Claude Code still starts and converses normally, only without memory injection;
- **AC-6 (per-turn deterministic capture)**: in a Claude Code session, every turn of user input is written to the daemon via the UserPromptSubmit hook — zero token consumption, zero model involvement (no memory-related tool calls in the host transcript); in Cursor, every turn of assistant reply is captured in full via afterAgentResponse;
- **AC-7 (per-turn injection)**: in Claude Code / Codex CLI, the user asks a mid-session question related to historical memories (without @-mentioning anything, without prompting the use of memory); the relevant memories injected by the daemon (additionalContext) automatically appear alongside that turn's prompt; p95 injection latency < 2s, and a timeout does not block the conversation;
- **AC-8 (Codex trust guidance)**: after installation in a fresh Codex CLI environment, the installer outputs `/hooks` trust guidance; once the user completes trust, all hooks take effect;
- **AC-9 (Tier 2 floor)**: in an MCP-only environment (simulating a desktop Chat scenario), with the daemon's instructions field delivered at ≤512 characters, the model can autonomously complete one recall → answer → remember loop beyond the system prompt (probabilistic; a 100% requirement is not made).

> Tier 2 desktop Chat (Claude Desktop Chat / ChatGPT product surface) is not part of the M1 acceptance; see design/06 §2.2–2.3.

## 6. Task Breakdown

1. `daemon/embedded` — single-process packaging (uv distribution as the primary path, see design/03 §1) (2d)
2. `installer/` — probe/register/backup/doctor/uninstall + Codex `/hooks` trust guidance (3d)
3. `plugins/claude-code/` — hooks (incl. UserPromptSubmit per-turn injection) + slash commands + marketplace manifest (2d)
4. `adapters/cursor/` — hooks.json + rules templates (1d)
5. `adapters/codex/` + `adapters/gemini/` — hooks + AGENTS.md/GEMINI.md guidance snippets (1d)
6. MCP instructions (≤512 characters) + downgrade-mode e2e (0.5d)

> Total estimate ≈ 9.5 days (original 8 days + 1.5 days for Cursor/Codex/Gemini adapters).

## 7. Dependencies

- M0 (schema base), PRD-01 (Capture funnel), PRD-03 (digest/recall API)
