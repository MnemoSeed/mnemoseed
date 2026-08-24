<!-- mirror of docs/zh/design/ux/模型路由配置-UX.md; zh is canonical -->

# Model Routing Configuration UX (Dream LLM configuration · console ⑧ / first-run wizard / CLI onboard)

## 1. Problem Statement

Users without an infrastructure background cannot complete the dream-LLM provider configuration surface correctly on the first try. Typical sticking points:

- Not knowing what each field corresponds to or what to fill in: where the endpoint goes, where the API key goes, where the model is chosen from (Fireworks.ai as the example; the same confusion applies to OpenRouter and Anthropic);
- Selecting `openai_compatible` as the connection method and still seeing an `oauth provider` input on the surface — a dead input unrelated to the current selection, clearly confusing.

Root cause: the UI speaks the code's terminology (driver names, "env var", "oauth provider") rather than the user's mental model ("I have a Fireworks account — how do I use it?"); effective defaults are invisible; inputs that do not apply to the current selection are displayed as-is.

This spec redesigns the three surfaces that share one mental task — pick a provider, connect MnemoSeed to it, verify it works, save — so that a first-time user with no infrastructure background can complete the flow in one pass.

---

## 2. Grounding (current code state)

### 2.1 The drivers that actually exist (verified in `src/mnemoseed/llm/drivers/`)

| Driver | What it is | Provider facts it serves |
|---|---|---|
| `openai_compatible` | `POST /chat/completions`, Bearer key; probe = `GET /models` | **Fireworks** and **OpenRouter** (both OpenAI-compatible) plus any other compatible endpoint |
| `anthropic` | Messages API `POST /v1/messages`, `x-api-key` + `anthropic-version`; probe = `GET /v1/models` | **Anthropic** (native, already exists — nothing to build) |
| `ollama` | native `POST /api/chat`, no key; probe = `GET /api/tags` | local **Ollama** (runs locally, no account needed; "fully offline" is a derived truth from the role composition, see note below) |
| `oauth` | reuses the host's `~/.codex/auth.json` / `~/.grok/auth.json`, OIDC refresh | host logins only: Codex / Grok (`SUPPORTED_PROVIDERS = ("codex", "grok")`) |
| `stub` | deterministic offline stub (testing / human review phases only) | must never be a user-visible provider |

There is **no** Fireworks or OpenRouter driver, and **none** needs to be built — both are served by `openai_compatible`. The native `anthropic` driver exists. There is **no** catalog endpoint; the catalog rides on probe results, returning `detail["models"]` on success (`openai_compatible.py:88`, `anthropic.py:95`, `ollama.py:78`).

**Role model (final)**: the dream engine has exactly two roles — `deep_reflection` (long-context deep-sleep reflection) and `short_increment` (short-increment merging). Each role can **independently point at any provider** (Fireworks / OpenRouter / Anthropic / Ollama / other OpenAI-compatible), and a cloud + local mix is a fully legitimate configuration that is never blocked or shamed. `local_track` is **no longer a role** — it survives only as a **deprecated config key**: accepted with a warning, no engine consumer. **Offline is a derived truth**: when all configured roles resolve to the local `ollama` driver, the page shows the "fully offline" badge; if any cloud role exists it does not show (no false privacy feel); there is no offline switch (§8 D10, §8.1).

### 2.2 Routing payload semantics (`admin.py:104-130`)

`GET /api/v1/llm/routes` returns per role: `driver`, `model`, `base_url`, `api_key_env`, `provider` — but `base_url`/`api_key_env`/`provider` are returned **only when explicitly set** (`table.get(...)`), so the effective defaults (`https://api.fireworks.ai/inference/v1`, the `MNEMOSEED_DEEP_REFLECTION_API_KEY,FIREWORKS_API_KEY` fallback chain) are **invisible** to the console edit form. The user opens "Edit route" and sees a blank base URL and blank key fields, with no knowledge of the defaults in effect. Once the SecretStore path is enabled (§5, §8 D1), `api_key_env` may also return a reference (`secrets:mnemoseed/dream/<role>`) — the UI must render that as a "key saved" state (masked tail), not a blank field.

### 2.3 Exact locations of the dead inputs

- **Wizard** (`console/static/app.js` `dreamSetupHtml`, ~lines 474-520): the BYOK form always renders five fields regardless of the selected driver, including `oauth provider` (placeholder `codex | grok`). It shows even when `openai_compatible` is selected. Confirmed in source.
- **console ⑧ edit form** (`llmEditFormHtml`, ~lines 2387-2407): the `oauth provider` text input renders for every driver, including openai_compatible / anthropic / ollama.

Why it exists: `provider` is a real routing parameter and the OAuth path needs it. But the UI exposes it as a text field across all flows. Correct approach: the OAuth path is a **separate routing choice**, not a field on a generic form (§6).

### 2.4 Terminology vs mental-model mismatch

- The wizard's driver dropdown lists raw names: `anthropic / oauth / ollama / openai_compatible / stub`. Users think in brands, not drivers.
- Placeholders are Anthropic-centric for every driver: model `e.g. claude-opus-5`, key `e.g. ANTHROPIC_API_KEY`. Both `claude-opus-5` and the `claude-sonnet-5` sample in the config (`config.py:376`) are **unverified model ids** — don't ship unverified ids in defaults.
- The wizard configures **only** `deep_reflection` (`submitWizard` POSTs to `/api/v1/llm/routes/deep_reflection`, app.js:572); there is no role explanation, no choice.
- Connectivity failures leak raw internal detail: `unreachable — {"error":"GET /models returned HTTP 401"}` (console) and `connectivity test failed: GET /models returned HTTP 401` (CLI `llm set`, `onboard`). No repair guidance.
- `stub` is a legitimate option in the wizard/console dropdowns — the test driver is put in front of users.

### 2.5 CLI `onboard` LLM step (`onboard/service.py:202-227`)

Prompts for `llm driver (e.g. ollama, anthropic, stub)` and `llm model` — no `base_url`, no `api_key_env`. As a result Fireworks/OpenRouter/Anthropic users **cannot** configure a cloud provider from the CLI at all (no key env var is collected → probe 401 → the step silently skips with "connectivity test failed"). The example drivers are all jargon and omit the actual default driver.

### 2.6 The two ways a key is held (must be taught)

