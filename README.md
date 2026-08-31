![MnemoSeed](assets/banner.png)

# MnemoSeed

**One memory for every AI agent you use.**

[![License: Dual AGPL/Commercial](https://img.shields.io/badge/License-Dual%20AGPL%2FCommercial-blue)](#license)
[![CI](https://github.com/MnemoSeed/mnemoseed/actions/workflows/ci.yml/badge.svg)](https://github.com/MnemoSeed/mnemoseed/actions/workflows/ci.yml)

Your Claude forgets everything when you open Grok. Your Cursor has no idea what Codex did yesterday. MnemoSeed is a local memory layer that sits underneath all of them. It captures what matters while you work, consolidates it in the background, and injects the right context into whichever agent you open next.

```
# install once, works everywhere
uv tool install mnemoseed
mnemoseed up        # local daemon, zero Docker needed
mnemoseed install   # wires up Claude Code, Cursor, Codex, Grok Build
```

## How it works

1. **Capture** — a deterministic scorer decides what is worth keeping (no LLM call, no API tokens). Logs and noise get stripped, durable facts get stored verbatim.
2. **Dream** — when enough has accumulated, a background pass consolidates raw chunks into a structured knowledge graph. Interrupt it any time; nothing blocks your session.
3. **Recall** — the next agent (any agent) gets a small, budgeted context package. Hard token cap, conflict pairs returned as-is, and an honest "I don't have anything" when there's nothing.

Everything is local-first. A typical dream run costs about $0.001 against cloud models, and $0 on the offline track.

## What you get

- **Cross-model memory** — Claude Code, Cursor, Codex, Grok Build, and anything that speaks MCP
- **A graph you can audit** — every fact carries provenance (who said it, when, from which session), and history is never overwritten
- **Cost you can predict** — dynamic budget per dream (5k–32k tokens, sized by actual backlog) plus a monthly token ledger with a hard cap
- **Privacy by default** — local-first by default; encrypted transport and encrypted at-rest storage wherever the daemon runs, and cloud dreams only ever leave through zero-retention (ZDR) model endpoints

## Status

**Shipped:** capture pipeline, dream engine, hybrid retrieval, six MCP tools (`memory.recall` / `remember` / `audit` / `timeline` / `export` / `forget_this`), installer + doctor + uninstall, Claude Code plugin, Cursor / Codex / Gemini adapters. ~1,100 tests green on every push.

**Roadmap:** management console, hosted cloud daemon (running in a TEE as standard), the anima personality module (spec in `docs/design/09`).

Design docs live in `docs/design/` (English) and `docs/zh/` (中文工作稿).

## Development

Test-driven, with an adversarial verifier on every task: failing tests first, mutation spot-checks before any merge. Gates: `uv run pytest -q`, `ruff check`, `ruff format --check`, `mypy src`.

## Sponsor MnemoSeed

**One memory under all your coding agents — local-first, cloud-optional, E2EE+TEE ready.**  
MnemoSeed is a dual-licensed (AGPL/Commercial) memory layer that runs the theory-driven pipeline: capture → dream → decay → retrieve. Every prompt and response is stored verbatim, scored for importance, distilled by an offline dream pass, and forgotten on an Ebbinghaus curve — so the next session starts with what matters, not a blank slate.

Your sponsorship keeps the dream model running (Opus-class inference), covers domain/infra for cloud TEE validation, and funds the daily burn of building in public.

### Tiers

| Tier | Monthly | What you get |
|------|---------|--------------|
| **Supporter** | $5 | Name in README Contributors |
| **Backer** | $25 | Priority issue response + monthly dream-cost/eval brief (real numbers) |
| **Sponsor** | $100 | README logo + quarterly roadmap sync (no feature promises) |
| **Enterprise** | $500 | Direct support channel + private deployment consult (pre-warms Cloud) |

One-time tips also welcome via Polar (below) or Buy Me a Coffee.

### Channels

[![GitHub Sponsors](https://img.shields.io/badge/Sponsor-GitHub-%23EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/MnemoSeed)
[![Polar](https://img.shields.io/badge/Donate-Polar-%230066FF?logo=polar&logoColor=white)](https://buy.polar.sh/polar_cl_KoNqAsMI8IQGy5RRE8MLbX7I5TBGUMh9Kn8CC35YfP7)
[![Buy Me a Coffee](https://img.shields.io/badge/Tip-Buy%20Me%20a%20Coffee-%23FFDD00?logo=buymeacoffee&logoColor=black)](https://buymeacoffee.com/mnemoseed)

> **Transparency promise**: monthly dream-cost figures, evaluation data, and sponsor counts/amounts are published in the Backer brief and quarterly sync. No feature gating, no license upsell, no roadmap over-promising.

[![Thanks.dev](https://img.shields.io/badge/Support-Thanks.dev-%23FF6B35)](https://thanks.dev/github/MnemoSeed/mnemoseed)

## License

Dual-licensed: **AGPL-3.0** (free, including commercial use, as long as derivatives stay open) or a **commercial license** for proprietary/closed-source integration. Contact `license@mnemoseed.com`.