**Primary path — SecretStore file backend, no restart.** An API key can be **pasted once** in console ⑧ / wizard / CLI; the daemon writes it to `~/.mnemoseed/secrets/<role>.key` (POSIX: file 0600, dir 0700; Windows: user-profile ACL). The config stores only a reference `secrets:mnemoseed/dream/<role>` — the key never enters the settings DB and is never echoed back to the UI. Changes apply **without a daemon restart**: routes re-resolve by generation, so a key change is picked up immediately (generation-bump re-resolve). The key is visible only at the moment it is pasted; afterwards only a masked tail `****1234` is shown, and it can be deleted with one click.

**Secondary path — environment variables (headless/CI).** Env-var names remain supported for headless deployments and CI (12-factor convention). The honest cost: `RoleRouter.resolve()` reads keys from the **daemon process environment** at **first materialization** and caches the instance (`routing.py:56-88`), so a **new env-var value** set in a *new terminal* is invisible to an *already running* daemon (on Windows, `setx` likewise affects only subsequently launched processes); the fix = "set the variable, then restart the daemon". This cost is now **optional** — interactive users take the primary path and skip the restart; headless environments accept it by default.

The UI must teach both paths: interactive surfaces steer to the primary path (paste = immediate effect); the env-var path appears only in headless/script scenarios; teaching blocks never say "must restart" for a key change (only for the env-var path, stated honestly). Industry basis (verified): Codex CLI `~/.codex/auth.json`, gh keychain-with-file-fallback + `GH_TOKEN`, Docker `config.json` base64, 12-factor env.

### 2.7 Doc vs code drift (must be resolved, not designed around)

| Promise | Code reality |
|---|---|
| FR-6.9: wizard order ① OAuth ② BYOK ③ offline track (*rewritten after finalization*: ③ offline track folded into the provider cards — Ollama card + quality hint, no separate offline sequence, see §8.1) | The wizard shows OAuth and BYOK side by side; the "use X OAuth" button merely pre-fills the same form. No sequenced guidance. |
| FR-6.9: "Chinese users may choose CLI providers such as MiniMax/Kimi, with an explicit data-residency-exit notice" | **Not implemented.** No MiniMax/Kimi provider and no egress notice anywhere. |
| FR-6.9: "Anthropic subscriptions explicitly not reused" | Code is correct — `oauth` supports only codex/grok; `anthropic` is key-only. Consistent. |
| design/02 §6: default deep_reflection → Kimi K3 (Fireworks), short_increment → DeepSeek V4 Flash (Fireworks); **local_track default route removed** (role model finalized, §8 D10) | Consistent with `DEFAULT_LLM_ROUTES` (`config.py:138-164`); it still contains a local_track entry, removed in the same engineering batch (§8.1). Fireworks model ids are verified in the config comments; treated as trusted defaults. |
| PRD-07 G-AC2: ⑧ configures all two roles (deep_reflection / short_increment) | Yes; the **wizard** only configures deep_reflection (+ the D4 share checkbox). Intentional but undocumented — see §8 decision D4. |

---

## 3. Design: a "provider-first route configurator" component

One component governs all three surfaces (§10). Its only job: **"I have a provider account — configure it so my dreams can run."** It never asks the user for a driver name, never shows fields that do not apply to the current selection, and never hides an effective default.

### 3.1 Step one — the provider picker (brand-first, driver-agnostic)

**Roles first, then providers.** The provider cards answer only "which provider", not "what for" — they apply equally to both dream roles (`deep_reflection` / `short_increment`); what is being edited is the role currently being edited. The ⑧ editor entry point is always a role card (§4 one-liner): after clicking "Edit route", that role's route editor expands the provider picker; the wizard defaults to editing `deep_reflection`, with the "also apply to short_increment" share checkbox decided in D4. This design has **no third role and no "third card"** — there is no `local_track` role card (§2.1 role model).

A group of radio cards, one per available path. Each card explains in one sentence "what you need":

```
◎ Fireworks                     OpenAI-compatible · pay-as-you-go · ~1,000 models
   "Best starting point — MnemoSeed's recommended models run here."
○ OpenRouter                    OpenAI-compatible · one key, many models
   "One API key for hundreds of models from many labs."
○ Anthropic / Claude            native API · requires an Anthropic API key
   "For Claude models (claude-opus / claude-sonnet class)."
○ Ollama on this computer       local · free · no account
   "Runs fully offline on this machine. Lower synthesis quality."
○ Another OpenAI-compatible API your chosen endpoint
   "Point at any other /chat/completions endpoint (e.g. a company gateway)."
```

Internal mapping (never shown verbatim to users, but reused in copy):

| Card | driver | base_url (pre-filled, editable) | key source (pre-filled, editable) |
|---|---|---|---|
| Fireworks | openai_compatible | `https://api.fireworks.ai/inference/v1` | `FIREWORKS_API_KEY` |
| OpenRouter | openai_compatible | `https://openrouter.ai/api/v1` | `OPENROUTER_API_KEY` |
| Anthropic | anthropic | `https://api.anthropic.com` | `ANTHROPIC_API_KEY` |
| Ollama | ollama | `http://localhost:11434` | (none — no key needed) |
| Other | openai_compatible | blank → required | default `MNEMOSEED_DEEP_REFLECTION_API_KEY` |

The key-source column holds env-var names (headless/CI path); the user may instead choose to **paste a key**, stored locally via the SecretStore (§2.6, §5) — both routes work.

**The OAuth path is a dual-path entry beside the provider selection** (identical in the wizard and the ⑧ editor, see §6): host-login cards (Codex / Grok, rendered in three states from `oauth-availability`) + a "paste a token instead" path — never a free-text field.

### 3.2 Step two — the morphing form

The form body changes with the selection (progressive disclosure):

- **The key field renders only for providers that need a key.** For Ollama the key block disappears.
- **The API key field supports two inputs**: (a) **paste a key** (primary path — the old "never ask for a key value" red line is superseded by the SecretStore: the value is handed to the daemon once over a local channel, written to `~/.mnemoseed/secrets/<role>.key`, never shown again, only the masked tail `****1234`, deletable); (b) **an env-var name** (headless/CI path, §2.6). A collapsible, OS-split "how to set it" teaching block sits below (§5).
- **base_url** pre-filled and editable, with a one-click "reset to <provider> default". Folded under "Advanced: endpoint" in the guided surfaces; fully expanded in the ⑧ editor.
- **model** is a **provider-scoped model picker** (§3.4): a live catalog (from the probe's `detail.models`) + curated per-provider suggestions visible before any probe + a **Load model list** button (fetches that endpoint's catalog without a probe) + free input always allowed. For Ollama the catalog comes from `GET /api/tags`, with a "pull it first" hint when the model is missing (`ollama pull llama3.1:8b`).

### 3.3 Step three — role assignment + test + save

The wizard states in one sentence which role it is configuring (§4); the ⑧ editor repeats that sentence on each role card. Then: **Test connection** → on success **Save is enabled**; on probe failure the form is kept with repair guidance (§7).

### 3.4 Provider-scoped model picker (industry pattern reference)

Making model selection "provider-scoped" — listing only that provider's models on that endpoint — is the de facto standard in today's IDEs and aggregators. Two reference implementations (verified; URLs recorded in §12):

- **OpenRouter** models page (https://openrouter.ai/models ): a provider-aggregated live catalog with instant search/filter and a copyable model id per row; the catalog is namespaced by provider (`provider/model`), matching `GET /api/v1/models`.
- **Cursor** models page (https://cursor.com/docs/models ): a pick-one model set with a pricing table, readable descriptions, and switch entry points per model — the user picks from a set rather than typing a model id.

Three behaviors are fixed accordingly (landed in §3.2 / §7): (1) the catalog is scoped to provider + endpoint, never mixing in another provider's models; (2) live catalogs usually run into the thousands, so the picker offers local search/filter; (3) free input is always available — the catalog is an accelerator, not a constraint.

---

## 4. Per-role guidance (one sentence per role, always visible)

Used on the ⑧ page (role-card subtitle) and in the wizard (one-line explanation).

The dream engine has exactly two roles (§8 D10). One plain-language sentence per role, used as the role-card subtitle on the ⑧ page and a one-line explanation in the wizard. The two roles can each independently choose a provider, unbound from each other — a cloud + local mix is normal usage.

| Role | One-sentence explanation (UI string) | Recommended pairing |
|---|---|---|
| deep_reflection | "The careful model. Reads your recent sessions and writes the distilled facts into long-term memory. Use the strongest model you can afford here." | Fireworks kimi-k3 (default) · Anthropic claude-opus class · any strong cloud model in budget (Ollama optional) |
| short_increment | "The quick model. Handles the frequent small consolidation passes. Use a fast, low-cost model." | Fireworks deepseek-v4-flash-0731 (default) · fast/low-cost cloud model (Ollama optional) |

**Quality-hint rule**: when either role **picks Ollama**, a one-line quality hint shows directly under the card/form — `Lower synthesis quality than cloud models — you accept this for privacy or cost.` Non-blocking, no second confirmation, informational only; identical in the wizard, the ⑧ editor, and the CLI (§11). Both roles pointing at Ollama = fully offline, and the header shows the derived badge (§9, §10.1).

The following terms must come with a tooltip/expander when they appear: **endpoint** ("the address where the provider receives MnemoSeed's requests"), **env var** ("a named value stored in your computer's environment — on the headless/CI path MnemoSeed reads the key from it; interactive surfaces prefer handing the key to MnemoSeed for local storage"), **context / max tokens** ("how much text the model is allowed to produce in one run"), **OpenAI compatible** ("the same API dialect spoken by Fireworks and OpenRouter — one code path serves both").

---

## 5. The API key teaching block ("where the key actually goes")

Rendered under the key field for every provider that needs a key. **The primary path is paste-once, no restart** (SecretStore file backend, §2.6, §8 D1); env vars are the headless/CI fallback. Layout (console/wizard):

```
API key
[ FIREWORKS_API_KEY        ]  ← env-var name (headless/CI)  ·  or paste a key once
[ •••••••••••••••••1234    ]  ← paste here once — never shown again
                               (key saved — ****1234)  [delete]

Paste your key once. MnemoSeed stores it locally under ~/.mnemoseed/secrets and
never displays it again — only this masked tail (****1234) is shown. It is never
written into settings, never uploaded to any MnemoSeed server, and you can
delete it any time. Changes apply immediately — no daemon restart.

1. Create a key:  https://app.fireworks.ai/settings/users/api-keys   [open]
2. Paste it here — or, for headless/CI, set it as an env var instead:
   (env fallback below; a NEW env value still needs a daemon restart)
```

Behavior points:

- **Paste path (primary)**: the key is visible only at the moment it is pasted; afterwards only the masked tail `****1234` shows, the saved chip carries a `delete` action, and deleting returns to the empty paste state. Changes apply immediately, **no daemon restart required**.
- **Env-var path (fallback, headless/CI)**: env-var names remain supported (12-factor convention). The honest cost is kept: `setx` / a **new** value in a new terminal is invisible to a running daemon — only on the env-var path does a key change need "set the variable, then restart the daemon" (§2.6). The teaching block says this plainly, and the probe (§7) confirms visibility (401 ⇒ re-paste the key or fix the env var).
- The copy is **customized per provider**: key-creation URL, standard env-var name, exact commands. macOS and Windows each get their own command tabs; a Windows GUI user never sees pure bash instructions and vice versa.

Provider quick-start facts (verified against official docs; URLs recorded in §12):

| Provider | Key creation | Env var | base_url | Catalog endpoint |
|---|---|---|---|---|
| Fireworks | app.fireworks.ai/settings/users/api-keys | `FIREWORKS_API_KEY` | `https://api.fireworks.ai/inference/v1` | `GET /models` |
| OpenRouter | openrouter.ai (account → keys) | `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` | `GET /api/v1/models` |
| Anthropic | platform.claude.com/settings/keys | `ANTHROPIC_API_KEY` | `https://api.anthropic.com` | `GET /v1/models` |
| xAI (Grok) | console.x.ai (API Keys page, sign-in required) | `XAI_API_KEY` | `https://api.x.ai/v1` | `GET /models` (OpenAI-compatible) |
| Ollama | none | none | `http://localhost:11434` | `GET /api/tags` |

---

## 6. OAuth visibility logic (killing the dead input)

**Rule: OAuth controls appear only when the OAuth path is genuinely offered, and the `provider` value can only be set by a dedicated control — never from a free-text field. The OAuth dual path (host-login cards / paste-a-token) exists identically in the wizard and the ⑧ editor.**

1. **Never** render a free-text `oauth provider` field in any BYOK/driver form. Remove it from `dreamSetupHtml` and `llmEditFormHtml`.
2. **Host-login cards (identical in wizard and ⑧ editor)** — the "reuse a login on this computer" panel renders above the provider cards, listing only the Codex / Grok host logins reported by `oauth-availability`, one card per provider, in three states:
   - `present && !expired` → **selectable card** `Use Codex login`. Clicking switches to **OAuth mode**: driver=oauth, provider=codex, the model field stays, base_url and key fields are hidden, and a banner shows: "MnemoSeed will use the Codex login on this machine — no key needed. It refreshes itself while you're signed in."
   - `present && expired` → **disabled card** "log in again first": the copy gives the **exact CLI command** (Codex: `codex login`, verified against official docs) and is copy-to-clipboard; saving that route is blocked until the user logs in again and returns.
   - `!present` → **disabled card** "log in to the <provider> CLI first" (the Codex host login lives in `~/.codex/auth.json`, Grok's in `~/.grok/auth.json`).
   - The model field in OAuth mode keeps curated suggestions (`gpt-5.6-codex` etc. **must be verified before release** — do not ship the current unverified placeholder).
3. **Paste-a-token path (second path, identical in wizard and ⑧ editor)** — "or paste a token instead": writes the token to the SecretStore via the key endpoint (same mechanism as §5), with **official doc links**: Codex → https://developers.openai.com/codex/auth (API-key sign-in section, verified); Grok → https://docs.x.ai/developers/quickstart (API Keys page at https://console.x.ai/team/default/api-keys, pointed to by the official docs; the console requires sign-in, marked "entry page"). OAuth providers stay configurable even when no host login exists.
4. **Save gate (that route only)** — an OAuth route whose login is unavailable (expired / absent) has **save blocked**, scoped to that route only, never touching the BYOK cards; the blocked copy gives the fix (log in again or paste a token, §11).
5. **CLI** — `--provider` remains a legitimate flag for `llm set` (scripting parity), but the interactive `onboard` wizard never asks for it as free text; it lists detected logins as numbered options and offers a "paste a token" alternative.

The per-field × per-provider-selection decision table (single source of truth for implementers):

| Field | openai_compatible (Fireworks/OR/other) | anthropic | ollama | oauth mode |
|---|---|---|---|---|
| API key (paste / env-var name) | ✅ visible, pre-filled | ✅ visible, pre-filled | hidden | hidden |
| base_url | ✅ visible (advanced) | ✅ visible (advanced) | ✅ visible (advanced) | hidden |
| model | ✅ visible + catalog | ✅ visible + catalog | ✅ visible + catalog | ✅ visible (suggestions) |
| oauth provider text field | **never** | **never** | **never** | **never** (controls only) |
| OAuth host-login card (Codex/Grok, three states) | ✅ top area | ✅ top area | ✅ top area | selected |
| max tokens (⑧ only) | ✅ advanced | ✅ advanced | hidden | ✅ advanced |

---

## 7. Probe / test UX (plain language, repair first)

### 7.1 States and copy

| Probe result | Presentation (UI string) |
|---|---|
| in progress | `Testing connection to Fireworks…`, standard loading style, button disabled |
| success | `Connected to Fireworks — key in FIREWORKS_API_KEY works. Found 1,204 models.` (green). The model dropdown fills from `detail.models`; **Save route** is armed. |
| probe ok but catalog empty | `No models listed — pick a suggestion, type the exact model id, or use Load model list.` |
| 401 / 403 | `Fireworks rejected the key in FIREWORKS_API_KEY. It's missing, wrong, or expired — check it at <provider key URL>, then paste a new key here.` (on the env-var path append "then fix the env var and restart") |
| connection refused / DNS (Ollama) | `Can't reach Ollama at http://localhost:11434. Is the Ollama app running? Install from ollama.com, then pull a model (ollama pull llama3.1:8b).` |
| connection refused / DNS (cloud) | `Couldn't reach <provider>. Check your internet connection or firewall, then try again.` |
| timeout | `Timed out talking to <provider>. The endpoint may be slow or blocked — check <endpoint> and try again.` |
| unknown driver (should not appear in the UI) | `That connection type isn't built in — go back and pick a provider.` |
| save blocked without a passing probe | `Test the connection first — a route can only be saved after it works.` |

### 7.2 Behavior

- A failed probe **keeps every field**; nothing is lost. The fix block is inline, focused, and points precisely at the field to change.
- The 401 case reuses the §5 key teaching block (collapsed), steering to "paste a new key" (primary path) or "fix the env var and restart" (headless path) — the user stays in the form and does not need a restart.
- On success the catalog refreshes: the model picker re-fills from the probe's `models` list (no backend change needed — it already rides on `detail["models"]`). Catalog refresh also has a **Load model list** button: fetches that endpoint's model list without a probe; while loading the button shows a spinner and is disabled (§3.2, §3.4). The cleaner long-term option is D2.
- The old raw renderings (`reachable — {"error":...}` JSON, the "unreachable" badge) are replaced everywhere, including the ⑧ role-card probe badges → `connected` / `needs attention`, with the same plain-language message on the card.

---

## 8. Decisions to make (orchestrator / product)

> D1 finalized: **SecretStore file backend** — an API key can be pasted once, written to `~/.mnemoseed/secrets/<role>.key` (POSIX file 0600, dir 0700; Windows user-profile ACL); the settings DB stays the primary store with a reserved scope column; the config stores only a reference `secrets:mnemoseed/dream/<role>`; changes apply **without a restart** (generation-bump re-resolve). Env-var names remain supported (headless/CI). SaaS key hosting is deferred to the TEE milestone.

| # | Question | Options | Recommendation |
|---|---|---|---|
| D1 | key handling: pure env vars (current) vs "paste a key and MnemoSeed writes it into your env var / OS credential store"? | (a) pure env vars + teaching (current); (b) write a `~/.mnemoseed/.env` or an OS keychain entry from the console; (c) full OS credential-store integration | **finalized** — SecretStore file backend (paste once, stored locally, no restart) + env-var fallback (headless/CI); see note above. Industry precedents (verified): Codex CLI `~/.codex/auth.json`, gh keychain-with-file-fallback + `GH_TOKEN`, Docker `config.json` base64, 12-factor env. |
| D2 | live model catalog: reuse the probe `detail["models"]` (zero backend change) vs a new `GET /api/v1/llm/catalog?driver=&base_url=` endpoint? | (a) probe only; (b) dedicated catalog endpoint | **(a) this round** — ship the UX first; (b) as a post-launch polish (the probe is on-demand, so the catalog is not visible until the user tests — acceptable on the happy path). |
| D3 | native drivers: nothing to build — Fireworks/OpenRouter = openai_compatible, Anthropic native, Ollama native. Confirm? | — | **Confirmed; no driver work.** |
| D4 | wizard role scope: deep_reflection only (current) vs an "also apply to short_increment" checkbox (writes both roles) vs having the wizard configure all two? | (a) current; (b) + share checkbox; (c) full two-role wizard | **(b)** — one checkbox, one line of copy, covers the common "one key, one provider" user without dragging TTFM past 3 minutes; each role can later be changed independently in ⑧ (including switching to Ollama, see D10). |
| D5 | hide the `stub` driver from the wizard/console dropdowns (keep it in the API and config for testing)? | (a) hide; (b) keep | **(a) hide** — a test seam is not a user path. |
| D6 | MiniMax/Kimi egress path (FR-6.9): implement, or delete the promise? | (a) add a "China region" note on the "Other OpenAI-compatible" card with a data-egress notice; (b) delete from docs before implementing | **(a)** — zero code, one notice, restores a documented promise; the notice states "your memories leave the country to the provider's servers". |
| D7 | `onboard` CLI LLM step: extend it to collect base_url + api_key_env + provider selection? | (a) yes, mirror the component; (b) keep driver+model | **(a)** — otherwise the CLI cannot configure cloud providers today (§2.5). |
| D8 | probe error classification: frontend parses strings (current) vs a structured `error.kind` from the backend? | (a) frontend parsing; (b) backend kinds | **(a) now, (b) later** — the three or four error classes in §7.1 are stable and match the existing strings. |
| D9 | verify model ids: current placeholders / `default_config_toml` samples (`claude-opus-5`, `claude-sonnet-5`) are unverified. | (a) take ids from the catalog only, ship no unverified id; (b) verify against provider docs | **(a)+(b)**: replace unverified ids with catalog-verified ids at release; the Fireworks defaults (verified in config comments) stay unchanged. |
| D10 | role model (**finalized**): the dream engine has exactly two roles `deep_reflection` / `short_increment`, each able to **independently point at any provider** (Fireworks / OpenRouter / Anthropic / Ollama / other OpenAI-compatible) — a cloud + local mix is a fully legitimate configuration, never blocked or shamed. `local_track` is no longer a role; it survives only as a **deprecated config key** (accepted + warning, no engine consumer, no role card). Offline = **derived truth**: the "fully offline" badge shows when all configured roles resolve to the local ollama driver; hidden if any cloud role exists; no offline switch. | — | **finalized** — §2.1, §3.1, §4, §9, §10 already rewritten accordingly; doc sync in §8.1. |
| D11 | permission scope (**finalized**): model routing (and engine settings) are **system-scoped**, configurable only at owner/admin level — self-hosted = the owner account (the sole account in the open-source single-user build); commercial multi-user license = admin level, applies to all users; SaaS = the cloud Admin Plane (system-operator level), applies to all users. **Not** a per-user personal setting. | — | **finalized** — the ⑧ permission model is §10.1.1. |

### 8.1 Doc-sync checklist (implemented in one engineering batch)

> With the role model and permission scope finalized, the following docs are synchronized in a single engineering batch (one line each, all pointing at the corresponding section of this spec):

- **PRD-02 FR-2.7**: the offline track is rewritten as "both roles point at Ollama" — the wording of "offline track" as a separate third option is removed.
- **PRD-02 FR-2.14**: `LLM_ROLES` = two roles (`deep_reflection` / `short_increment`); `local_track` is demoted to a **deprecated config key** (accepted + warning, no engine consumer).
- **design/02 §6 defaults**: the `local_track` default route is deleted; only the two per-role defaults remain.
- **PRD-06 FR-6.9**: offline option ③ is folded into the provider cards (Ollama card + quality hint); the "guided offline track" sequence is deleted.
- **PRD-07 G-AC2**: "all three roles" → "all two roles" (`deep_reflection` / `short_increment`).
- **design/07 §8**: the dream routing table goes from three rows to two rows (the `local_track` row is deleted).
- **CLI `llm` help text**: role descriptions become two roles; the `local_track` example is removed.
- **onboard LLM step copy**: aligned with §10.3 / §11.3 — provider selection includes the Ollama quality hint and a two-role explanation.

---

## 9. Empty / error / loading states + accessibility

### 9.1 Per-surface states

| State | Behavior |
|---|---|
| loading (wizard/⑧) | reuse the existing `Loading…` skeleton with role/provider placeholders; never leave a blank page. |
| `oauth-availability` fetch fails | the OAuth panel hides; the provider cards + BYOK remain usable (the `showDreamSetup` catch already does this). |
| no provider detected (wizard) | the OAuth panel shows one greyed line: "No Codex/Grok login detected on this machine — you can still use an API key below." |
| probe succeeds but catalog empty | the model picker falls back to curated suggestions + free input; hint: "The catalog returned no models — pick from the suggestions or type the exact model id." |
| model list loading (Load model list) | the button shows a spinner and is disabled; on success the picker fills with that endpoint's live catalog; on failure it falls back to suggestions with a message. |
| pasted key saved | a chip shows `key saved — ****1234` (masked tail) + `[delete]`; deleting returns to the empty paste state; the change applies immediately, no restart. |
| daemon down / fetch failure (⑧) | reuse the existing error panel + Retry, unchanged. |
| save → 409 (test-required race) | map to the plain-language "Test the connection first", never the raw 409 detail. |
| route card with no explicit config (⑧) | a "defaults" badge replaces the empty block — the effective base URL / key chain / model are visible on the card, not only while editing. |
| fully-offline derived badge (⑧ page header / route cards) | shows `fully offline — nothing leaves this machine` only when **all** configured roles resolve to the local ollama driver; **does not** show if any cloud role exists (mixes never show it — no false privacy feel). No offline switch. |

### 9.2 Accessibility

- Provider cards are radio inputs with visible labels (not bare click-divs); full keyboard support; `aria-checked` + focus ring.
- All `output.feedback` regions stay `aria-live="polite"`; probe/save feedback is read aloud.
- Success/failure is never color-only: icon + text + message.
- All form controls have a real `<label for>` pairing (`app.js` already uses this pattern); the new morphing form keeps it — hidden fields are still labeled fields when they appear.
- Collapsible teaching blocks use native `<details>`/`<summary>` (focus/keyboard for free).
- Env-var command blocks render as `<pre>` with a copy button (`navigator.clipboard`, already used for token copying), `aria-label` includes the variable name.
- Contrast and reduced-motion follow the `styles.css` conventions; no new animations.

---

## 10. Surface mapping (one pattern, three renderings)

### 10.1 console ⑧ Models & Routing (full editor)

```
┌─ models & routing ───────────────────────────────────────────────────┐
│  per-role dream models: what each role does, and which model serves   │
│  it. Key values never appear here — only env-var names or a masked    │
│  key tail (****1234).                                                  │
│                                                                       │
│  fully-offline badge (derived — shown only when BOTH roles resolve    │
│  to local ollama;  ⇢ (this page is a cloud+local mix Fireworks+Ollama,│
│  so it does not show):                                                │
│  ◉ fully offline — nothing leaves this machine                        │
│                                                                       │
│  host logins: [codex: logged in] [grok: not detected]                 │
│  OAuth dual path: [Use Codex login] (card) · or [Paste a token        │
│  instead]; expired → "log in again first" + codex login; absent →     │
│  disabled card "log in to the <provider> CLI first"                   │
│                                                                       │
│  ┌─ deep_reflection ── the careful model ──────────────────────────┐  │
│  │  connected · Fireworks · accounts/fireworks/models/kimi-k3      │  │
│  │  key: MNEMOSEED_DEEP_REFLECTION_API_KEY → FIREWORKS_API_KEY     │  │
│  │  base URL: https://api.fireworks.ai/inference/v1  ·  max 2048   │  │
│  │  [test connection] [edit route]                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│  ┌─ short_increment ── the quick model ────────────────────────────┐  │
│  │  connected · Ollama · llama3.1:8b                               │  │
│  │  quality note: lower synthesis quality than cloud models —      │  │
│  │  you accept this for privacy or cost.                           │  │
│  │  [test connection] [edit route]                                 │  │
│  └─────────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  [edit route] expands the provider-first form (§3) inline:           │
│    role card → provider picker (cards apply to whichever role is      │
│    being edited) → morphing form → test → save (armed only after      │
│    a passing probe of the exact values)                               │
│                                                                       │
│  foot: Model routing is system-scoped — set by the owner/admin and    │
│        applies to every user.                                         │
└───────────────────────────────────────────────────────────────────────┘
```

#### 10.1.1 Permission model (system-scoped, not per-user)

Model routing and engine settings are **system-scoped**, configurable only at owner/admin level (§8 D11):

- **Self-hosted** (open-source single-user build): the owner account is the only account; the owner is the configurator.
- **Commercial multi-user license**: admin level, applies to all users — ordinary users see no routing and cannot change settings.
- **SaaS**: configured at the cloud Admin Plane (system-operator level), applies to all users; **no** per-user personal routing settings are provided.

The ⑧ page footer always shows one line: `Model routing is system-scoped — set by the owner/admin and applies to every user.` When an ordinary user opens ⑧ the whole page is read-only (the §9.2 keyboard/focus discipline applies too — no edit entry point is itself the read-only signal).

### 10.2 First-run wizard (after owner creation)

```
┌─ dream model ───────────────────────────────────────────────────────┐
│  "Pick the model that distills your sessions into long-term memory. │
│   One model gets you started — change any role later in Models."    │
│                                                                      │
│  step 1  provider   step 2  key + model   step 3  test & save       │
│                                                                      │
│  ○ Fireworks (recommended)   ○ OpenRouter   ○ Anthropic   ○ Ollama  │
│  ○ Another OpenAI-compatible endpoint                                │
│                                                                      │
│  ── or reuse a login on this computer ──                             │
│  [Codex: logged in] → [Use Codex login] · or [Paste a token instead] │
│  (expired → "log in again first" + codex login; absent → disabled    │
│  card)                                                               │
│                                                                      │
│  [continue]                                            [skip for now]│
└───────────────────────────────────────────────────────────────────────┘
```
The provider cards apply equally to both roles (§3.1). The share checkbox on step three (D4 finalized):

```
┌─ step 3 ─ test & save ─────────────────────────────────────────────┐
│  provider: Fireworks                    [x] also apply to           │
│  Testing connection to Fireworks…         short_increment           │
│  ✓ Connected — key in FIREWORKS_API_KEY works.            [save]    │
└───────────────────────────────────────────────────────────────────────┘
```

Step two morphs per provider (§5), step three runs the probe (§7) then saves. The wizard configures `deep_reflection` by default; the **"also apply to short_increment" share checkbox** is on the same screen — checking it writes the same provider + same key into `short_increment` as well. If both roles pick Ollama, the share checkbox applies equally, and the "fully offline" badge appears after saving (§9). "skip for now" keeps a capture-only daemon — stated explicitly, not left for the user to discover.

### 10.3 CLI `mnemoseed onboard` LLM step

Mirrors the same steps as numbered prompts, adapted to a terminal (no radio UI, no collapsibles — flat text, commands printed verbatim one per line):

```
[llm]
  Pick the model that distills your sessions into long-term memory.
  One model gets you started; change any role later with `mnemoseed llm set`.
  1) Fireworks (recommended)   3) Anthropic       5) other OpenAI-compatible
  2) OpenRouter                4) Ollama on this computer
  provider [1]: 1
  Create a key at https://app.fireworks.ai/settings/users/api-keys
  Paste the key once — stored locally under ~/.mnemoseed/secrets, never
  shown again (masked tail ****1234, deletable). Or set an env var for
  headless/CI use (a NEW env value still needs a daemon restart):
    Windows:  setx FIREWORKS_API_KEY "your-key"
    macOS/Linux: export FIREWORKS_API_KEY="your-key"   # add to ~/.zshrc
  api key (paste once) or env var name [FIREWORKS_API_KEY]:
  key saved — ****1234
  model [accounts/fireworks/models/kimi-k3]:            ← verified default
  (if 4 Ollama is chosen, print a quality-hint line first, then run the test:)
  ⚠ Ollama chosen for this role — lower synthesis quality than cloud
    models; you accept this for privacy or cost.
  testing connection to Fireworks…
  connected — key works. saving…
  also apply to short_increment? [y/N]: y        ← CLI form of the D4 share checkbox
  ✓ dream model configured (openai_compatible/accounts/fireworks/models/kimi-k3)
  (skip: entering no model → "capture-only daemon (dreaming disabled)"; 
   --skip llm and --llm-driver/--llm-model scripted flags unchanged)
```

The CLI and the console share the **same** backend (first `POST /api/v1/llm/test`, then `/api/v1/llm/routes/deep_reflection`), the same provider table, the same plain-language probe copy, and the same default-value source. Terms are defined inline on first appearance ("env var = a named value on this computer that MnemoSeed reads the key from").

---

## UI Copy (English — product surface language)

The following are the product UI string assets. Keep them in English, verbatim.

### 11.1 Wizard

- Title: `dream model`
- Intro: `Pick the model that distills your sessions into long-term memory. One model gets
  you started — you can change any role later in Models.`
- Provider group: `Which provider do you use?`
- Fireworks card: label `Fireworks`, blurb `Recommended starting point — MnemoSeed's default
  models run here.`
- OpenRouter card: label `OpenRouter`, blurb `One API key, hundreds of models from many labs.`
- Anthropic card: label `Anthropic (Claude)`, blurb `Requires an Anthropic API key from
  platform.claude.com.`
- Ollama card: label `Ollama on this computer`, blurb `Free and offline. Runs entirely on
  this machine; lower synthesis quality.`
- Ollama quality hint (shown when the role being configured picks Ollama): `Lower
  synthesis quality than cloud models — you accept this for privacy or cost.`
- Share checkbox (D4): label `also apply to short_increment`, note `Uses the same provider
  and key for the quick consolidation model.`
- Other card: label `Another OpenAI-compatible API`, blurb `Point at any other endpoint
  that speaks the OpenAI chat API.`
- OAuth panel header: `Or reuse a login already on this computer`
- OAuth hint: `MnemoSeed uses that login's access — you don't paste a key. No key value is
  read, sent, or stored.`
- OAuth live: `Codex login found — sign in is current.` / button `Use Codex login`
- OAuth expired: `Codex login found but expired — log in again first, then return here.` (card
  disabled) / command line `codex login` (copy-to-clipboard)
- OAuth absent: `Log in to the Codex CLI first, then come back.` (card disabled;
  per-provider: `<provider>` = Codex / Grok)
- Paste-token affordance (second path): `or paste a token instead` → official doc links:
  `How to create a Codex token` → https://developers.openai.com/codex/auth · `How to create
  an xAI API key` → https://docs.x.ai/developers/quickstart
- OAuth blocked-save: `This route can't be saved until a login is available — log in to the
  Codex CLI first, or paste a token instead.`
- OAuth banner (after selection): `Using the Codex login on this machine — no key needed.
  It refreshes itself while you're signed in.`
- Key label: `api key` — `paste once (stored locally, never shown again)` / `or an env var
  name for headless/CI use`
- Key teaching intro: `Paste your key once — MnemoSeed stores it locally under
  ~/.mnemoseed/secrets and never shows it again (only the masked tail ****1234). For
  headless/CI you can use an env var instead.`
- Key saved chip: `key saved — ****1234` (button `delete`)
- Key saved note: `Stored under ~/.mnemoseed/secrets. Not shown again; only this masked
  tail. Deletable any time.`
- Delete key confirm: `Delete this key? Routes using it fail until a new key is set.`
- Key 401 fix: `Fireworks rejected the key in FIREWORKS_API_KEY — it's missing, wrong, or
  expired. Check it at <provider key URL>, then paste a new key here.` (per-provider
  substitution; the env-var path appends `or fix the env var and restart for headless/CI.`)
- Load model list button: `Load model list`
- Endpoint label: `endpoint` / advanced header: `Advanced: endpoint`
- Endpoint reset: `reset to Fireworks default`
- Model label: `model`
- Model placeholder: `type or pick a model`
- Catalog empty: `No models listed — pick a suggestion or type the exact model id.`
- Probe in-flight: `Testing connection to Fireworks…`
- Probe ok: `Connected — key in FIREWORKS_API_KEY works.`
- Probe saved: `dream model configured: deep reflection → <model>` (shared: `deep
  reflection + short increment → <model>`)
- Skip button: `Skip for now — capture-only (dreaming stays off)`
- Skip confirm: `Skipped — MnemoSeed keeps capturing sessions, dreaming stays off until a
  model is configured. You can set one any time in Models.`

### 11.2 console ⑧

- Page title: `models & routing`
- Page note: `What each role does, and which model serves it. Key values never appear here —
  only env-var names or a masked key tail (****1234).`
- Role subtitles (§4) — two roles only: `deep_reflection` / `short_increment`.
- Offline badge (derived): `fully offline — nothing leaves this machine` (meaning: shown
  only when all configured roles resolve to local ollama; hidden if any cloud role exists —
  derived truth, no switch)
- Card quality note (any role on Ollama): `lower synthesis quality than cloud models — you
  accept this for privacy or cost.`
- Permission footnote: `Model routing is system-scoped — set by the owner/admin and applies
  to every user.` (§10.1.1 permission model)
- Card probe: `connected` / `needs attention` (with the plain message from §7, not raw JSON)
- Card key line: `key: MNEMOSEED_DEEP_REFLECTION_API_KEY → FIREWORKS_API_KEY`
- Card base: `base URL: https://api.fireworks.ai/inference/v1`
- Defaults chip (no explicit config): `defaults`
- Buttons: `Test connection` / `Edit route` / `Cancel edit` / `Save route` (disabled until a
  passing probe of the exact values)
- Save gate error: `Test the connection first — a route can only be saved after a passing
  probe of these exact values.`
- 409 mapped: same as save gate error.
- Editor header: `Edit route — <role>` (deep_reflection / short_increment)
- Provider group in editor: `Which provider?` (same cards, minus "recommended")
- max tokens label: `max tokens` (advanced), note: `blank = role default`
- Saved banner: `route deep_reflection saved — config version <v> (audited)`
- Restart note (env-fallback only): `Note for headless/CI: a NEW env-var value is picked up
  only after the daemon restarts. Pasted keys apply immediately.`
- OAuth line: `host logins: codex — logged in · grok — not detected`
- OAuth card available (⑧ editor): `Use Codex login` (selectable card)
- OAuth card expired (⑧ editor): `Codex login expired — log in again first` + command
  `codex login` (card disabled)
- OAuth card absent (⑧ editor): `Log in to the Codex CLI first` (card disabled;
  per-provider `<provider>`)
- Paste-token affordance (⑧ editor): `Paste a token instead` (+ official doc links, §11.1)
- OAuth blocked-save (⑧ editor): `This route can't be saved until a login is available —
  log in to the Codex CLI first, or paste a token instead.`
- Key saved chip (⑧ editor): `key saved — ****1234` + `[delete]`
- Load model list button (⑧ editor): `Load model list`

### 11.3 CLI (onboard LLM step + llm set)

- Step header: `[llm]`
- Intro: `Pick the model that distills your sessions into long-term memory. One model gets
  you started; change any role later with 'mnemoseed llm set'.`
- Provider prompt: `provider [1]: ` (list printed as §10.3)
- Key URL line: `Create a key at <url>`
- Key teaching (printed once, §10.3 block): paste-once path + env-var fallback
- `api key (paste once, stored locally) or env var name [FIREWORKS_API_KEY]: `
- `key saved — ****1234`
- `model [accounts/fireworks/models/kimi-k3]: `
- `testing connection to Fireworks…`
- `connected — key works. saving…`
- Share prompt (D4, CLI form): `also apply to short_increment? [y/N]: `
- Ollama quality line (printed first when provider = Ollama): `Ollama chosen for this role — lower
  synthesis quality than cloud models; you accept this for privacy or cost.`
- Success: `✓ dream model configured (<driver>/<model>)`
- Fail 401: `error: Fireworks rejected the key — paste a new one, or fix the env var (then
  restart for headless/CI), and re-run onboard (it resumes here).`
- OAuth expired (CLI): `Codex login expired — run 'codex login' first, then re-run onboard
  (it resumes here).`
- OAuth absent (CLI): `No Codex login detected — log in to the Codex CLI first, or paste a
  token instead.`
- Ollama fail: `error: can't reach Ollama at http://localhost:11434 — is it running?
  Install from ollama.com and pull a model (ollama pull llama3.1:8b).`
- Skip: `skipping the LLM wizard: the daemon stays capture-only (dreaming disabled until a
  model is configured)`
- `mnemoseed llm set --help`: driver help updated to `provider (or --provider codex|grok
  for a host login)`; add `--provider-card`? No — keep parity, add examples in help text.
  Help text names the two roles (`deep_reflection` / `short_increment`) only — no `local_track`
  example or role.

---

## 12. Provider fact verification (archive)

- **Fireworks**: quickstart (key-creation URL `app.fireworks.ai/settings/users/api-keys`, `setx`/`export FIREWORKS_API_KEY`, OpenAI-compatible base `https://api.fireworks.ai/inference/v1`, `GET /models` implied by the OpenAI SDK path) — https://docs.fireworks.ai/getting-started/quickstart
- **OpenRouter**: quickstart (base `https://openrouter.ai/api/v1`, `OPENROUTER_API_KEY`, catalog `GET /api/v1/models`, OpenAI-compatible) — https://openrouter.ai/docs/quickstart
- **Anthropic**: API overview (base `https://api.anthropic.com`, Messages `POST /v1/messages`, `x-api-key` + `anthropic-version`, keys from `platform.claude.com/settings/keys`, models `GET /v1/models`) — https://platform.claude.com/docs/en/api/overview
- **Ollama**: API reference (`POST /api/chat` stream=false, `GET /api/tags`, no auth, `model:tag` naming) — https://github.com/ollama/ollama/blob/main/docs/api.md
- **Codex / OpenAI auth** (official doc link for paste-a-token): `~/.codex/auth.json` plaintext credential cache, `codex login` and `codex login --with-api-key`, API keys created at platform.openai.com/api-keys — https://developers.openai.com/codex/auth
- **xAI / Grok** (official doc link for paste-a-token): quickstart (`XAI_API_KEY`, base `https://api.x.ai/v1`, API Keys page https://console.x.ai/team/default/api-keys — the console requires sign-in, anonymous fetch 403, marked "entry page") — https://docs.x.ai/developers/quickstart
- **OpenRouter models page** (§3.4 pattern reference) — https://openrouter.ai/models
- **Cursor models page** (§3.4 pattern reference) — https://cursor.com/docs/models

### 12.1 Verification notes (grounding for this spec)

- Code reading: `console/static/app.js` (wizard + ⑧ render/edit/probe; line numbers referenced inline above), `config.py` `DEFAULT_LLM_ROUTES`, `llm/admin.py` + `admin_routes.py` (explicit payload only, probe signature, 409-gated saves), `llm/routing.py` (env resolution + instance caching), `llm/drivers/*` (the five drivers; the catalog rides in the probe detail), `configwrite/service.py` (env-name validation), `onboard/service.py` (LLM step), `cli.py` (`llm status/set`, `onboard`).
- Local empirical test: with a temporary `MNEMOSEED_HOME` on a spare port (embedded preset, `127.0.0.1:18764`), walked through: owner setup → login → `/api/v1/llm/routes` (confirmed explicit-payload-only) → `/api/v1/llm/oauth-availability` (both local logins detected but expired) → `/api/v1/llm/test` probe shapes (no-key Fireworks 401; offline Ollama connection refused; unknown driver; save-without-probe → 409). Observed `stub` in the drivers directory and confirmed defaults are invisible in the live payload.
- Unverified: the current model placeholders `claude-opus-5` / config samples `claude-sonnet-5` (D9), and the `gpt-5.6-codex` placeholder in OAuth mode. The Grok host-login re-login command (varies by installed CLI) is not verified in the official docs — the UI falls back to "log in to the <provider> CLI first" plus paste-a-token. console.x.ai requires sign-in (anonymous 403); its API Keys page URL is taken from the official docs' pointer.