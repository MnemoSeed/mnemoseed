"use strict";
/* MnemoSeed console SPA (PRD-07 T2).
 *
 * Dependency-free vanilla JS — no build step, no CDN, works fully offline.
 * The MnemoSeed frontend talks to the daemon's own /api/v1 REST surface, so
 * every observable behaviour is also covered at the API-contract level by the
 * console test suite. This module keeps the dynamic rendering in pure helpers
 * (a value in -> an HTML string out) so the heavy lifting stays trivial to
 * reason about; the fetch glue below them is the only async surface.
 *
 * Error policy: no fetch is ever left unhandled. Every load sets a loading
 * state first and swaps in an inline error panel (with a retry button) on
 * failure — a degraded daemon renders a readable error, never a blank page.
 */

// ---------------------------------------------------------------- localStorage guard
const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch (_err) {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (_err) {
      /* storage unavailable; degrade silently */
    }
  },
};

// ---------------------------------------------------------------- pure helpers
const esc = (value) =>
  String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

function fmtNum(value) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString();
}

function fmtEpoch(timestamp) {
  if (timestamp === null || timestamp === undefined || timestamp === "") return "—";
  const n = Number(timestamp);
  if (!Number.isFinite(n) || n <= 0) return "—";
  return new Date(n * 1000).toLocaleString(undefined, {
    dateStyle: "short",
    timeStyle: "medium",
  });
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(Number(seconds))) return "—";
  const s = Number(seconds);
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s % 60);
  return `${m}m ${String(rest).padStart(2, "0")}s`;
}

function fmtMoney(usd) {
  if (usd === null || usd === undefined || !Number.isFinite(Number(usd))) return "—";
  return `$${Number(usd).toFixed(2)}`;
}

function fmtRange(range) {
  if (!range) return "—";
  return `#${fmtNum(range.start)}→#${fmtNum(range.end)}`;
}

function truncate(text, max) {
  const s = String(text ?? "");
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

function niceLabel(value) {
  return String(value ?? "")
    .toLowerCase()
    .replace(/_/g, " ");
}

function datetimeToEpoch(value) {
  if (!value) return null;
  const millis = new Date(value).getTime();
  return Number.isFinite(millis) ? millis / 1000 : null;
}

function decayMeter(weight) {
  const raw = Number(weight);
  const w = Number.isFinite(raw) ? Math.min(1, Math.max(0, raw)) : 0;
  const cls = w >= 0.66 ? "meter-strong" : w >= 0.33 ? "" : "meter-weak";
  return `<span class="meter ${cls}" title="decay_weight ${w.toFixed(3)}"><span style="width:${(w * 100).toFixed(1)}%"></span></span> ${esc(w.toFixed(3))}`;
}

function flagValue(value) {
  if (value === true) return '<span class="badge badge-ok">yes</span>';
  if (value === false) return '<span class="badge">no</span>';
  return '<span class="badge badge-err">—</span>';
}

function flagBadge(label, on, cls) {
  return on ? `<span class="badge badge-${cls}">${esc(label)}</span>` : "";
}

function tile(value, label, cls) {
  return `<div class="tile"><div class="tile-value ${cls ? ` ${cls}` : ""}">${value}</div><div class="tile-label">${esc(label)}</div></div>`;
}

function kvList(pairs) {
  return pairs
    .map(
      ([label, valueHtml]) =>
        `<div class="kv"><span class="kv-label">${esc(label)}</span><span class="kv-value">${valueHtml}</span></div>`,
    )
    .join("");
}

function badgeList(values) {
  return (values || []).length
    ? values.map((v) => `<span class="badge">${esc(v)}</span>`).join(" ")
    : '<span class="dim">—</span>';
}

function errorInline(message) {
  return `<p class="error-inline">${esc(message)}</p>`;
}

function errorPanel(message) {
  return `<div class="error-panel"><p><strong>Something went wrong</strong></p><p class="error-detail">${esc(message)}</p><button class="btn" data-act="retry">Retry</button></div>`;
}

function emptyPanel(message) {
  return `<div class="empty-panel">${esc(message)}</div>`;
}

function detailCell(detail) {
  if (detail === null || detail === undefined || detail === "") return '<span class="dim">—</span>';
  if (typeof detail === "object") return `<code class="mono">${esc(JSON.stringify(detail))}</code>`;
  return esc(detail);
}

// Minimal LCS word diff for adjacent version statements (M1 reduction of the
// design's "any-two-versions diff view" — the raw versions come from /api/v1).
function diffWords(before, after) {
  const a = String(before ?? "").split(/\s+/).filter(Boolean);
  const b = String(after ?? "").split(/\s+/).filter(Boolean);
  if (a.join(" ") === b.join(" ")) return esc(b.join(" "));
  const n = a.length;
  const m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i -= 1) {
    for (let j = m - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push(esc(a[i]));
      i += 1;
      j += 1;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push(`<del>${esc(a[i])}</del>`);
      i += 1;
    } else {
      out.push(`<ins>${esc(b[j])}</ins>`);
      j += 1;
    }
  }
  while (i < n) {
    out.push(`<del>${esc(a[i])}</del>`);
    i += 1;
  }
  while (j < m) {
    out.push(`<ins>${esc(b[j])}</ins>`);
    j += 1;
  }
  return out.join(" ");
}

// ---------------------------------------------------------------- constants
const REFRESH_MS = 15000;
const NODE_TYPES = [
  "USER",
  "HABIT",
  "PREFERENCE",
  "ANIMA",
  "INTENTION",
  "CONSTRAINT",
  "EPISODE",
  "SKILL_SEQUENCE",
  "DECISION",
  "PROJECT",
  "TOOL",
];

// Filter-bar model: which fields exist per browser tab, and how their raw
// control value maps to /api/v1 query parameters. kind values:
//   datetime -> local datetime-local string -> epoch float
//   text     -> free text (entity splits on commas into repeated params)
//   tier     -> 1|2|3
//   decay    -> 0..1 float
//   node-type-> NODE_TYPES enum value
//   check    -> true sends the flag filter, false omits it (server default)
const FILTER_MODEL = {
  chunks: [
    { name: "time_after", label: "Ingested after", kind: "datetime" },
    { name: "time_before", label: "Ingested before", kind: "datetime" },
    { name: "project", label: "Project", kind: "text" },
    { name: "host", label: "Host", kind: "text" },
    { name: "entity", label: "Entities (comma)", kind: "text" },
    { name: "tier", label: "Tier", kind: "tier" },
    { name: "min_decay", label: "Decay min", kind: "decay" },
    { name: "max_decay", label: "Decay max", kind: "decay" },
    { name: "consolidated", label: "Consolidated", kind: "check" },
    { name: "needs_reconcile", label: "Needs reconcile", kind: "check" },
  ],
  nodes: [
    { name: "updated_after", label: "Updated after", kind: "datetime" },
    { name: "updated_before", label: "Updated before", kind: "datetime" },
    { name: "node_type", label: "Node type", kind: "node-type" },
    { name: "entity", label: "Entities (comma)", kind: "text" },
    { name: "tier", label: "Tier", kind: "tier" },
    { name: "min_decay", label: "Decay min", kind: "decay" },
    { name: "max_decay", label: "Decay max", kind: "decay" },
    { name: "needs_reconcile", label: "Needs reconcile", kind: "check" },
    { name: "pending_consolidation", label: "Pending consolidation", kind: "check" },
    { name: "conflict", label: "In conflict", kind: "check" },
  ],
};

// FR-7.6 review verdicts and FR-7.7 resolution branches are closed vocab on the
// console surface (mirrors the API / glance-view). All labels stay in English.
const REVIEW_VERDICTS = [
  { value: "accept", label: "accept", cls: "ok" },
  { value: "reject", label: "reject", cls: "warn" },
  { value: "hallucination", label: "hallucination", cls: "err" },
];
const VERDICT_BADGE = {
  accept: '<span class="badge badge-ok">accept</span>',
  reject: '<span class="badge badge-warn">reject</span>',
  hallucination: '<span class="badge badge-err">hallucination</span>',
};
const CONFLICT_BRANCHES = [
  { value: "reinforce", label: "reinforce one side" },
  { value: "coexist", label: "let both coexist — scope it" },
  { value: "invalidate", label: "invalidate one side" },
  { value: "pending", label: "leave pending" },
];

// ---------------------------------------------------------------- state
const state = {
  profileId: store.get("mnemoseed.profile") || null,
  profiles: [],
  dashboard: null,
  dreamStatus: null,
  dreamRuns: null,
  browse: {
    tab: "chunks",
    filters: {},
    offset: 0,
    limit: 50,
    data: null,
  },
  review: { runId: null, data: null },
  conflicts: { data: null },
  llm: {
    routes: null,
    oauth: null,
    config: null,
    editingRole: null,
    message: null,
    probeOk: {},
    // provider id -> model list fetched by a passing probe (the editor's
    // provider-scoped datalist catalog, §7.2)
    catalog: {},
    // role -> live model text while that role's editor is open (the role
    // card's model tile reflects the picked provider, not a stale route)
    editModel: {},
    wizard: null,
  },
  profilesPage: { tokens: {} },
  settings: { config: null, versions: null, message: null },
  audit: { filters: {}, offset: 0, limit: 50, data: null },
  detailFlash: null,
  browseFlash: null,
  autoRefreshTimer: null,
};

// ---------------------------------------------------------------- identity (issue #14)
// The console is owner-only after setup: every /api/v1 call carries the profile
// token from the login view. Pre-setup the boot gate renders the setup wizard
// instead; post-setup an absent/expired token renders the login view.
const AUTH = {
  token: store.get("mnemoseed.token") || null,
  username: store.get("mnemoseed.username") || null,
};

let authViewKind = null; // "setup" | "login" | null while the page is gated

function setAuth(session) {
  AUTH.token = session ? session.token : null;
  AUTH.username = session ? session.username : null;
  store.set("mnemoseed.token", AUTH.token || "");
  store.set("mnemoseed.username", AUTH.username || "");
  renderHeaderAuth();
}

function renderHeaderAuth() {
  const authed = Boolean(AUTH.token);
  for (const id of ["auth-nav", "auth-picker"]) {
    const node = document.getElementById(id);
    if (node) node.hidden = !authed;
  }
  const identity = document.getElementById("auth-identity");
  if (identity) {
    identity.hidden = !authed;
    identity.textContent = authed ? `signed in as ${AUTH.username}` : "";
  }
  const signOut = document.getElementById("sign-out");
  if (signOut) signOut.hidden = !authed;
}

function flashAuthError(panel, message) {
  if (!panel) return;
  panel.querySelectorAll(".error-inline").forEach((node) => node.remove());
  const node = document.createElement("p");
  node.className = "error-inline";
  node.textContent = message;
  panel.prepend(node);
}

function showLogin(message) {
  authViewKind = "login";
  document.title = "MnemoSeed console — sign in";
  renderHeaderAuth();
  const view = document.getElementById("view");
  view.innerHTML = `<div class="auth-panel card">
    <h2>sign in</h2>
    <p class="toolbar-note">Setup is complete. The console is owner-only — sign in with the owner password to obtain a profile token.</p>
    ${message ? `<p class="error-inline">${esc(message)}</p>` : ""}
    <form data-auth-form="login">
      <div class="filter-grid">
        <div class="field"><label for="login-username">username</label><input type="text" id="login-username" name="username" required autocomplete="username" /></div>
        <div class="field"><label for="login-password">password</label><input type="password" id="login-password" name="password" required autocomplete="current-password" /></div>
      </div>
      <div class="toolbar"><button class="btn btn-primary" type="submit">sign in</button></div>
    </form>
  </div>`;
}

function showSetup(message) {
  authViewKind = "setup";
  document.title = "MnemoSeed console — first-run setup";
  renderHeaderAuth();
  const view = document.getElementById("view");
  view.innerHTML = `<div class="auth-panel card">
    <h2>first-run setup</h2>
    <p class="toolbar-note">No owner account exists yet. Create the single owner to finish setup — the only account this local daemon will ever have. The password is stored as an argon2 hash, never as plaintext.</p>
    ${message ? `<p class="error-inline">${esc(message)}</p>` : ""}
    <form data-auth-form="setup">
      <div class="filter-grid">
        <div class="field"><label for="setup-username">username</label><input type="text" id="setup-username" name="username" required autocomplete="username" /></div>
        <div class="field"><label for="setup-password">password</label><input type="password" id="setup-password" name="password" required minlength="8" autocomplete="new-password" /></div>
        <div class="field"><label for="setup-confirm">confirm password</label><input type="password" id="setup-confirm" name="confirm" required minlength="8" autocomplete="new-password" /></div>
      </div>
      <div class="toolbar"><button class="btn btn-primary" type="submit">create owner</button></div>
    </form>
  </div>`;
}

// Setup + login live outside the profile-token gate, so they never attach the
// bearer header (a stale token from a previous session must not reach these).
async function fetchOpen(path, options) {
  const response = await fetch(path, options || {});
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      if (body && body.detail) detail = String(body.detail);
    } catch (_err) {
      /* non-JSON error body */
    }
    throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
  }
  return response.json();
}

async function submitSetup(form) {
  const panel = form.closest(".auth-panel");
  const data = new FormData(form);
  const username = String(data.get("username") || "").trim();
  const password = String(data.get("password") || "");
  const confirm = String(data.get("confirm") || "");
  if (!username || !password) return flashAuthError(panel, "username and password are required");
  if (password.length < 8) return flashAuthError(panel, "password must be at least 8 characters");
  if (password !== confirm) return flashAuthError(panel, "passwords do not match");
  try {
    await fetchOpen("/api/v1/setup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  } catch (error) {
    return flashAuthError(panel, `setup failed: ${error.message}`);
  }
  await setupLoginAndDreamModels({ username, password });
}

async function submitLogin(form) {
  const panel = form.closest(".auth-panel");
  const data = new FormData(form);
  const username = String(data.get("username") || "").trim();
  const password = String(data.get("password") || "");
  if (!username || !password) return flashAuthError(panel, "username and password are required");
  let body;
  try {
    body = await fetchOpen("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
  } catch (error) {
    return flashAuthError(panel, `sign in failed: ${error.message}`);
  }
  setAuth({ token: body.token, username: body.username });
  state.profileId = body.profile_id || "default";
  store.set("mnemoseed.profile", state.profileId);
  authViewKind = null;
  render();
}

// First-run dream model step (FR-6.9): after the owner is created, the wizard
// offers host-OAuth pickup (Codex / Grok), a bring-your-own-key route, or skip.
// Keys are referenced by env-var NAME only — no token value ever crosses this
// page or is written by the daemon.
async function setupLoginAndDreamModels(credentials) {
  let body;
  try {
    body = await fetchOpen("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(credentials),
    });
  } catch (error) {
    showLogin(`auto sign-in failed: ${error.message}`);
    return;
  }
  setAuth({ token: body.token, username: body.username });
  state.profileId = body.profile_id || "default";
  store.set("mnemoseed.profile", state.profileId);
  await showDreamSetup();
}

// ---------------------------------------------------------------- models & routing (models-routing-ux.md §11)
// The five provider cards shared by the first-run wizard and the ⑧ editor.
// Curated model ids were verified before shipping (D9): Fireworks from the
// live catalog, OpenRouter from the keyless openrouter.ai/api/v1/models fetch,
// Anthropic from the official models overview, Ollama from the library tags —
// never publish an unverified id. Key values never appear on this page: only
// env-var NAMES.
const LLM_PROVIDERS = [
  {
    id: "fireworks",
    label: "Fireworks (recommended)",
    driver: "openai_compatible",
    baseUrl: "https://api.fireworks.ai/inference/v1",
    keyEnv: "FIREWORKS_API_KEY",
    keyUrl: "https://app.fireworks.ai/settings/users/api-keys",
    note: "Recommended starting point — MnemoSeed's default models run here.",
    models: [
      "accounts/fireworks/models/kimi-k3",
      "accounts/fireworks/models/deepseek-v4-flash-0731",
    ],
    defaults: {
      deep_reflection: "accounts/fireworks/models/kimi-k3",
      short_increment: "accounts/fireworks/models/deepseek-v4-flash-0731",
    },
  },
  {
    id: "openrouter",
    label: "OpenRouter",
    driver: "openai_compatible",
    baseUrl: "https://openrouter.ai/api/v1",
    keyEnv: "OPENROUTER_API_KEY",
    keyUrl: "https://openrouter.ai/settings/keys",
    note: "One API key, hundreds of models from many labs.",
    models: [
      "deepseek/deepseek-v4-flash",
      "moonshotai/kimi-k3",
      "anthropic/claude-opus-5",
      "qwen/qwen3-coder-plus",
    ],
    defaults: {
      deep_reflection: "moonshotai/kimi-k3",
      short_increment: "deepseek/deepseek-v4-flash",
    },
  },
  {
    id: "anthropic",
    label: "Anthropic (Claude)",
    driver: "anthropic",
    baseUrl: "https://api.anthropic.com",
    keyEnv: "ANTHROPIC_API_KEY",
    keyUrl: "https://platform.claude.com/settings/keys",
    note: "Requires an Anthropic API key from platform.claude.com.",
    models: ["claude-opus-5", "claude-sonnet-5"],
    defaults: {
      deep_reflection: "claude-opus-5",
      short_increment: "claude-sonnet-5",
    },
  },
  {
    id: "ollama",
    label: "Ollama on this computer",
    driver: "ollama",
    baseUrl: "http://localhost:11434",
    keyEnv: "",
    note: "Free and offline. Runs entirely on this machine; lower synthesis quality.",
    models: ["llama3.1:8b", "qwen3:8b", "deepseek-r1:8b"],
    defaults: {
      deep_reflection: "deepseek-r1:8b",
      short_increment: "llama3.1:8b",
    },
  },
  {
    id: "other",
    label: "Another OpenAI-compatible API",
    driver: "openai_compatible",
    baseUrl: "",
    keyEnv: "",
    note: "Point at any other endpoint that speaks the OpenAI chat API.",
  },
];

// The env-var NAME each dream role falls back to when no provider default is in
// play (only names — the values live in the daemon's own environment).
const LLM_ROLE_KEY_ENV = {
  deep_reflection: "MNEMOSEED_DEEP_REFLECTION_API_KEY",
  short_increment: "MNEMOSEED_SHORT_INCREMENT_API_KEY",
};

const LLM_ROLE_SUBTITLES = {
  deep_reflection:
    "The careful model. Reads your recent sessions and writes the distilled facts into long-term memory. Use the strongest model you can afford here.",
  short_increment:
    "The quick model. Handles the frequent small consolidation passes. Use a fast, low-cost model.",
};

function llmProviderById(id) {
  return LLM_PROVIDERS.find((provider) => provider.id === id) || null;
}

function llmProviderFor(driver, providerName) {
  if (providerName) {
    const byName = llmProviderById(providerName);
    if (byName) return byName;
  }
  return (
    LLM_PROVIDERS.find((provider) => provider.driver === driver && provider.id !== "other") || null
  );
}

// Curated model ids for a provider (datalist options before any probe).
function llmCuratedModels(provider) {
  return provider && Array.isArray(provider.models) ? provider.models.slice() : [];
}

// The role-appropriate curated default a provider card re-seeds the model
// field with when it is picked (deep_reflection gets the strongest id,
// short_increment the fast/cheap one).
function llmRoleDefaultModel(provider, role) {
  if (!provider || !provider.defaults || !provider.defaults[role]) return "";
  return provider.defaults[role];
}

// Host-login CLI sign-in commands, verified against the providers' official
// docs: Codex — developers.openai.com/codex/auth ("Run `codex login`, then
// complete the browser flow"); Grok Build — docs.x.ai/build/cli/reference
// ("grok login — Sign in").
const LLM_OAUTH_LOGIN_CMD = {
  codex: "codex login",
  grok: "grok login",
};

// Official docs for each host-login provider's token / API-key sign-in (the
// paste-a-token path). Codex auth page verified; the xAI console key page is
// login-walled and unverifiable, so the verified docs root is linked instead.
const LLM_OAUTH_TOKEN_DOCS = {
  codex: "https://developers.openai.com/codex/auth",
  grok: "https://docs.x.ai/",
};

function llmOauthEntry(provider) {
  if (!provider) return null;
  return (state.llm.oauth && state.llm.oauth.providers || []).find(
    (entry) => entry.provider === provider,
  ) || null;
}

function llmOauthLive(provider) {
  const entry = llmOauthEntry(provider);
  return Boolean(entry && entry.present === true && entry.expired !== true);
}

// The per-route block copy shown when a route's host login is expired or
// absent (JH: only that route is blocked until availability returns).
function llmOauthBlockMessage(provider) {
  const name = cap(provider || "");
  const cmd = LLM_OAUTH_LOGIN_CMD[provider] || `${provider} login`;
  const entry = llmOauthEntry(provider);
  if (entry && entry.present === true) return `login expired — run ${cmd} first`;
  return `no local ${name} CLI login detected — log in first (${cmd})`;
}

function llmEffectiveBaseUrl(role) {
  // The daemon may soon carry an `effective` field per role; until then the
  // explicit route wins, then the provider default (defensive).
  if (role.effective && role.effective.base_url) return role.effective.base_url;
  if (role.base_url) return role.base_url;
  const provider = llmProviderFor(role.driver, role.provider);
  return provider ? provider.baseUrl : "";
}

function llmEffectiveKeyEnv(role) {
  if (role.effective && role.effective.api_key_env) return role.effective.api_key_env;
  if (role.api_key_env) return role.api_key_env;
  const provider = llmProviderFor(role.driver, role.provider);
  if (provider && provider.keyEnv) return provider.keyEnv;
  return LLM_ROLE_KEY_ENV[role.role] || "";
}

// "fully offline" is a derived state, never a stored flag (§6.5): it holds only
// when every dream role serves from the local ollama driver. The effective
// driver (resolved defaults) wins when the route payload carries it.
function isFullyOffline(roles) {
  const dreamRoles = (roles || []).filter((role) => LLM_ROLE_SUBTITLES[role.role]);
  return (
    dreamRoles.length > 0 &&
    dreamRoles.every((role) => {
      const driver = role.effective && role.effective.driver ? role.effective.driver : role.driver;
      return driver === "ollama";
    })
  );
}

function cap(word) {
  return word ? word[0].toUpperCase() + word.slice(1) : word;
}

// The plain-language connectivity-probe mapper (§7.1). The fallback always
// carries the raw driver error so a typed failure is never hidden.
function llmProbeMessage(probe, payload, provider) {
  if (probe.ok) {
    return `Connected — key in ${payload.api_key_env || "your environment"} works.`;
  }
  const detail = probe.detail;
  const errorText =
    detail && typeof detail === "object" && !Array.isArray(detail)
      ? String(detail.error || JSON.stringify(detail))
      : String(detail || "");
  const name = provider ? provider.label.replace(" (recommended)", "") : "the endpoint";
  const base = payload.base_url || (provider ? provider.baseUrl : "");
  const keyEnv = payload.api_key_env || "";
  if (/401|403/.test(errorText)) {
    const where = provider && provider.keyUrl ? provider.keyUrl : "the provider's site";
    return `The provider rejected the key in ${keyEnv} — it's missing, wrong, or expired. Create a new one at ${where}, set ${keyEnv}, and restart MnemoSeed, then test again.`;
  }
  if (payload.driver === "ollama") {
    return `Can't reach Ollama at ${base} — is the Ollama app running? Install from ollama.com, then pull a model (ollama pull llama3.1:8b).`;
  }
  if (/timeout|timed out/i.test(errorText)) {
    return `Timed out talking to ${name}. The endpoint may be slow or blocked — check ${base} and try again.`;
  }
  if (/unknown llm driver|no such driver|not built in/i.test(errorText)) {
    return `That connection type isn't built in — go back and pick a provider.`;
  }
  return `Couldn't reach ${name}. Check your internet connection or firewall, then try again. (${errorText})`;
}

// ---------------------------------------------------------------- first-run dream wizard (§11.1)
async function showDreamSetup() {
  authViewKind = "dream";
  document.title = "MnemoSeed console — dream model";
  renderHeaderAuth();
  const view = document.getElementById("view");
  view.innerHTML = '<p class="loading">Checking host OAuth…</p>';
  let routes = { roles: [], drivers: [] };
  let oauth = { providers: [] };
  try {
    const loaded = await Promise.all([
      api("/api/v1/llm/routes"),
      api("/api/v1/llm/oauth-availability"),
    ]);
    routes = loaded[0];
    oauth = loaded[1];
  } catch (_err) {
    /* degrade: the BYO-key and skip paths still work without the catalogs */
  }
  state.llm.routes = routes;
  state.llm.oauth = oauth;
  state.llm.wizard = {
    step: 1,
    providerId: null,
    oauthProvider: null,
    model: "",
    baseUrl: "",
    keyEnv: "",
    share: false,
    probeOk: false,
    models: [],
  };
  if (view) view.innerHTML = wizardStep1Html(state.llm.wizard);
}

function renderWizardPanel() {
  const view = document.getElementById("view");
  const wizard = state.llm.wizard;
  if (!view || !wizard) return;
  if (wizard.step <= 1) view.innerHTML = wizardStep1Html(wizard);
  else if (wizard.step === 2) view.innerHTML = wizardStep2Html(wizard);
  else view.innerHTML = wizardStep3Html(wizard);
}

function wizardStepBar(current) {
  const steps = ["provider", "key + model", "test & save"];
  return `<p class="toolbar-note">${steps
    .map((name, index) =>
      index + 1 === current
        ? `<span class="wizard-step-active">step ${index + 1} · ${name}</span>`
        : `<span class="dim">step ${index + 1} · ${name}</span>`,
    )
    .join(" → ")}</p>`;
}

function wizardOAuthRows(oauth) {
  const providers = (oauth && oauth.providers) || [];
  if (!providers.length) return "";
  const rows = providers
    .map((entry) => {
      const live = entry.present === true && entry.expired !== true;
      const providerName = cap(entry.provider);
      const mark = live
        ? `<span class="badge badge-ok">${providerName} login found — sign in is current.</span>`
        : entry.present === true
          ? `<span class="badge badge-warn">${providerName} login found but expired — sign in again with the ${providerName} CLI, then return here.</span>`
          : `<span class="badge">No ${providerName} login detected on this machine.</span>`;
      return `<div class="resolve-row">
        ${mark}
        <span class="spacer"></span>
        <button class="btn btn-primary" data-act="wz-oauth" data-provider="${esc(entry.provider)}" ${live ? "" : "disabled"}>Use ${providerName} login</button>
      </div>`;
    })
    .join("");
  return `<div data-oauth-panel>
    <h3>Or reuse a login already on this computer</h3>
    <p class="toolbar-note">MnemoSeed uses that login's access — you don't paste a key. No key value is read, sent, or stored.</p>
    ${rows}
  </div>`;
}

function wizardProviderCard(provider, wizard) {
  const active = wizard.providerId === provider.id;
  return `<label class="wizard-provider-card ${active ? "selected" : ""}">
    <input type="radio" name="wizard-provider" value="${esc(provider.id)}" ${active ? "checked" : ""} />
    <span class="wizard-provider-title">${esc(provider.label)}</span>
    <span class="toolbar-note">${esc(provider.note)}</span>
  </label>`;
}

// §4 / §11.1: the Ollama quality hint, shown on every wizard step while the
// role being configured is served by the local ollama driver.
function wizardQualityHint(wizard) {
  if (!wizard || wizard.oauthProvider) return "";
  const provider = llmProviderById(wizard.providerId);
  if (!provider || provider.driver !== "ollama") return "";
  return '<p class="toolbar-note">Lower synthesis quality than cloud models — you accept this for privacy or cost.</p>';
}

function wizardStep1Html(wizard) {
  return `<div class="auth-panel card" data-wizard-panel>
    <h2>dream model</h2>
    <p class="toolbar-note">Pick the model that distills your sessions into long-term memory. One model gets you started — you can change any role later in Models.</p>
    ${wizardStepBar(1)}
    ${wizardOAuthRows(state.llm.oauth)}
    <h3>Which provider do you use?</h3>
    <div class="filter-grid">${LLM_PROVIDERS.map((provider) => wizardProviderCard(provider, wizard)).join("")}</div>
    ${wizardQualityHint(wizard)}
    <div class="toolbar">
      <button class="btn" data-act="wz-skip">Skip for now — capture-only (dreaming stays off)</button>
      <span class="spacer"></span>
      <button class="btn btn-primary" data-act="wz-next" ${wizard.providerId ? "" : "disabled"}>continue</button>
    </div>
  </div>`;
}

function wizardKeyHint(provider) {
  if (provider.id === "other") {
    return `Point at any OpenAI-compatible endpoint. Your memories leave this machine to the provider's servers. Set the key env var — on macOS/Linux: export ${LLM_ROLE_KEY_ENV.deep_reflection}=…; on Windows: setx ${LLM_ROLE_KEY_ENV.deep_reflection} ….`;
  }
  return `Create the key at ${provider.keyUrl}, then set ${provider.keyEnv} — on macOS/Linux: export ${provider.keyEnv}=…; on Windows: setx ${provider.keyEnv} …. Remember: the daemon reads env vars from its own startup environment. If you set a new one, restart MnemoSeed.`;
}

function wizardKeyField(provider, wizard) {
  const roleEnv = LLM_ROLE_KEY_ENV.deep_reflection;
  const value = wizard.keyEnv || provider.keyEnv || "";
  const placeholder = provider.keyEnv || roleEnv;
  return `<div class="field"><label for="wz-keyenv">api key env var</label>
    <input type="text" id="wz-keyenv" name="api_key_env" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" />
    <details class="key-teaching">
      <summary>Your key lives in an environment variable. MnemoSeed reads it from there — you never paste the key here and it is never stored.</summary>
      <p class="toolbar-note">${esc(wizardKeyHint(provider))}</p>
    </details>
  </div>`;
}

function wizardEndpointField(provider, wizard) {
  if (provider.id === "other") {
    return `<div class="field"><label for="wz-base-url">endpoint</label>
      <input type="text" id="wz-base-url" name="base_url" value="${esc(wizard.baseUrl || "")}" placeholder="https://…/v1" required autocomplete="off" />
    </div>`;
  }
  return `<details class="key-teaching">
    <summary>Advanced: endpoint</summary>
    <div class="field"><label for="wz-base-url">endpoint</label>
      <input type="text" id="wz-base-url" name="base_url" value="${esc(wizard.baseUrl || provider.baseUrl)}" autocomplete="off" />
      <button class="btn" type="button" data-act="wz-endpoint-reset">${provider.id === "fireworks" ? "reset to Fireworks default" : "reset to default"}</button>
    </div>
  </details>`;
}

function wizardModelOptions(provider, wizard) {
  const curated = llmCuratedModels(provider);
  const catalog = (wizard.models || []).filter((model) => !curated.includes(model));
  return curated
    .concat(catalog)
    .map((model) => `<option value="${esc(model)}"></option>`)
    .join("");
}

function wizardStep2Html(wizard) {
  const provider = llmProviderById(wizard.providerId);
  const oauthMode = wizard.oauthProvider !== null;
  const oauthName = cap(wizard.oauthProvider);
  return `<div class="auth-panel card" data-wizard-panel>
    <h2>dream model</h2>
    <p class="toolbar-note">Pick the model that distills your sessions into long-term memory. One model gets you started — you can change any role later in Models.</p>
    ${wizardStepBar(2)}
    ${oauthMode ? `<p class="toolbar-note"><span class="badge badge-ok">Using the ${oauthName} login on this machine — no key needed. It refreshes itself while you're signed in.</span></p>` : ""}
    <form data-llm-wizard-form>
      ${oauthMode ? "" : provider && provider.driver !== "ollama" ? wizardKeyField(provider, wizard) : ""}
      ${oauthMode ? "" : provider ? wizardEndpointField(provider, wizard) : ""}
      ${wizardQualityHint(wizard)}
      <div class="field"><label for="wz-model">model</label>
        <input type="text" id="wz-model" name="model" list="wz-models" value="${esc(wizard.model || llmRoleDefaultModel(provider, "deep_reflection"))}" placeholder="type or pick a model" required autocomplete="off" />
        <datalist id="wz-models">${provider ? wizardModelOptions(provider, wizard) : ""}</datalist>
        ${provider && provider.id === "ollama" ? '<span class="toolbar-note">If the model is missing, pull it first: ollama pull llama3.1:8b</span>' : ""}
        ${provider && !oauthMode && !llmCuratedModels(provider).length ? '<span class="toolbar-note">No models listed — pick a suggestion or type the exact model id.</span>' : ""}
      </div>
      <div class="toolbar">
        <button class="btn" type="button" data-act="wz-back">back</button>
        <span class="spacer"></span>
        <button class="btn btn-primary" type="submit">continue</button>
      </div>
      <output class="feedback" data-wz-feedback></output>
    </form>
  </div>`;
}

function wizardStep3Html(wizard) {
  const provider = llmProviderById(wizard.providerId);
  const oauthMode = wizard.oauthProvider !== null;
  const summary = oauthMode
    ? `<span class="badge badge-ok">${cap(wizard.oauthProvider)} login on this machine</span>`
    : `<span class="badge">${esc(provider ? provider.label : "")}</span>`;
  return `<div class="auth-panel card" data-wizard-panel>
    <h2>dream model</h2>
    <p class="toolbar-note">Pick the model that distills your sessions into long-term memory. One model gets you started — you can change any role later in Models.</p>
    ${wizardStepBar(3)}
    <p>${summary} · <span class="mono">${esc(wizard.model)}</span></p>
    ${wizardQualityHint(wizard)}
    <form data-llm-wizard-form>
      <label class="wizard-share">
        <input type="checkbox" name="wizard-share" ${wizard.share ? "checked" : ""} />
        <span>also apply to short_increment</span>
        <span class="toolbar-note">Uses the same provider and key for the quick consolidation model.</span>
      </label>
      <div class="toolbar">
        <button class="btn" type="button" data-act="wz-back">back</button>
        <span class="spacer"></span>
        <button class="btn" type="button" data-act="wz-test">Test connection</button>
        <button class="btn btn-primary" type="submit" ${wizard.probeOk ? "" : "disabled"}>save</button>
      </div>
      <output class="feedback" data-wz-feedback></output>
    </form>
  </div>`;
}

function wizardPayload(wizard) {
  const provider = llmProviderById(wizard.providerId);
  const oauthMode = wizard.oauthProvider !== null;
  const payload = {
    driver: oauthMode ? "oauth" : provider ? provider.driver : "",
    model: wizard.model.trim(),
    provider: oauthMode ? wizard.oauthProvider : provider ? provider.id : "",
  };
  if (!oauthMode && provider) {
    const baseUrl = (wizard.baseUrl || provider.baseUrl || "").trim();
    if (baseUrl) payload.base_url = baseUrl;
    // ollama needs no key (the field is hidden); every other provider falls
    // back to its standard env-var name, or the role's when none is in play.
    const keyEnv =
      provider.driver === "ollama"
        ? ""
        : (wizard.keyEnv || provider.keyEnv || LLM_ROLE_KEY_ENV.deep_reflection).trim();
    if (keyEnv) payload.api_key_env = keyEnv;
  }
  return payload;
}

function wizardCollect(form, wizard) {
  const data = new FormData(form);
  wizard.model = String(data.get("model") || "").trim();
  wizard.baseUrl = String(data.get("base_url") || "").trim();
  wizard.keyEnv = String(data.get("api_key_env") || "").trim();
  wizard.probeOk = false;
}

async function wizardTest(form) {
  const wizard = state.llm.wizard;
  if (!wizard) return;
  const feedback = form.querySelector("[data-wz-feedback]");
  const payload = wizardPayload(wizard);
  const provider = llmProviderById(wizard.providerId);
  const probeLabel = provider ? provider.label.replace(" (recommended)", "") : "the endpoint";
  if (feedback) feedback.innerHTML = `<span class="dim">Testing connection to ${esc(probeLabel)}…</span>`;
  try {
    const probe = await api("/api/v1/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "deep_reflection", ...payload }),
    });
    const message = llmProbeMessage(probe, payload, provider);
    if (probe.ok) {
      wizard.probeOk = true;
      if (probe.detail && Array.isArray(probe.detail.models)) wizard.models = probe.detail.models;
    } else {
      wizard.probeOk = false;
    }
    if (feedback) {
      feedback.innerHTML = probe.ok
        ? `<span class="ok-inline">${esc(message)}</span>`
        : errorInline(esc(message));
    }
  } catch (error) {
    wizard.probeOk = false;
    if (feedback) feedback.innerHTML = errorInline(`test failed: ${error.message}`);
  }
}

async function wizardSave(form) {
  const wizard = state.llm.wizard;
  if (!wizard) return;
  const feedback = form.querySelector("[data-wz-feedback]");
  const payload = wizardPayload(wizard);
  if (!wizard.probeOk) {
    if (feedback) feedback.innerHTML = errorInline("Test the connection first — a route can only be saved after a passing probe of these exact values.");
    return;
  }
  if (feedback) feedback.innerHTML = '<span class="dim">saving…</span>';
  try {
    await api("/api/v1/llm/routes/deep_reflection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (wizard.share) {
      await api("/api/v1/llm/routes/short_increment", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
    }
    const model = wizard.model.trim();
    finishDreamSetup(
      wizard.share
        ? `dream model configured: deep reflection + short increment → ${model}`
        : `dream model configured: deep reflection → ${model}`,
    );
  } catch (error) {
    if (feedback) {
      feedback.innerHTML = /HTTP 409/.test(error.message)
        ? errorInline("Test the connection first — a route can only be saved after a passing probe of these exact values.")
        : errorInline(`save failed: ${error.message}`);
    }
  }
}

function finishDreamSetup(message) {
  authViewKind = null;
  state.llm.message = message;
  render();
}

async function signOut() {
  await api("/api/v1/auth/logout", { method: "POST" }).catch(() => null);
  setAuth(null);
  showLogin("Signed out.");
}

// Boot gate: probe setup mode, then render the wizard, the login view, or the
// app (validating any stored token once so a stale one returns to login).
async function boot() {
  clearAutoRefresh();
  let status;
  try {
    status = await fetchOpen("/api/v1/setup/status");
  } catch (error) {
    const view = document.getElementById("view");
    if (view) view.innerHTML = errorPanel(`Console unavailable: ${error.message}`);
    return;
  }
  if (status.setup_required) {
    showSetup();
    return;
  }
  if (!AUTH.token) {
    showLogin();
    return;
  }
  try {
    const me = await api("/api/v1/auth/me");
    if (!AUTH.token) return; // a 401 cleared the token and deferred the login view
    AUTH.username = me.username || AUTH.username;
    store.set("mnemoseed.username", AUTH.username || "");
    renderHeaderAuth();
    authViewKind = null;
    render();
  } catch (error) {
    if (!AUTH.token) return;
    const view = document.getElementById("view");
    if (view) view.innerHTML = errorPanel(`Console unavailable: ${error.message}`);
  }
}

// ---------------------------------------------------------------- API glue
async function api(path, options) {
  const opts = options || {};
  const headers = new Headers(opts.headers || {});
  if (AUTH.token) headers.set("Authorization", `Bearer ${AUTH.token}`);
  const response = await fetch(path, { ...opts, headers });
  if (response.status === 401) {
    setAuth(null);
    // Defer so an in-flight loader's error panel cannot stomp the login view.
    setTimeout(() => showLogin("Session expired — sign in again."), 0);
    throw new Error("HTTP 401: profile token invalid or expired");
  }
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      if (body && body.detail) detail = String(body.detail);
    } catch (_err) {
      /* non-JSON error body */
    }
    throw new Error(`HTTP ${response.status}${detail ? `: ${detail}` : ""}`);
  }
  return response.json();
}

function setUpdatedAt() {
  const node = document.getElementById("updated-at");
  if (node) node.textContent = `updated ${new Date().toLocaleTimeString()}`;
}

// ---------------------------------------------------------------- routing
function parseRoute() {
  const hash = (location.hash || "").replace(/^#/, "") || "/dashboard";
  if (hash.startsWith("/detail/")) {
    const bits = hash.split("/").filter(Boolean);
    return { name: "detail", type: bits[1] || null, id: decodeURIComponent(bits[2] || "") };
  }
  if (hash.startsWith("/browse")) return { name: "browse" };
  if (hash.startsWith("/graph")) return { name: "graph" };
  if (hash.startsWith("/review")) return { name: "review" };
  if (hash.startsWith("/conflicts")) return { name: "conflicts" };
  if (hash.startsWith("/profiles")) return { name: "profiles" };
  if (hash.startsWith("/llm")) return { name: "llm" };
  if (hash.startsWith("/settings")) return { name: "settings" };
  if (hash.startsWith("/audit")) return { name: "audit" };
  return { name: "dashboard" };
}

function updateNav(name) {
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.classList.toggle("active", link.dataset.nav === name);
  });
}

function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

function clearAutoRefresh() {
  if (state.autoRefreshTimer) {
    clearInterval(state.autoRefreshTimer);
    state.autoRefreshTimer = null;
  }
}

function scheduleAutoRefresh() {
  clearAutoRefresh();
  if (parseRoute().name !== "dashboard") return;
  state.autoRefreshTimer = setInterval(render, REFRESH_MS);
}

function render() {
  if (authViewKind) {
    // A hash change while the setup wizard or login view owns the page: rerun
    // the gate instead of rendering a client section the server would reject.
    boot();
    return;
  }
  const route = parseRoute();
  if (route.name !== "graph") disposeGraphView();
  updateNav(route.name === "detail" ? "browse" : route.name);
  document.title = route.name === "detail" ? "MnemoSeed console — detail" : "MnemoSeed console";
  clearAutoRefresh();
  const view = document.getElementById("view");
  if (route.name === "dashboard") {
    view.innerHTML = '<p class="loading">Loading dashboard…</p>';
    renderProfilePicker();
    loadDashboard().then(scheduleAutoRefresh);
  } else if (route.name === "browse") {
    renderBrowseShell();
    loadBrowse();
  } else if (route.name === "graph") {
    view.innerHTML = '<p class="loading">Loading graph…</p>';
    loadGraph();
  } else if (route.name === "review") {
    view.innerHTML = '<p class="loading">Loading dream review…</p>';
    loadReview();
  } else if (route.name === "conflicts") {
    view.innerHTML = '<p class="loading">Loading conflicts inbox…</p>';
    loadConflicts();
  } else if (route.name === "profiles") {
    view.innerHTML = '<p class="loading">Loading profiles…</p>';
    loadProfiles();
  } else if (route.name === "llm") {
    view.innerHTML = '<p class="loading">Loading models…</p>';
    loadLLM();
  } else if (route.name === "settings") {
    view.innerHTML = '<p class="loading">Loading settings…</p>';
    loadSettings();
  } else if (route.name === "audit") {
    view.innerHTML = '<p class="loading">Loading audit log…</p>';
    loadAudit();
  } else {
    view.innerHTML = '<p class="loading">Loading detail…</p>';
    if (!route.type || !route.id) {
      view.innerHTML = errorPanel("Incomplete detail link.");
      return;
    }
    loadDetail(route.type, route.id);
  }
}

// ---------------------------------------------------------------- profile picker
function syncProfileFromStatus() {
  const ids = state.profiles.map((p) => p.profile_id);
  if (!state.profileId || !ids.includes(state.profileId)) {
    state.profileId = ids.length ? ids[0] : null;
    if (state.profileId) store.set("mnemoseed.profile", state.profileId);
  }
}

async function ensureProfile() {
  if (!state.profiles.length) {
    // A fresh page load (e.g. a deep link to #/browse) has no profile list yet;
    // resolve it once so the header picker reflects the daemon's real profiles
    // even when a cached profile selection already exists in localStorage.
    const status = await api("/api/v1/status");
    state.dashboard = status;
    state.profiles = status.profiles || [];
    state.dreamStatus = null;
    syncProfileFromStatus();
    renderProfilePicker();
  }
  return state.profileId;
}

function renderProfilePicker() {
  const select = document.getElementById("profile-select");
  if (!select) return;
  const ids = state.profiles.map((p) => p.profile_id);
  if (!ids.length) {
    select.innerHTML = '<option value="">no profiles</option>';
    select.disabled = true;
    return;
  }
  select.disabled = false;
  select.innerHTML = ids
    .map((id) => `<option value="${esc(id)}" ${id === state.profileId ? "selected" : ""}>${esc(id)}</option>`)
    .join("");
}

// ---------------------------------------------------------------- dashboard
async function loadDashboard() {
  try {
    const status = await api("/api/v1/status");
    state.dashboard = status;
    state.profiles = status.profiles || [];
    state.dreamStatus = null;
    syncProfileFromStatus();
    renderProfilePicker();
    let dream = null;
    let runs = null;
    if (state.profileId) {
      try {
        dream = await api(`/api/v1/dream/status?profile_id=${encodeURIComponent(state.profileId)}`);
      } catch (error) {
        dream = { error: error.message };
      }
      try {
        runs = await api("/api/v1/dream/runs?limit=10");
      } catch (error) {
        runs = { error: error.message };
      }
    }
    state.dreamStatus = dream;
    state.dreamRuns = runs;
    const view = document.getElementById("view");
    view.innerHTML = dashboardHtml(status, dream, runs);
    if (state.llm.message) {
      // First-run dream model result: one banner on the freshly loaded dashboard.
      view.insertAdjacentHTML(
        "afterbegin",
        `<div class="card"><h2>dream model</h2><span class="ok-inline">${esc(state.llm.message)}</span></div>`,
      );
      state.llm.message = null;
    }
    setUpdatedAt();
  } catch (error) {
    const view = document.getElementById("view");
    view.innerHTML = errorPanel(`Status unavailable: ${error.message}`);
  }
}

function dashboardHtml(status, dream, runs) {
  const daemon = status.daemon || {};
  const rows = status.profiles || [];
  let body = daemonCard(daemon);
  if (!rows.length) {
    body += emptyPanel("No profiles yet. Memories appear here once capture writes to a profile.");
  } else {
    const activeId = state.profileId;
    body += rows
      .map((row) => profileCard(row, row.profile_id === activeId))
      .join("");
    const activeRow = rows.find((row) => row.profile_id === activeId) || rows[0];
    body += dreamPanel(activeRow, dream, runs);
  }
  return body;
}

function daemonCard(daemon) {
  const gateOk = daemon.gate && daemon.gate.ok === true;
  const drivers = daemon.drivers || {};
  const chips = ["vector", "graph", "meta", "embed"]
    .map((kind) => `<span class="badge" title="${esc(kind)} driver">${esc(drivers[kind] || "?")}</span>`)
    .join(" ");
  return `<div class="card">
    <h2>daemon health</h2>
    <div class="tiles">
      ${tile(esc(daemon.version || "—"), "version")}
      ${tile(gateOk ? "ok" : "degraded", "capability gate", gateOk ? "ok" : "err")}
      ${tile(esc(daemon.preset || "—"), "preset")}
      <div class="tile"><div class="tile-value"><span class="badges">${chips}</span></div><div class="tile-label">storage drivers</div></div>
    </div>
  </div>`;
}

function profileCard(row, active) {
  const counts = row.counts || {};
  const pool = row.pool || {};
  const tokens = row.tokens || {};
  const ledger = tokens.ledger || {};
  const dream = row.dream || {};
  const reconcile = counts.needs_reconcile || 0;
  const pending = counts.pending_consolidation || 0;
  const activeMark = active ? ' style="border-color:rgba(79,140,255,0.5)"' : "";
  return `<div class="card"${activeMark}>
    <h2>${esc(row.profile_id)} ${active ? '<span class="badge badge-accent">active</span>' : ""}</h2>
    <div class="tiles">
      ${tile(esc(dream.state || "—"), "dream state", dream.state === "dreaming" ? "warn" : "")}
      ${tile(fmtNum(pool.balance), "pool balance")}
      ${tile(pool.watermark ? fmtRange(pool.watermark) : "—", "pool watermark")}
      ${tile(fmtNum(counts.chunks), "chunks")}
      ${tile(fmtNum(counts.nodes), "nodes")}
      ${tile(fmtNum(reconcile), "needs reconcile", reconcile > 0 ? "warn" : "")}
      ${tile(fmtNum(pending), "pending consolidation", pending > 0 ? "warn" : "")}
      ${tile(fmtNum(tokens.today), "dream tokens today")}
      ${tile(fmtNum(tokens.this_week), "dream tokens this week")}
      ${tile(fmtNum(ledger.used_tokens), `schedule tokens ${esc(ledger.year_month || "")}`)}
    </div>
    <h3>monthly ledger</h3>
    ${kvList([
      ["budget", esc(fmtMoney(ledger.budget_usd))],
      ["used", esc(fmtMoney(ledger.used_usd))],
      ["remaining", esc(fmtMoney(ledger.remaining_usd))],
    ])}
  </div>`;
}

function dreamPanel(statusRow, dream, runs) {
  const autoOn = !!(statusRow && statusRow.dream && statusRow.dream.auto_trigger === true);
  const stateStr = (dream && dream.state) || (statusRow && statusRow.dream && statusRow.dream.state) || "—";
  const pool = (statusRow && statusRow.pool) || {};
  const queue =
    (dream && (dream.queue_depth ?? dream.pending_manual ?? 0)) ||
    (statusRow && statusRow.dream && statusRow.dream.pending_manual) ||
    0;
  const dreamErr = dream && dream.error;
  const runsErr = runs && runs.error;
  return `<div class="card">
    <h2>dream engine</h2>
    <div class="tiles">
      ${tile(esc(stateStr), "state machine", stateStr === "dreaming" ? "warn" : "")}
      ${tile(fmtNum(pool.balance), "pool balance")}
      ${tile(fmtNum(queue), "pending queue", queue > 0 ? "warn" : "")}
    </div>
    <h3>controls</h3>
    <div class="toolbar">
      <button class="btn btn-primary" data-act="dream-once" ${state.profileId ? "" : "disabled"}>Dream once</button>
      <label class="check-row"><input type="checkbox" data-act="toggle-auto" ${autoOn ? "checked" : ""} ${state.profileId ? "" : "disabled"} /> auto-trigger</label>
      <span class="spacer"></span>
      <span class="toolbar-note">writes are audited</span>
    </div>
    ${dreamErr ? errorInline(`Dream status unavailable: ${dreamErr}`) : lastEventHtml(dream)}
    <h3>run history</h3>
    ${runsErr ? errorInline(`Run history unavailable: ${runsErr}`) : runsTable(runs)}
  </div>`;
}

function lastEventHtml(dream) {
  const last = dream && dream.last_event;
  const head = "<h3>last trigger event</h3>";
  if (!dream) return "";
  if (!last) {
    return `${head}<div class="kv"><span class="kv-label">event</span><span class="kv-value"><span class="dim">none yet</span></span></div>`;
  }
  return `${head}${kvList([
    ["kind", `<span class="mono">${esc(last.kind)}</span>`],
    ["fired at", esc(fmtEpoch(last.fired_at))],
    ["turn range", esc(fmtRange(last.turn_range))],
  ])}`;
}

function runsTable(runs) {
  const list = runs && runs.runs ? runs.runs : [];
  if (!list.length) return '<p class="dim">No dream runs recorded yet.</p>';
  const rows = list
    .map(
      (run) => `<tr>
        <td>${fmtEpoch(run.started_at)}</td>
        <td>${fmtDuration(run.duration_seconds)}</td>
        <td class="mono">${esc(run.model_id || "—")}</td>
        <td class="mono">${esc((run.run_id || "").slice(0, 8) || "—")}</td>
        <td>${fmtNum(run.tokens)}</td>
        <td>${fmtMoney(run.cost)}</td>
        <td>${run.interrupted ? '<span class="badge badge-warn">interrupted</span>' : '<span class="badge badge-ok">done</span>'}</td>
        <td>${fmtNum(run.dropped_count)}</td>
        <td>${esc(fmtRange(run.turn_range))}</td>
      </tr>`,
    )
    .join("");
  return `<div class="table-wrap"><table>
    <thead><tr><th>started</th><th>duration</th><th>model</th><th>run</th><th>tokens</th><th>cost</th><th>status</th><th>dropped</th><th>turn range</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

// ---------------------------------------------------------------- memory browser
function renderBrowseShell() {
  const view = document.getElementById("view");
  view.innerHTML = `<div class="toolbar">
      <div class="tabs" role="tablist">
        <button class="tab ${state.browse.tab === "chunks" ? "active" : ""}" data-act="browse-tab" data-tab="chunks" role="tab" aria-selected="${state.browse.tab === "chunks"}">Chunks</button>
        <button class="tab ${state.browse.tab === "nodes" ? "active" : ""}" data-act="browse-tab" data-tab="nodes" role="tab" aria-selected="${state.browse.tab === "nodes"}">Nodes</button>
      </div>
      <span class="spacer"></span>
      <button class="btn" data-act="browse-refresh">Refresh</button>
    </div>
    <form class="card" data-browse-form>
      <h2>filters · ${state.browse.tab}</h2>
      <div class="filter-grid">${filterFieldsHtml(state.browse.tab, state.browse.filters)}</div>
      <div class="toolbar">
        <button class="btn btn-primary" type="submit">Apply</button>
        <button class="btn" type="button" data-act="browse-reset">Reset</button>
      </div>
    </form>
    <div id="browse-results"></div>`;
}

function filterFieldsHtml(tab, filters) {
  return FILTER_MODEL[tab]
    .map((field) => fieldHtml(field, filters[field.name]))
    .join("");
}

function fieldSelect(field, values, labels, value) {
  const opts = values
    .map((v, i) => `<option value="${esc(v)}" ${String(value ?? "") === String(v) ? "selected" : ""}>${esc(labels[i])}</option>`)
    .join("");
  return `<div class="field"><label for="f-${field.name}">${esc(field.label)}</label><select id="f-${field.name}" name="${field.name}">${opts}</select></div>`;
}

function fieldHtml(field, value) {
  const id = `f-${field.name}`;
  switch (field.kind) {
    case "datetime":
      return `<div class="field"><label for="${id}">${esc(field.label)}</label><input type="datetime-local" id="${id}" name="${field.name}" value="${esc(value ?? "")}" /></div>`;
    case "text":
      return `<div class="field"><label for="${id}">${esc(field.label)}</label><input type="text" id="${id}" name="${field.name}" value="${esc(value ?? "")}" /></div>`;
    case "tier":
      return fieldSelect(field, ["", "1", "2", "3"], ["any", "Tier 1", "Tier 2", "Tier 3"], value);
    case "decay":
      return `<div class="field"><label for="${id}">${esc(field.label)} (0–1)</label><input type="number" id="${id}" name="${field.name}" min="0" max="1" step="0.01" value="${esc(value ?? "")}" /></div>`;
    case "node-type":
      return fieldSelect(
        field,
        ["", ...NODE_TYPES],
        ["any", ...NODE_TYPES.map(niceLabel)],
        value,
      );
    case "check":
      return `<div class="field"><label>&nbsp;</label><label class="check-row"><input type="checkbox" id="${id}" name="${field.name}" ${value ? "checked" : ""} /> ${esc(field.label)}</label></div>`;
    default:
      return "";
  }
}

function readFilters(form) {
  const data = new FormData(form);
  const out = {};
  for (const field of FILTER_MODEL[state.browse.tab]) {
    if (field.kind === "check") out[field.name] = data.get(field.name) !== null;
    else out[field.name] = String(data.get(field.name) ?? "");
  }
  return out;
}

function queryParams(filters, offset, limit) {
  const params = new URLSearchParams();
  if (state.profileId) params.set("profile_id", state.profileId);
  for (const field of FILTER_MODEL[state.browse.tab]) {
    const raw = filters[field.name];
    switch (field.kind) {
      case "datetime": {
        const epoch = datetimeToEpoch(raw);
        if (epoch !== null) params.set(field.name, String(epoch));
        break;
      }
      case "text": {
        if (field.name === "entity") {
          String(raw || "")
            .split(",")
            .map((part) => part.trim())
            .filter(Boolean)
            .forEach((part) => params.append("entity", part));
        } else if (raw && String(raw).trim()) {
          params.set(field.name, String(raw).trim());
        }
        break;
      }
      case "tier":
        if (raw) params.set(field.name, String(raw));
        break;
      case "decay": {
        if (raw === "" || raw === null || raw === undefined) break;
        const num = Number(raw);
        if (Number.isFinite(num)) params.set(field.name, String(num));
        break;
      }
      case "node-type":
        if (raw) params.set(field.name, String(raw));
        break;
      case "check":
        if (raw === true) params.set(field.name, "true");
        break;
      default:
        break;
    }
  }
  params.set("offset", String(offset));
  params.set("limit", String(limit));
  return params.toString();
}

function switchTab(tab) {
  if (tab !== "chunks" && tab !== "nodes") return;
  state.browse.tab = tab;
  state.browse.filters = {};
  state.browse.offset = 0;
  renderBrowseShell();
  loadBrowse();
}

async function loadBrowse() {
  const results = document.getElementById("browse-results");
  if (!results) return;
  results.innerHTML = `<p class="loading">Loading ${esc(state.browse.tab)}…</p>`;
  try {
    await ensureProfile();
  } catch (_err) {
    /* fall through to the empty state */
  }
  if (!state.profileId) {
    results.innerHTML = emptyPanel("No profile available yet — memories appear here once capture writes to a profile.");
    return;
  }
  try {
    const endpoint = state.browse.tab === "chunks" ? "/api/v1/chunks" : "/api/v1/nodes";
    const params = queryParams(state.browse.filters, state.browse.offset, state.browse.limit);
    const body = await api(`${endpoint}?${params}`);
    state.browse.data = body;
    results.innerHTML = browseResultsHtml(body);
    setUpdatedAt();
  } catch (error) {
    state.browse.data = null;
    results.innerHTML = errorPanel(`Browser request failed: ${error.message}`);
  }
}

function browseResultsHtml(body) {
  const items = body.items || [];
  const paging = body.paging || { total: 0, offset: 0, limit: state.browse.limit };
  const list = items.length
    ? items.map((item) => (state.browse.tab === "chunks" ? chunkRowHtml(item) : nodeRowHtml(item))).join("")
    : emptyPanel("No memories match this profile and filter set.");
  const flash = state.browseFlash
    ? `<div class="card"><span class="ok-inline">${esc(state.browseFlash)}</span></div>`
    : "";
  state.browseFlash = null;
  return `${flash}${list}${paginationBar(paging, items.length)}`;
}

function chunkRowHtml(chunk) {
  const cues = chunk.cues || {};
  const metaBits = [
    `Tier ${chunk.cognitive_tier ?? "—"}`,
    cues.project ? `project ${cues.project}` : "",
    cues.host ? `host ${cues.host}` : "",
    `ingested ${fmtEpoch(chunk.ingested_at)}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return `<button class="row-item" data-act="open-detail" data-type="chunk" data-id="${esc(chunk.chunk_id)}">
    <div class="row-title">${esc(truncate(chunk.text, 160))}</div>
    <div class="row-meta">
      <span>${decayMeter(chunk.decay_weight)}</span>
      ${flagBadge("consolidated", chunk.consolidated === true, "accent")}
      ${badgeList(cues.entities)}
    </div>
    <div class="row-meta">${esc(metaBits)}</div>
  </button>`;
}

function nodeRowHtml(node) {
  const metaBits = [
    `type ${niceLabel(node.node_type)}`,
    node.conflict_group ? `conflict group ${node.conflict_group}` : "",
    `v${fmtNum(node.version)}`,
    `hits ${fmtNum(node.hit_count)}`,
    `updated ${fmtEpoch(node.updated_at)}`,
  ]
    .filter(Boolean)
    .join(" · ");
  return `<button class="row-item" data-act="open-detail" data-type="node" data-id="${esc(node.node_id)}">
    <div class="row-title">${esc(truncate(node.statement, 160))}</div>
    <div class="row-meta">
      <span>${decayMeter(node.decay_weight)}</span>
      ${flagBadge("conflict", node.conflict_flag === true, "err")}
      ${flagBadge("reconcile", node.needs_reconcile === true, "warn")}
      ${flagBadge("pending", node.pending_consolidation === true, "warn")}
      ${badgeList(node.entities)}
    </div>
    <div class="row-meta">${esc(metaBits)}</div>
  </button>`;
}

function paginationBar(paging, shown, act = "browse-page") {
  const total = paging.total || 0;
  const offset = paging.offset || 0;
  const limit = paging.limit || state.browse.limit;
  const count = shown || 0;
  const hasPrev = offset > 0;
  const hasNext = offset + count < total;
  const prevOffset = Math.max(0, offset - limit);
  const nextOffset = offset + limit;
  const label = count
    ? `${offset + 1}–${offset + count} of ${fmtNum(total)}`
    : `offset ${fmtNum(offset)} of ${fmtNum(total)}`;
  return `<div class="pagination">
    <button class="btn" data-act="${act}" data-offset="${prevOffset}" ${hasPrev ? "" : "disabled"}>← prev</button>
    <span>${label}</span>
    <button class="btn" data-act="${act}" data-offset="${nextOffset}" ${hasNext ? "" : "disabled"}>next →</button>
  </div>`;
}

// ---------------------------------------------------------------- memory detail
async function loadDetail(type, id) {
  try {
    await ensureProfile();
  } catch (_err) {
    /* fall through */
  }
  const view = document.getElementById("view");
  if (!state.profileId) {
    view.innerHTML = errorPanel("No profile selected — the dossier cannot be resolved without a profile.");
    return;
  }
  const kind = type === "node" ? "nodes" : "chunks";
  const url = `/api/v1/${kind}/${encodeURIComponent(id)}?profile_id=${encodeURIComponent(state.profileId)}`;
  try {
    const dossier = await api(url);
    const flash = state.detailFlash ? `<div class="card"><span class="ok-inline">${esc(state.detailFlash)}</span></div>` : "";
    state.detailFlash = null;
    view.innerHTML = flash + detailHtml(dossier);
    document.title = `MnemoSeed console — ${type} ${id}`;
    setUpdatedAt();
  } catch (error) {
    const notFound = String(error.message).includes("404");
    view.innerHTML = errorPanel(
      notFound
        ? `This ${type} was not found in profile ${state.profileId} (removed, or it was never written).`
        : `Dossier request failed: ${error.message}`,
    );
  }
}

function detailHtml(dossier) {
  const isNode = dossier.type === "node";
  const id = isNode ? dossier.node_id : dossier.chunk_id;
  const head = `<div class="detail-head">
    <button class="btn" data-act="go-browse">← browse</button>
    <h1 class="mono">${esc(id)}</h1>
    ${isNode ? `<span class="badge badge-accent">${esc(niceLabel(dossier.node_type))}</span>` : `<span class="badge">chunk · tier ${esc(String((dossier.metadata || {}).cognitive_tier ?? "—"))}</span>`}
  </div>`;
  const content = isNode ? nodeContentCard(dossier) : chunkContentCard(dossier);
  const states = isNode ? nodeStatesCard(dossier) : chunkStatesCard(dossier);
  const actions = actionsCard(dossier);
  const provenance = provenanceCard(dossier.provenance);
  return `${head}${content}${states}${actions}${provenance}`;
}

// FR-7.9 / G-AC1: the dossier actions card. Forget is confirm-twice; pin flips
// never_decay (node-only); the weight control is a bounded [0,1] input with the
// old -> new change displayed after the write. Every action is audited server-side.
function actionsCard(dossier) {
  const isNode = dossier.type === "node";
  const id = isNode ? dossier.node_id : dossier.chunk_id;
  const neverDecay = isNode && dossier.weights && dossier.weights.never_decay === true;
  const pinBtn = isNode
    ? `<button class="btn" data-act="detail-pin" data-id="${esc(id)}" data-pinned="${neverDecay ? "false" : "true"}" title="${neverDecay ? "unpin — resume decay" : "pin — never decay"}">${neverDecay ? "unpin" : "pin"}</button>`
    : "";
  const current = dossier.weights && dossier.weights.decay_weight;
  return `<div class="card">
    <h2>actions</h2>
    <div class="toolbar">
      ${pinBtn}
      <button class="btn btn-danger" data-act="detail-forget" data-type="${isNode ? "node" : "chunk"}" data-id="${esc(id)}" title="forget this memory (audited)">forget</button>
    </div>
    <h3>manual decay adjustment</h3>
    <form data-weight-form data-kind="${isNode ? "node" : "chunk"}" data-id="${esc(id)}">
      <div class="filter-grid">
        <div class="field"><label for="weight-${esc(id)}">decay weight (0–1)</label><input type="number" id="weight-${esc(id)}" name="decay_weight" min="0" max="1" step="0.05" value="${esc(String(current ?? ""))}" required /></div>
      </div>
      <div class="toolbar">
        <span class="toolbar-note">current ${esc(String(current ?? "—"))} → new; the audit records both values</span>
        <span class="spacer"></span>
        <button class="btn btn-primary" type="submit">apply weight</button>
      </div>
      <output class="feedback" data-weight-feedback></output>
    </form>
  </div>`;
}

function chunkContentCard(dossier) {
  const content = dossier.content || {};
  const cues = dossier.cues || {};
  return `<div class="card">
    <h2>verbatim channel</h2>
    <pre class="mono" style="white-space:pre-wrap;font-size:0.9rem">${esc(content.verbatim || "")}</pre>
    ${kvList(cueKvs(cues))}
  </div>`;
}

function cueKvs(cues) {
  const emotion = cues.emotion;
  const pairs = [
    ["project", esc(cues.project || "—")],
    ["host", esc(cues.host || "—")],
    ["task", esc(cues.task || "—")],
    ["tools used", esc((cues.tools_used || []).join(", ") || "—")],
    ["time bucket", esc(cues.time_bucket || "—")],
    ["entities", badgeList(cues.entities)],
  ];
  if (emotion) {
    pairs.push(["emotion valence", esc(String(emotion.valence ?? "—"))]);
    pairs.push(["emotion arousal", esc(String(emotion.arousal ?? "—"))]);
    pairs.push(["peripheral gaps", esc(String(emotion.peripheral_gaps ?? "—"))]);
  } else {
    pairs.push(["emotion", '<span class="dim">none</span>']);
  }
  return pairs;
}

function chunkStatesCard(dossier) {
  const weights = dossier.weights || {};
  const flags = dossier.flags || {};
  const usage = dossier.usage || {};
  const meta = dossier.metadata || {};
  return `<div class="card">
    <h2>weights</h2>
    ${kvList([
      ["decay weight", decayMeter(weights.decay_weight)],
      ["score", esc(fmtNum(weights.score))],
      ["confidence", esc(String(weights.confidence ?? "—"))],
      ["last reinforced", esc(fmtEpoch(weights.last_reinforced))],
      ["reinforce count", esc(fmtNum(weights.reinforce_count))],
    ])}
    <h3>flags</h3>
    ${kvList([
      ["consolidated", flagValue(flags.consolidated)],
      ["needs reconcile", flagValue(flags.needs_reconcile)],
      ["pending consolidation", flagValue(flags.pending_consolidation)],
      ["conflict", flagValue(flags.conflict_flag)],
      ["peripheral gaps", flagValue(flags.peripheral_gaps)],
    ])}
    <h3>usage</h3>
    ${kvList([
      ["hit count", esc(fmtNum(usage.hit_count))],
      ["last hit at", esc(fmtEpoch(usage.last_hit_at))],
    ])}
    <h3>metadata</h3>
    ${kvList([
      ["cognitive tier", esc(String(meta.cognitive_tier ?? "—"))],
      ["model id", esc(meta.model_id || "—")],
      ["persona id", esc(meta.persona_id || "—")],
      ["ingested at", esc(fmtEpoch(meta.ingested_at))],
      ["turn range", esc(fmtRange(meta.turn_start != null ? { start: meta.turn_start, end: meta.turn_end } : null))],
    ])}
  </div>`;
}

function nodeContentCard(dossier) {
  const content = dossier.content || {};
  const hasTriple = [content.subject, content.predicate, content.object].some(
    (v) => v !== null && v !== undefined && v !== "",
  );
  const triple = hasTriple
    ? `<div class="kv"><span class="kv-label">triple</span><span class="kv-value"><span class="triple-term s">${esc(String(content.subject ?? "?"))}</span> → <span class="triple-term p">${esc(String(content.predicate ?? "?"))}</span> → <span class="triple-term o">${esc(String(content.object ?? "?"))}</span></span></div>`
    : `<div class="kv"><span class="kv-label">triple</span><span class="kv-value"><span class="dim">no structured triple for this node type</span></span></div>`;
  return `<div class="card">
    <h2>triple channel</h2>
    ${kvList([
      ["statement", `<span class="mono">${esc(content.statement || "—")}</span>`],
      ["entities", badgeList(dossier.entities)],
    ])}
    ${triple}
  </div>`;
}

function nodeStatesCard(dossier) {
  const weights = dossier.weights || {};
  const flags = dossier.flags || {};
  const usage = dossier.usage || {};
  const version = dossier.version || {};
  return `<div class="card">
    <h2>weights</h2>
    ${kvList([
      ["decay weight", decayMeter(weights.decay_weight)],
      ["confidence", esc(String(weights.confidence ?? "—"))],
      ["reinforce count", esc(fmtNum(weights.reinforce_count))],
      ["last reinforced", esc(fmtEpoch(weights.last_reinforced))],
      ["never decay", flagValue(weights.never_decay)],
    ])}
    <h3>flags</h3>
    ${kvList([
      ["conflict", flagValue(flags.conflict_flag)],
      ["conflict group", esc(flags.conflict_group || "—")],
      ["needs reconcile", flagValue(flags.needs_reconcile)],
      ["pending consolidation", flagValue(flags.pending_consolidation)],
      ["peripheral gaps", flagValue(flags.peripheral_gaps)],
    ])}
    <h3>usage</h3>
    ${kvList([
      ["hit count", esc(fmtNum(usage.hit_count))],
      ["last hit at", esc(fmtEpoch(usage.last_hit_at))],
    ])}
    <h3>promotion</h3>
    ${kvList([["status", esc(dossier.promotion_status || "—")]])}
    <h3>version</h3>
    ${kvList([
      ["number", esc(String(version.number ?? "—")) + (version.current ? ' <span class="badge badge-ok">current</span>' : "")],
      ["prev version", version.prev_version_id ? `<span class="mono">${esc(version.prev_version_id.slice(0, 12))}</span>` : '<span class="dim">—</span>'],
      ["valid from", esc(fmtEpoch(version.valid_from))],
      ["valid to", version.valid_to ? esc(fmtEpoch(version.valid_to)) : '<span class="badge badge-ok">now</span>'],
    ])}
    <h3>version chain</h3>
    ${versionChainHtml(dossier.version_chain)}
    ${dossier.timeline && dossier.timeline.length ? `<h3>timeline</h3>${timelineHtml(dossier.timeline)}` : ""}
    ${kvList([
      ["created at", esc(fmtEpoch(dossier.created_at))],
      ["updated at", esc(fmtEpoch(dossier.updated_at))],
    ])}
  </div>`;
}

function versionChainHtml(chain) {
  if (!chain || !chain.length) {
    return '<p class="dim">No version revisions recorded (current version only).</p>';
  }
  const sorted = [...chain].sort((a, b) => (a.version || 0) - (b.version || 0));
  const rows = sorted
    .map((version) => {
      const stmt = version.props && version.props.statement;
      return `<div class="kv">
        <span class="kv-label">v${esc(String(version.version))}${version.valid_to == null ? ' <span class="badge badge-ok">current</span>' : ""}</span>
        <span class="kv-value">${esc(stmt || JSON.stringify(version.props || {}))}</span>
      </div>`;
    })
    .join("");
  const diffs = [];
  for (let index = 1; index < sorted.length; index += 1) {
    const before = (sorted[index - 1].props && sorted[index - 1].props.statement) || "";
    const after = (sorted[index].props && sorted[index].props.statement) || "";
    if (before !== after) {
      diffs.push(
        `<h3>v${sorted[index - 1].version} → v${sorted[index].version}</h3><div class="diff">${diffWords(before, after)}</div>`,
      );
    }
  }
  if (!diffs.length) {
    diffs.push('<p class="dim">No statement changes between recorded versions.</p>');
  }
  return rows + diffs.join("");
}

function timelineHtml(events) {
  const rows = events
    .map(
      (event) => `<tr>
        <td>${fmtEpoch(event.when)}</td>
        <td class="mono">${esc(String(event.version ?? ""))}</td>
        <td>${esc(event.summary ?? "")}</td>
      </tr>`,
    )
    .join("");
  return `<div class="table-wrap"><table>
    <thead><tr><th>when</th><th>version</th><th>summary</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function provenanceCard(provenance) {
  const provenanceInfo = provenance || {};
  return `<div class="card">
    <h2>provenance</h2>
    ${kvList([
      ["asserted by", esc(provenanceInfo.asserted_by || "—")],
      ["agent id", esc(provenanceInfo.agent_id || "—")],
      ["session id", esc(provenanceInfo.session_id || "—")],
      ["source", esc(provenanceInfo.source || "—")],
      ["confidence", esc(String(provenanceInfo.confidence ?? "—"))],
      ["asserted at", esc(fmtEpoch(provenanceInfo.asserted_at))],
    ])}
    <h3>history timeline</h3>
    ${provenanceHistoryHtml(provenanceInfo.history)}
  </div>`;
}

function provenanceHistoryHtml(events) {
  if (!events || !events.length) return '<p class="dim">No provenance history recorded.</p>';
  const rows = events
    .map(
      (event) => `<tr>
        <td>${fmtEpoch(event.at)}</td>
        <td><span class="badge badge-accent">${esc(event.action)}</span></td>
        <td>${esc(event.actor || "—")}</td>
        <td>${detailCell(event.detail)}</td>
      </tr>`,
    )
    .join("");
  return `<div class="table-wrap"><table>
    <thead><tr><th>at</th><th>action</th><th>actor</th><th>detail</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

// ---------------------------------------------------------------- memory writes (FR-7.9 / G-AC1)

// Forget is confirm-twice on the button itself: the first click arms it (the
// label flips to a warning), the second click fires the POST. An unarmed click
// never erases anything.
async function forgetDetail(type, id) {
  const view = document.getElementById("view");
  const payload = { profile_id: state.profileId };
  if (type === "node") payload.node_id = id;
  else payload.chunk_id = id;
  try {
    await api("/api/v1/forget", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.browseFlash = `forgot ${type} ${id} (audited)`;
    location.hash = "#/browse";
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`forget failed: ${error.message}`));
  }
}

async function togglePin(id, pinned) {
  const view = document.getElementById("view");
  try {
    const result = await api("/api/v1/pin", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: state.profileId, node_id: id, pinned }),
    });
    state.detailFlash = result.changed
      ? `pin ${result.never_decay ? "on" : "off"} — version ${result.version} (audited)`
      : `already ${pinned ? "pinned" : "unpinned"} — nothing changed`;
    loadDetail("node", id);
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`pin failed: ${error.message}`));
  }
}

async function adjustWeight(form) {
  const view = document.getElementById("view");
  const feedback = form.querySelector("[data-weight-feedback]");
  const kind = form.dataset.kind;
  const id = form.dataset.id;
  const raw = String(new FormData(form).get("decay_weight") || "").trim();
  const weight = Number(raw);
  if (!Number.isFinite(weight) || weight < 0 || weight > 1) {
    if (feedback) feedback.innerHTML = errorInline("decay weight must be a number between 0 and 1");
    return;
  }
  if (feedback) feedback.innerHTML = '<span class="dim">applying…</span>';
  try {
    const result = await api("/api/v1/weights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        profile_id: state.profileId,
        kind,
        target_id: id,
        decay_weight: weight,
      }),
    });
    state.detailFlash = `decay weight ${result.old_decay_weight} → ${result.decay_weight} (audited)`;
    loadDetail(kind === "node" ? "node" : "chunk", id);
  } catch (error) {
    if (feedback) feedback.innerHTML = errorInline(`weight adjust failed: ${error.message}`);
  }
}

// ---------------------------------------------------------------- profiles (FR-7.3)

async function loadProfiles() {
  const view = document.getElementById("view");
  try {
    await ensureProfile();
  } catch (_err) {
    /* fall through to the empty state */
  }
  try {
    const status = await api("/api/v1/status");
    state.dashboard = status;
    state.profiles = status.profiles || [];
    syncProfileFromStatus();
    renderProfilePicker();
    view.innerHTML = profilesHtml(state.profiles);
    setUpdatedAt();
  } catch (error) {
    view.innerHTML = errorPanel(`Profiles request failed: ${error.message}`);
  }
}

function profilesHtml(rows) {
  const toolbar = `<div class="toolbar">
    <button class="btn" data-act="go-home">← dashboard</button>
    <button class="btn" data-act="profiles-refresh">Refresh</button>
    <span class="spacer"></span>
    <span class="toolbar-note">profile writes are audited; token secrets are shown once</span>
  </div>`;
  const createCard = `<form class="card" data-profiles-create-form>
    <h2>create profile</h2>
    <div class="filter-grid">
      <div class="field"><label for="profile-id">profile id</label><input type="text" id="profile-id" name="profile_id" required pattern="[A-Za-z0-9._-]+" placeholder="e.g. work" autocomplete="off" /></div>
      <div class="field"><label for="profile-name">display name</label><input type="text" id="profile-name" name="display_name" placeholder="optional" autocomplete="off" /></div>
    </div>
    <div class="toolbar"><button class="btn btn-primary" type="submit">create</button></div>
    <output class="feedback" data-profiles-feedback></output>
  </form>`;
  const cards = rows.map((row) => profileAdminCard(row)).join("");
  return toolbar + createCard + (cards || emptyPanel("No profiles yet."));
}

function profileAdminCard(row) {
  const counts = row.counts || {};
  const archived = row.archived === true;
  const issuedTokens = Object.values(state.profilesPage.tokens).filter(
    (token) => token.profile_id === row.profile_id,
  );
  const tokenRows = issuedTokens.length
    ? issuedTokens
        .map(
          (token) => `<div class="resolve-row">
            <span class="mono">${esc(token.token_id.slice(0, 8))}…</span>
            <span class="badge badge-accent">${esc((token.scopes || []).join(", ") || "all")}</span>
            <span class="dim">${token.expires_at ? `expires ${esc(fmtEpoch(token.expires_at))}` : "no expiry"}</span>
            <span class="spacer"></span>
            <button class="btn" data-act="token-revoke" data-token-id="${esc(token.token_id)}">revoke</button>
          </div>`,
        )
        .join("")
    : '<p class="dim">No tokens issued this session — issue one below (the bearer secret shows once).</p>';
  return `<div class="card">
    <h2>${esc(row.profile_id)} ${esc(row.display_name || "")} ${archived ? '<span class="badge badge-warn">archived</span>' : ""}</h2>
    <div class="tiles">
      ${tile(fmtNum(counts.chunks), "chunks")}
      ${tile(fmtNum(counts.nodes), "nodes")}
      ${tile(fmtNum(counts.needs_reconcile), "needs reconcile")}
      ${tile(fmtNum(counts.pending_consolidation), "pending consolidation")}
    </div>
    <h3>manage</h3>
    <div class="toolbar">
      <form data-profile-rename-form data-profile-id="${esc(row.profile_id)}">
        <input type="text" name="display_name" value="${esc(row.display_name || "")}" placeholder="display name" autocomplete="off" />
        <button class="btn" type="submit">rename</button>
      </form>
      <span class="spacer"></span>
      <button class="btn" data-act="profile-archive" data-profile-id="${esc(row.profile_id)}" data-archived="${archived ? "false" : "true"}">${archived ? "unarchive" : "archive"}</button>
      <button class="btn btn-primary" data-act="token-issue" data-profile-id="${esc(row.profile_id)}">issue token</button>
    </div>
    <output class="feedback" data-token-issue data-profile-id="${esc(row.profile_id)}"></output>
    <h3>session tokens</h3>
    ${tokenRows}
  </div>`;
}

async function createProfile(form) {
  const feedback = form.querySelector("[data-profiles-feedback]");
  const data = new FormData(form);
  const profile_id = String(data.get("profile_id") || "").trim();
  const display_name = String(data.get("display_name") || "").trim();
  if (!profile_id) {
    if (feedback) feedback.innerHTML = errorInline("profile id is required");
    return;
  }
  if (feedback) feedback.innerHTML = '<span class="dim">creating…</span>';
  try {
    await api("/api/v1/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id, display_name }),
    });
    await loadProfiles();
  } catch (error) {
    if (feedback) feedback.innerHTML = errorInline(`create failed: ${error.message}`);
  }
}

async function renameProfile(form) {
  const view = document.getElementById("view");
  const profile_id = form.dataset.profileId;
  const display_name = String(new FormData(form).get("display_name") || "").trim();
  try {
    await api(`/api/v1/profiles/${encodeURIComponent(profile_id)}/rename`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name }),
    });
    await loadProfiles();
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`rename failed: ${error.message}`));
  }
}

async function toggleArchive(profile_id, archived) {
  const view = document.getElementById("view");
  try {
    await api(`/api/v1/profiles/${encodeURIComponent(profile_id)}/archive`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archived }),
    });
    await loadProfiles();
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`archive update failed: ${error.message}`));
  }
}

async function issueToken(profile_id) {
  const view = document.getElementById("view");
  const output = view ? view.querySelector(`[data-token-issue][data-profile-id="${profile_id}"]`) : null;
  if (output) output.innerHTML = '<span class="dim">issuing…</span>';
  try {
    const result = await api(`/api/v1/profiles/${encodeURIComponent(profile_id)}/tokens`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    // The bearer secret rides back exactly once and is never stored client-side
    // (not in state, not in localStorage); the token record above it is kept
    // for the session's revoke buttons.
    state.profilesPage.tokens[result.token_id] = {
      profile_id: result.profile_id,
      scopes: result.scopes || [],
      expires_at: result.expires_at,
    };
    if (output) {
      output.innerHTML = `<div class="ok-inline"><strong>token issued — copy it now, it will never be shown again:</strong>
        <div class="mono" style="overflow-wrap:anywhere">${esc(result.token_secret)}</div>
        <button class="btn" data-act="token-copy" data-secret="${esc(result.token_secret)}">copy</button></div>`;
    }
  } catch (error) {
    if (output) output.innerHTML = errorInline(`token issue failed: ${error.message}`);
  }
}

async function copyToken(secret) {
  try {
    await navigator.clipboard.writeText(secret);
  } catch (_err) {
    const view = document.getElementById("view");
    if (view) view.insertAdjacentHTML("beforeend", errorInline("clipboard unavailable — select the secret manually"));
  }
}

async function revokeToken(token_id) {
  const view = document.getElementById("view");
  try {
    await api(`/api/v1/tokens/${encodeURIComponent(token_id)}/revoke`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    delete state.profilesPage.tokens[token_id];
    await loadProfiles();
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`revoke failed: ${error.message}`));
  }
}

// ---------------------------------------------------------------- settings (FR-7.11 / G-AC3)

async function loadSettings() {
  const view = document.getElementById("view");
  try {
    const loaded = await Promise.all([
      api("/api/v1/config"),
      api("/api/v1/config/versions"),
      api("/api/v1/status"),
    ]);
    state.settings.config = loaded[0] && loaded[0].config;
    state.settings.versions = loaded[1] && loaded[1].versions;
    state.dashboard = loaded[2];
    state.profiles = (loaded[2] && loaded[2].profiles) || [];
    syncProfileFromStatus();
    renderProfilePicker();
    view.innerHTML = settingsHtml();
    setUpdatedAt();
  } catch (error) {
    state.settings.config = null;
    state.settings.versions = null;
    view.innerHTML = errorPanel(`Settings request failed: ${error.message}`);
  }
}

function configLeaf(config, path) {
  let node = config;
  for (const part of path.split(".")) {
    if (!node || typeof node !== "object") return undefined;
    node = node[part];
  }
  return node;
}

function restartRequiredBadge(config) {
  const rr = config && config.restart_required;
  const names = Object.keys(rr || {}).filter((key) => rr[key] === true);
  if (!names.length) return "";
  return `<span class="badge badge-warn" title="${esc(names.join(", "))}">restart required</span>`;
}

function settingsHtml() {
  const config = state.settings.config || {};
  const banner = state.settings.message
    ? `<div class="card"><span class="ok-inline">${esc(state.settings.message)}</span></div>`
    : "";
  state.settings.message = null;
  const toolbar = `<div class="toolbar">
    <button class="btn" data-act="go-home">← dashboard</button>
    <button class="btn" data-act="settings-refresh">Refresh</button>
    <span class="spacer"></span>
    ${restartRequiredBadge(config)}
    <span class="toolbar-note">every change is audited and versioned</span>
  </div>`;
  const dream = config.dream || {};
  const autoTrigger = dream.auto_trigger === true;
  const budget = dream.token_budget_usd;
  const editable = `<form class="card" data-settings-form>
    <h2>dream engine</h2>
    <div class="filter-grid">
      <div class="field">
        <label for="settings-auto-trigger">auto-trigger dream</label>
        <input type="checkbox" id="settings-auto-trigger" name="dream.auto_trigger" ${autoTrigger ? "checked" : ""} />
      </div>
      <div class="field">
        <label for="settings-budget">token budget (USD per month)</label>
        <input type="number" id="settings-budget" name="dream.token_budget_usd" min="0" step="0.01" value="${esc(budget === null || budget === undefined ? "" : String(budget))}" placeholder="blank = no cap" autocomplete="off" />
      </div>
    </div>
    <div class="toolbar">
      <span class="toolbar-note">model routes and key env vars live on the Models page</span>
      <span class="spacer"></span>
      <button class="btn btn-primary" type="submit">save settings</button>
    </div>
    <output class="feedback" data-settings-feedback></output>
  </form>`;
  const storageState = profilesStorageState();
  const storage = `<div class="card">
    <h2>storage driver</h2>
    <p class="dim">Managed by the daemon from MNEMOSEED_HOME. The driver and vector index are
    selected at boot; switching requires a restart of the daemon.</p>
    ${storageState}
  </div>`;
  const versions = versionsTable(state.settings.versions);
  return banner + toolbar + editable + storage + versions;
}

function profilesStorageState() {
  const rows = state.profiles || [];
  const anyData = rows.some((row) => (row.counts && (row.counts.chunks > 0 || row.counts.nodes > 0)) || row.needs_reconcile > 0);
  return `<p><span class="badge ${anyData ? "badge-accent" : ""}">${anyData ? "in use — switch requires a full re-sync" : "empty — safe to switch at next boot"}</span></p>
  <div class="tiles">${rows.map((row) => tile(fmtNum(row.counts ? row.counts.chunks : 0), `${row.profile_id} chunks`)).join("")}</div>`;
}

function versionsTable(versions) {
  if (!versions || !versions.length) return '<div class="card"><h2>config versions</h2><p class="dim">No versions recorded yet.</p></div>';
  const rows = versions
    .map(
      (version) => `<tr>
        <td><span class="mono">${esc(version.version_id)}</span></td>
        <td><span class="mono">${esc(version.key)}</span></td>
        <td>v${esc(version.version)}</td>
        <td>${fmtEpoch(version.updated_at)}</td>
        <td>${detailCell(version.value)}</td>
        <td><button class="btn" data-act="config-rollback" data-version-id="${esc(version.version_id)}" title="restore this version's config (audited)">rollback</button></td>
      </tr>`,
    )
    .join("");
  return `<div class="card"><h2>config versions</h2>
    <div class="table-wrap"><table>
      <thead><tr><th>version</th><th>key</th><th>#</th><th>at</th><th>value</th><th></th></tr></thead>
      <tbody>${rows}</tbody>
    </table></div></div>`;
}

async function saveSettings(form) {
  const feedback = form.querySelector("[data-settings-feedback]");
  const data = new FormData(form);
  const autoTrigger = data.get("dream.auto_trigger") !== null;
  const rawBudget = String(data.get("dream.token_budget_usd") || "").trim();
  let budget = null;
  if (rawBudget) {
    budget = Number(rawBudget);
    if (!Number.isFinite(budget) || budget < 0) {
      if (feedback) feedback.innerHTML = errorInline("token budget must be a non-negative number, or blank");
      return;
    }
  }
  if (feedback) feedback.innerHTML = '<span class="dim">saving…</span>';
  try {
    const writes = [];
    if (configLeaf(state.settings.config, "dream.auto_trigger") !== autoTrigger) {
      writes.push({ key_path: "dream.auto_trigger", value: autoTrigger });
    }
    if ((configLeaf(state.settings.config, "dream.token_budget_usd") ?? null) !== budget) {
      writes.push({ key_path: "dream.token_budget_usd", value: budget });
    }
    if (!writes.length) {
      if (feedback) feedback.innerHTML = '<span class="ok-inline">no changes</span>';
      return;
    }
    let lastVersion = null;
    let restartRequired = false;
    for (const set of writes) {
      const result = await api("/api/v1/config/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(set),
      });
      lastVersion = result.version_id;
      if (result.restart_required === true) restartRequired = true;
    }
    state.settings.message = `settings saved — config version ${lastVersion} (audited)${restartRequired ? "; restart required" : ""}`;
    await loadSettings();
  } catch (error) {
    if (feedback) feedback.innerHTML = errorInline(`save failed: ${error.message}`);
  }
}

async function rollbackConfig(version_id) {
  const view = document.getElementById("view");
  try {
    const result = await api("/api/v1/config/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ version_id }),
    });
    state.settings.message = `rolled back to version ${result.version_id} — restored ${result.restored} (audited)`;
    await loadSettings();
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`rollback failed: ${error.message}`));
  }
}

// ---------------------------------------------------------------- audit log (⑩)

async function loadAudit() {
  const view = document.getElementById("view");
  const query = new URLSearchParams();
  if (state.audit.filters.actor) query.set("actor", state.audit.filters.actor);
  if (state.audit.filters.action) query.set("action", state.audit.filters.action);
  if (state.audit.filters.since) query.set("since", String(state.audit.filters.since));
  if (state.audit.offset > 0) query.set("offset", String(state.audit.offset));
  query.set("limit", String(state.audit.limit));
  const suffix = query.toString();
  try {
    const result = await api(`/api/v1/audit${suffix ? `?${suffix}` : ""}`);
    state.audit.data = result;
    view.innerHTML = auditHtml(result);
    setUpdatedAt();
  } catch (error) {
    view.innerHTML = errorPanel(`Audit request failed: ${error.message}`);
  }
}

function auditHtml(result) {
  const entries = (result && result.items) || [];
  const paging = result && result.paging;
  const toolbar = `<div class="toolbar">
    <button class="btn" data-act="go-home">← dashboard</button>
    <button class="btn" data-act="audit-refresh">Refresh</button>
    <button class="btn" data-act="audit-reset">reset filters</button>
    <span class="spacer"></span>
    <span class="toolbar-note">append-only provenance trail</span>
  </div>`;
  const filterForm = `<form class="card" data-audit-form>
    <h2>filters</h2>
    <div class="filter-grid">
      <div class="field"><label for="audit-actor">actor</label><input type="text" id="audit-actor" name="actor" value="${esc(state.audit.filters.actor || "")}" placeholder="console | cli | daemon" autocomplete="off" /></div>
      <div class="field"><label for="audit-action">action</label><input type="text" id="audit-action" name="action" value="${esc(state.audit.filters.action || "")}" placeholder="e.g. capture" autocomplete="off" /></div>
      <div class="field"><label for="audit-since">since</label><input type="datetime-local" id="audit-since" name="since" value="${esc(auditSinceInput())}" /></div>
    </div>
    <div class="toolbar"><button class="btn btn-primary" type="submit">apply filters</button></div>
    <output class="feedback" data-audit-feedback></output>
  </form>`;
  const table = entries.length
    ? `<div class="card"><div class="table-wrap"><table>
        <thead><tr><th>at</th><th>actor</th><th>action</th><th>detail</th></tr></thead>
        <tbody>${entries
          .map(
            (entry) => `<tr>
              <td>${fmtEpoch(entry.at)}</td>
              <td>${esc(entry.actor || "—")}</td>
              <td><span class="badge badge-accent">${esc(entry.action)}</span></td>
              <td>${detailCell(entry.detail)}</td>
            </tr>`,
          )
          .join("")}</tbody>
      </table></div>${paginationBar(paging, entries.length, "audit-page")}</div>`
    : emptyPanel("No audit entries match the current filters.");
  return toolbar + filterForm + table;
}

// datetime-local (YYYY-MM-DDTHH:MM) <-> epoch seconds, matching the audit `since` filter.
function auditSinceInput() {
  const since = state.audit.filters.since;
  if (!since) return "";
  const date = new Date(since * 1000);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (value) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

async function applyAuditFilters(form) {
  const data = new FormData(form);
  const rawSince = String(data.get("since") || "").trim();
  let since = null;
  if (rawSince) {
    const epoch = Date.parse(rawSince) / 1000;
    if (Number.isNaN(epoch)) {
      const feedback = form.querySelector("[data-audit-feedback]");
      if (feedback) feedback.innerHTML = errorInline("invalid since value");
      return;
    }
    since = Math.floor(epoch);
  }
  state.audit.filters.actor = String(data.get("actor") || "").trim() || undefined;
  state.audit.filters.action = String(data.get("action") || "").trim() || undefined;
  state.audit.filters.since = since;
  state.audit.offset = 0;
  await loadAudit();
}

// ---------------------------------------------------------------- dream writes
async function dreamOnce() {
  const view = document.getElementById("view");
  if (!state.profileId || !view) return;
  try {
    const result = await api("/api/v1/dream/once", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile_id: state.profileId }),
    });
    await loadDashboardNow();
    const live = document.getElementById("view");
    if (live) {
      live.insertAdjacentHTML(
        "afterbegin",
        `<div class="card"><h2>dream once</h2>${kvList([
          ["launched", result.launched ? '<span class="badge badge-ok">yes</span>' : '<span class="badge badge-warn">no — nothing pending</span>'],
        ])}</div>`,
      );
    }
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`dream_once failed: ${error.message}`));
  }
}

async function setAutoTrigger(enabled) {
  try {
    await api("/api/v1/dream/auto_trigger", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    await loadDashboardNow();
  } catch (error) {
    const view = document.getElementById("view");
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`auto-trigger update failed: ${error.message}`));
    setTimeout(loadDashboardNow, 800); // restore the switch to the server truth
  }
}

async function loadDashboardNow() {
  await loadDashboard();
  scheduleAutoRefresh();
}

// ---------------------------------------------------------------- dream review (FR-7.6)
async function loadReview() {
  const view = document.getElementById("view");
  try {
    await ensureProfile();
  } catch (_err) {
    /* fall through to the empty state */
  }
  if (!state.profileId) {
    if (view) view.innerHTML = reviewShellHtml(null, null);
    return;
  }
  try {
    const runsBody = await api("/api/v1/dream/runs?limit=1");
    const run = (runsBody.runs || [])[0] || null;
    if (!run) {
      state.review.runId = null;
      state.review.data = null;
      if (view) view.innerHTML = reviewShellHtml(run, null);
      return;
    }
    const review = await api(
      `/api/v1/dream/review/${encodeURIComponent(run.run_id)}?profile_id=${encodeURIComponent(state.profileId)}`,
    );
    state.review.runId = run.run_id;
    state.review.data = review;
    if (view) view.innerHTML = reviewShellHtml(run, review);
    setUpdatedAt();
  } catch (error) {
    state.review.data = null;
    if (view) view.innerHTML = errorPanel(`Review request failed: ${error.message}`);
  }
}

function reviewShellHtml(run, review) {
  const toolbar = `<div class="toolbar">
    <button class="btn" data-act="go-home">← dashboard</button>
    <button class="btn" data-act="review-refresh" ${state.profileId ? "" : "disabled"}>Refresh</button>
    <span class="spacer"></span>
    <span class="toolbar-note">every verdict is written to the audit log</span>
  </div>`;
  if (!run) {
    return toolbar + `<div class="card"><h2>dream quality review</h2>${emptyPanel("No dream runs recorded yet — Dream once on the dashboard, then review its distilled triples against the source chunks here.")}</div>`;
  }
  const head = `<div class="card">
    <h2>dream quality review</h2>
    ${kvList([
      ["run", `<span class="mono">${esc(run.run_id)}</span>`],
      ["turn range", esc(fmtRange(run.turn_range))],
      ["started", esc(fmtEpoch(run.started_at))],
      ["model", esc(run.model_id || "—")],
    ])}
  </div>`;
  if (!review || review.reflected !== true) {
    return toolbar + head + `<div class="card"><h2>distilled triples</h2>${emptyPanel("This run produced no reviewable triples.")}</div>`;
  }
  const triples = (review.triples || []);
  const cards = triples.map((triple, index) => tripleReviewCard(triple, index)).join("");
  return toolbar + head + `<div class="card"><h2>distilled triples · ${fmtNum(triples.length)}</h2><p class="toolbar-note">each triple sits beside the verbatim source chunks it was distilled from — accept what held, reject what misled, flag what was invented.</p>${cards || emptyPanel("No triples distilled in this run.")}</div>`;
}

function tripleReviewCard(triple, index) {
  const chunks = (triple.chunks || []).map(
    (chunk) => `<div class="source-chunk"><div class="row-title">${esc(chunk.text)}</div><div class="chunk-id mono">${esc(chunk.chunk_id)}</div></div>`,
  ).join("");
  const verdict = triple.verdict;
  const controls = verdict
    ? `${VERDICT_BADGE[verdict.action] || esc(verdict.action)} <span class="dim">recorded · ${esc(fmtEpoch(verdict.at))}</span>`
    : REVIEW_VERDICTS.map(
        (v) =>
          `<button class="btn btn-primary" data-act="review-verdict" data-index="${index}" data-verdict="${v.value}" title="record ${esc(v.value)} verdict">${esc(v.label)}</button>`,
      ).join("");
  return `<div class="triple-review">
    <div class="trip-line"><span class="triple-term s">${esc(triple.subject)}</span> → <span class="triple-term p">${esc(triple.predicate)}</span> → <span class="triple-term o">${esc(triple.object)}</span></div>
    <div class="row-meta">route ${esc(triple.route)} · confidence ${fmtNum(triple.confidence)} · preference ${triple.preference ? "yes" : "no"} · polarity ${esc(triple.polarity)}</div>
    ${chunks}
    <div class="verdict-row">${controls}</div>
  </div>`;
}

async function submitReviewVerdict(runId, triple, verdict) {
  const view = document.getElementById("view");
  try {
    await api("/api/v1/dream/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_id: runId,
        profile_id: state.profileId,
        subject: triple.subject,
        predicate: triple.predicate,
        object: triple.object,
        route: triple.route,
        verdict,
      }),
    });
    await loadReview();
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`verdict failed: ${error.message}`));
  }
}

// ---------------------------------------------------------------- conflicts inbox (FR-7.7)
async function loadConflicts() {
  const view = document.getElementById("view");
  try {
    await ensureProfile();
  } catch (_err) {
    /* fall through to the empty state */
  }
  if (!state.profileId) {
    if (view) view.innerHTML = conflictsShellHtml(null);
    return;
  }
  try {
    const body = await api(`/api/v1/conflicts?profile_id=${encodeURIComponent(state.profileId)}`);
    state.conflicts.data = body;
    if (view) view.innerHTML = conflictsShellHtml(body);
    setUpdatedAt();
  } catch (error) {
    state.conflicts.data = null;
    if (view) view.innerHTML = errorPanel(`Conflicts request failed: ${error.message}`);
  }
}

function conflictsShellHtml(body) {
  const toolbar = `<div class="toolbar">
    <button class="btn" data-act="go-home">← dashboard</button>
    <button class="btn" data-act="conflict-refresh" ${state.profileId ? "" : "disabled"}>Refresh</button>
    <span class="spacer"></span>
    <span class="toolbar-note">resolution writes the version chain — reconciled, never erased</span>
  </div>`;
  if (!body) {
    return toolbar + `<div class="card"><h2>conflicts inbox</h2>${emptyPanel("No profile available yet — contradictions surface here per active profile.")}</div>`;
  }
  const groups = body.groups || [];
  if (!groups.length) {
    return toolbar + `<div class="card"><h2>conflicts inbox</h2>${emptyPanel("No conflict pairs in the inbox. When two memories land in the same conflict group, they appear here side-by-side for a reconcile decision.")}</div>`;
  }
  const cards = groups.map((group, index) => conflictGroupCard(group, index)).join("");
  return toolbar + `<div class="card"><h2>conflicts inbox · ${fmtNum(groups.length)} pair${groups.length === 1 ? "" : "s"}</h2>${cards}</div>`;
}

function conflictGroupCard(group, index) {
  const sides = (group.sides || []).map((side) => conflictSideCard(side, index)).join("");
  const branches = CONFLICT_BRANCHES.map(
    (branch) => `<option value="${branch.value}">${esc(branch.label)}</option>`,
  ).join("");
  return `<div class="conflict-group">
    <h3>conflict group <span class="mono">${esc(group.group_id)}</span></h3>
    <div class="conflict-sides">${sides}</div>
    <h4>resolve</h4>
    <div class="resolve-row">
      <select data-resolution-branch data-index="${index}" aria-label="resolution branch">${branches}</select>
      <input type="text" data-resolution-scope data-index="${index}" hidden placeholder="context where each side holds, e.g. 'go projects use tabs, python uses spaces'" />
      <button class="btn btn-primary" data-act="conflict-resolve" data-index="${index}">resolve</button>
    </div>
    <p class="resolve-note" data-scope-note data-index="${index}">reinforce / invalidate pick one side; coexist needs a scope annotation.</p>
  </div>`;
}

function conflictSideCard(side, index) {
  const provenance = side.provenance || {};
  return `<div class="conflict-side">
    <label class="check-row side-pick"><input type="radio" name="conflict-side-${index}" value="${esc(side.node_id)}" /> <span><strong>${esc(side.statement)}</strong></span></label>
    <div class="row-meta">${decayMeter(side.decay_weight)} · confidence ${fmtNum(side.confidence)} · ${fmtNum(side.reinforce_count)} reinforcement${side.reinforce_count === 1 ? "" : "s"} · v${fmtNum(side.version)}</div>
    ${kvList([
      ["domain", esc(side.domain || "—")],
      ["scope", esc(side.scope || "—")],
      ["entities", badgeList(side.entities)],
      ["asserted by", esc(provenance.asserted_by || "—")],
      ["source", esc(provenance.source || "—")],
      ["asserted at", esc(fmtEpoch(provenance.asserted_at))],
    ])}
  </div>`;
}

async function resolveConflict(index) {
  const view = document.getElementById("view");
  const groups = (state.conflicts.data && state.conflicts.data.groups) || [];
  const group = groups[index];
  if (!group) return;
  const branchSelect = document.querySelector(`[data-resolution-branch][data-index="${index}"]`);
  const branch = branchSelect ? branchSelect.value : "pending";
  const scopeInput = document.querySelector(`[data-resolution-scope][data-index="${index}"]`);
  const scope = scopeInput ? String(scopeInput.value || "").trim() : "";
  let nodeId = null;
  if (branch === "reinforce" || branch === "invalidate") {
    const picked = document.querySelector(`input[name="conflict-side-${index}"]:checked`);
    if (!picked || !picked.value) {
      if (view) view.insertAdjacentHTML("beforeend", errorInline("pick the side to reinforce or invalidate first."));
      return;
    }
    nodeId = picked.value;
  }
  const payload = { profile_id: state.profileId, branch };
  if (nodeId) payload.node_id = nodeId;
  if (branch === "coexist") payload.scope = scope;
  try {
    const result = await api(`/api/v1/conflicts/${encodeURIComponent(group.group_id)}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    await loadConflicts();
    const live = document.getElementById("view");
    if (live) {
      const outcome = result.already_resolved ? "already resolved earlier" : `resolved · ${esc(result.branch)}`;
      const written = Array.isArray(result.written) && result.written.length
        ? `<span class="mono">${esc(result.written.join(", "))}</span>`
        : '<span class="dim">none</span>';
      live.insertAdjacentHTML(
        "afterbegin",
        `<div class="card"><h2>resolution</h2>${kvList([
          ["group", `<span class="mono">${esc(group.group_id)}</span>`],
          ["branch", esc(result.branch)],
          ["outcome", esc(outcome)],
          ["written", written],
        ])}</div>`,
      );
    }
  } catch (error) {
    if (view) view.insertAdjacentHTML("beforeend", errorInline(`resolve failed: ${error.message}`));
  }
}

// ---------------------------------------------------------------- models & routing (FR-6.9)
async function loadLLM() {
  const view = document.getElementById("view");
  try {
    const loaded = await Promise.all([
      api("/api/v1/llm/routes"),
      api("/api/v1/llm/oauth-availability"),
      api("/api/v1/config"),
    ]);
    state.llm.routes = loaded[0];
    state.llm.oauth = loaded[1];
    // Resolved config carries each role's max_tokens (the routes payload does
    // not); both stay env-var NAMES only.
    state.llm.config = (loaded[2] && loaded[2].config) || null;
    if (view) view.innerHTML = llmShellHtml(loaded[0], loaded[1], state.llm.message);
    state.llm.message = null;
    setUpdatedAt();
  } catch (error) {
    state.llm.routes = null;
    state.llm.oauth = null;
    state.llm.config = null;
    if (view) view.innerHTML = errorPanel(`Models request failed: ${error.message}`);
  }
}

function llmShellHtml(routes, oauth, message) {
  const banner = message
    ? `<div class="card"><h2>models &amp; routing</h2><span class="ok-inline">${esc(message)}</span></div>`
    : "";
  const drivers = routes.drivers || [];
  const roles = routes.roles || [];
  const offline = isFullyOffline(roles);
  return `${banner}
    <div class="toolbar">
      <button class="btn" data-act="go-home">← dashboard</button>
      <button class="btn" data-act="llm-refresh">Refresh</button>
      <span class="spacer"></span>
      <span class="toolbar-note">What each role does, and which model serves it. Key values never appear here — only the env-var names MnemoSeed reads them from.</span>
    </div>
    <div class="card">
      <h2>models &amp; routing</h2>
      ${offline ? '<p><span class="badge badge-ok">fully offline — nothing leaves this machine</span></p>' : ""}
      <p class="toolbar-note">Model routing is system-scoped — set by the owner/admin and applies to every user.</p>
      ${oauthHintsHtml(oauth)}
      <h3>drivers</h3>
      <p class="badges">${drivers.length ? drivers.map((d) => `<span class="badge" title="${esc(d.description || "")}">${esc(d.name)}</span>`).join(" ") : '<span class="dim">none registered</span>'}</p>
    </div>
    ${roles.length ? roles.map((role) => llmRoleCard(role, drivers)).join("") : emptyPanel("No dream roles configured on this daemon.")}`;
}

function oauthHintsHtml(oauth) {
  const providers = (oauth && oauth.providers) || [];
  if (!providers.length) return "";
  const bits = providers
    .map((entry) => {
      const live = entry.present === true && entry.expired !== true;
      const state = live ? "logged in" : entry.present === true ? "expired" : "not detected";
      return `${esc(entry.provider)} — ${state}`;
    })
    .join(" · ");
  return `<p class="toolbar-note">host logins: ${bits}</p>`;
}

function llmDriverLabel(role) {
  if (role.driver === "oauth") return `oauth · ${esc(role.provider || "")}`;
  return esc(role.driver || "—");
}

function llmRoleCard(role, drivers) {
  const conn = role.connectivity || {};
  const ok = conn.ok === true;
  const editing = state.llm.editingRole === role.role;
  const subtitle = LLM_ROLE_SUBTITLES[role.role] || "";
  const baseUrl = llmEffectiveBaseUrl(role);
  const keyEnv = llmEffectiveKeyEnv(role);
  const roleFallback = LLM_ROLE_KEY_ENV[role.role] || "";
  const keyLine =
    roleFallback && roleFallback !== keyEnv ? `key: ${roleFallback} → ${keyEnv}` : `key: ${keyEnv || "—"}`;
  const probe = ok
    ? '<span class="badge badge-ok">connected</span>'
    : '<span class="badge badge-err">needs attention</span>';
  const modelShown =
    editing && state.llm.editModel[role.role] != null
      ? String(state.llm.editModel[role.role])
      : role.model || "—";
  return `<div class="card">
    <h2>${esc(role.role)} ${!role.explicit ? '<span class="badge">defaults</span>' : ""}</h2>
    ${subtitle ? `<p class="toolbar-note">${esc(subtitle)}</p>` : ""}
    ${role.driver === "ollama" ? '<p class="toolbar-note">lower synthesis quality than cloud models — you accept this for privacy or cost.</p>' : ""}
    <div class="tiles">
      ${tile(`<span class="mono">${llmDriverLabel(role)}</span>`, "driver")}
      <div class="tile"><div class="tile-value" data-model-tile data-role="${esc(role.role)}"><span class="mono">${esc(modelShown)}</span></div><div class="tile-label">model</div></div>
      ${tile(esc(baseUrl || "default"), "endpoint")}
      ${tile(esc(keyLine), "api key env")}
      ${tile(esc(fmtNum(configRoleMaxTokens(role.role))), "max tokens")}
    </div>
    <div class="toolbar">
      <span>${probe}</span>
      <span class="dim">checked ${esc(fmtEpoch(conn.checked_at))}</span>
      <span class="spacer"></span>
      <button class="btn" data-act="llm-test" data-role="${esc(role.role)}" title="probe this saved route now">Test connection</button>
      <button class="btn" data-act="llm-edit" data-role="${esc(role.role)}" title="edit this route's config row">${editing ? "Cancel edit" : "Edit route"}</button>
    </div>
    <output class="feedback" data-llm-feedback data-feedback-role="${esc(role.role)}"></output>
    ${editing ? llmEditFormHtml(role, drivers) : ""}
  </div>`;
}

// The editor's model datalist is provider-scoped (§3.2/§7.2): curated ids for
// the provider currently picked in the form, plus any catalog a passing probe
// fetched for THAT provider (state.llm.catalog) — never the stale catalog of a
// previously saved route from another provider.
function llmEditorModelOptions(provider) {
  const curated = llmCuratedModels(provider);
  const seen = new Set(curated);
  const catalog = provider ? state.llm.catalog[provider.id] || [] : [];
  const extra = catalog.filter((model) => typeof model === "string" && !seen.has(model));
  return curated
    .concat(extra)
    .map((model) => `<option value="${esc(model)}"></option>`)
    .join("");
}

function llmEditorProviderCard(role, provider, activeProvider) {
  const selected = activeProvider && activeProvider.id === provider.id;
  return `<label class="wizard-provider-card ${selected ? "selected" : ""}">
    <input type="radio" name="llm-provider" value="${esc(provider.id)}" ${selected ? "checked" : ""} />
    <span class="wizard-provider-title">${esc(provider.label)}</span>
    <span class="toolbar-note">${esc(provider.note)}</span>
  </label>`;
}

// §6.3: in the editor "Reuse <login>" is a provider CARD per detected oauth
// provider, in every availability state — never a free-text provider field:
//   live    -> selectable card
//   expired -> visible but disabled, "login expired — run <cmd> first"
//   absent  -> visible but disabled, "no local <provider> CLI login detected"
function llmEditorOAuthCards(oauth, activeId) {
  const providers = (oauth && oauth.providers) || [];
  return providers
    .map((entry) => {
      const providerName = cap(entry.provider);
      const cardId = `oauth:${entry.provider}`;
      const live = entry.present === true && entry.expired !== true;
      const selected = activeId === cardId;
      const cmd = LLM_OAUTH_LOGIN_CMD[entry.provider] || `${entry.provider} login`;
      const note = live
        ? `No key needed — uses the ${providerName} login already on this machine.`
        : entry.present === true
          ? `login expired — run ${cmd} first`
          : `no local ${providerName} CLI login detected — log in first (${cmd})`;
      return `<label class="wizard-provider-card ${selected ? "selected" : ""} ${live ? "" : "muted"}">
        <input type="radio" name="llm-provider" value="${esc(cardId)}" ${selected ? "checked" : ""} ${live ? "" : "disabled"} />
        <span class="wizard-provider-title">Reuse ${providerName} login</span>
        <span class="toolbar-note">${esc(note)}</span>
      </label>`;
    })
    .join("");
}

// The dual path under the oauth cards: "paste a token instead" opens a key
// paste field bound to the host login currently picked in the form and calls
// the daemon's key endpoint (POST /api/v1/llm/key); the official docs link for
// the token rides along. Rendered whenever any host login is known to the
// daemon.
function llmOauthPasteHtml(oauth, activeId) {
  const providers = (oauth && oauth.providers) || [];
  if (!providers.length) return "";
  const picked = String(activeId).startsWith("oauth:")
    ? String(activeId).replace(/^oauth:/, "")
    : providers[0].provider;
  const entry = providers.find((candidate) => candidate.provider === picked) || providers[0];
  const providerName = cap(entry.provider);
  const docs = LLM_OAUTH_TOKEN_DOCS[entry.provider];
  const docsLink = docs
    ? `<a href="${esc(docs)}" target="_blank" rel="noopener noreferrer">official docs for ${esc(providerName)} tokens</a>`
    : "";
  return `<details class="key-teaching" data-oauth-paste data-provider="${esc(entry.provider)}">
    <summary>paste a token instead</summary>
    <div class="field">
      <label for="llm-oauth-token">API token for ${esc(providerName)}</label>
      <input type="password" id="llm-oauth-token" name="oauth_token" placeholder="paste the ${esc(providerName)} API token" autocomplete="off" />
    </div>
    <p class="toolbar-note">The token goes straight to the daemon — never into browser storage. ${docsLink}</p>
    <div class="toolbar">
      <button class="btn" type="button" data-act="llm-key-paste" data-provider="${esc(entry.provider)}">store token</button>
    </div>
    <output class="feedback" data-key-paste-feedback></output>
  </details>`;
}

// Keep the paste field bound to the host login currently picked in the form
// (or the first known host login when no oauth card is selected).
function llmBindOauthPaste(form, activeId) {
  if (!form) return;
  const paste = form.querySelector("[data-oauth-paste]");
  if (!paste) return;
  const providers = (state.llm.oauth && state.llm.oauth.providers) || [];
  if (!providers.length) return;
  const picked = String(activeId).startsWith("oauth:")
    ? String(activeId).replace(/^oauth:/, "")
    : providers[0].provider;
  const entry = providers.find((candidate) => candidate.provider === picked) || providers[0];
  paste.dataset.provider = entry.provider;
  const label = paste.querySelector("label[for='llm-oauth-token']");
  if (label) label.textContent = `API token for ${cap(entry.provider)}`;
  const input = paste.querySelector("#llm-oauth-token");
  if (input) input.placeholder = `paste the ${cap(entry.provider)} API token`;
  const button = paste.querySelector('[data-act="llm-key-paste"]');
  if (button) button.dataset.provider = entry.provider;
}

// Live role-card model tile: reflects the editor's current model while the
// route is being edited (provider pick morphs it), the saved route otherwise.
function updateModelTile(role) {
  const tile = document.querySelector(`[data-model-tile][data-role="${role}"]`);
  if (!tile) return;
  const value = state.llm.editModel[role];
  tile.textContent = value != null && String(value) !== "" ? String(value) : "—";
}

// The editor's per-route gate: while an expired/absent host login backs the
// selected oauth route, Test/Save/Load-model-list are disabled for THIS route
// only and a fix note is shown (availability refresh re-arms them).
function llmSyncEditorGate(form, activeId) {
  if (!form) return;
  const isOAuth = String(activeId).startsWith("oauth:");
  const oauthProvider = isOAuth ? String(activeId).replace(/^oauth:/, "") : "";
  const blocked = isOAuth && !llmOauthLive(oauthProvider);
  const driver = form.elements.driver ? String(form.elements.driver.value || "").trim() : "";
  const model = form.elements.model ? String(form.elements.model.value || "").trim() : "";
  const testBtn = form.querySelector('[data-act="llm-test-edit"]');
  const saveBtn = form.querySelector('button[type="submit"]');
  const loadBtn = form.querySelector('[data-act="llm-load-models"]');
  if (testBtn) testBtn.disabled = blocked;
  if (saveBtn) saveBtn.disabled = blocked;
  if (loadBtn) loadBtn.disabled = blocked || isOAuth || !driver || !model;
  const gateNote = form.querySelector("[data-llm-gate-note]");
  if (gateNote) {
    gateNote.hidden = !blocked;
    if (blocked) gateNote.textContent = `${llmOauthBlockMessage(oauthProvider)} — this route only; the other role is unaffected.`;
  }
}

async function llmKeyPaste(form, provider) {
  const output = form.querySelector("[data-key-paste-feedback]");
  const input = form.elements.oauth_token;
  const token = input ? String(input.value || "").trim() : "";
  if (!token) {
    if (output) output.innerHTML = errorInline("paste the token first");
    return;
  }
  const role = form.dataset.role || "";
  if (!role) {
    if (output) output.innerHTML = errorInline("no role bound to this editor");
    return;
  }
  if (output) output.innerHTML = '<span class="dim">storing token…</span>';
  try {
    await api("/api/v1/llm/key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role, key: token }),
    });
    if (input) input.value = "";
    if (output) output.innerHTML = '<span class="badge badge-ok">token stored</span>';
  } catch (error) {
    const cmd = LLM_OAUTH_LOGIN_CMD[provider] || `${provider} login`;
    if (output) {
      output.innerHTML = /HTTP (404|405|501|502)/.test(error.message)
        ? errorInline(`key paste is not available on this daemon yet — run ${cmd} instead`)
        : errorInline(`store failed: ${error.message}`);
    }
  }
}

// The provider card id currently in play for a role: a saved oauth route maps to
// its "oauth:<provider>" card, a driver/provider route to its matching card.
function llmActiveProviderId(role) {
  const route = findLLMRoute(role);
  if (!route) return "";
  if (route.driver === "oauth") return `oauth:${route.provider || ""}`;
  const provider = llmProviderFor(route.driver, route.provider);
  return provider ? provider.id : "";
}

// §3.2 morphing: the editor form's fields follow the picked provider card.
// ollama hides the key block; oauth mode hides key + endpoint; the residency
// note is "Other"-only. ``morphValues`` (user switched cards) also rewrites the
// field values to the provider defaults — including re-seeding the model with
// the picked provider's role-appropriate curated id (kills the "anthropic
// picked, kimi-k3 still shown" state) — and updates the live model tile; a
// plain re-render only enforces the oauth clearing so a save can never pin a
// key/endpoint to an oauth route.
function llmApplyEditorProvider(form, activeId, morphValues) {
  if (!form) return;
  const isOAuth = String(activeId).startsWith("oauth:");
  const provider = isOAuth ? null : llmProviderById(activeId);
  const role = form.dataset.role || "";
  const driverInput = form.elements.driver;
  if (driverInput) driverInput.value = isOAuth ? "oauth" : provider ? provider.driver : "";
  const needsKey = Boolean(provider && provider.keyEnv);
  const keyField = form.querySelector("[data-key-field]");
  const keyInput = form.elements.api_key_env;
  if (keyField) keyField.hidden = isOAuth || !needsKey;
  if (keyInput) {
    if (isOAuth || (morphValues && !needsKey)) keyInput.value = "";
    else if (morphValues && needsKey) keyInput.value = provider.keyEnv || LLM_ROLE_KEY_ENV[role] || "";
  }
  const endpointField = form.querySelector("[data-endpoint-field]");
  const urlInput = form.elements.base_url;
  if (endpointField) endpointField.hidden = isOAuth;
  if (isOAuth && urlInput) urlInput.value = "";
  else if (morphValues && urlInput) urlInput.value = provider ? provider.baseUrl || "" : "";
  const tokensField = form.querySelector("[data-tokens-field]");
  if (tokensField) tokensField.hidden = Boolean(provider && provider.driver === "ollama");
  const residency = form.querySelector("[data-residency-note]");
  if (residency) residency.hidden = !(provider && provider.id === "other");
  const teaching = form.querySelector("[data-key-teaching]");
  if (teaching) teaching.hidden = isOAuth || !needsKey;
  const modelInput = form.elements.model;
  if (morphValues && !isOAuth && provider && modelInput) {
    const seed = llmRoleDefaultModel(provider, role);
    if (seed) {
      modelInput.value = seed;
      state.llm.editModel[role] = seed;
    }
  }
}

// §5: the env-var teaching block under the key field (per-provider commands).
function llmKeyTeachingHtml(provider, role) {
  const keyEnv = provider.keyEnv || LLM_ROLE_KEY_ENV[role] || "";
  if (!keyEnv) return "";
  const where = provider.keyUrl ? `Create the key at ${provider.keyUrl}, then ` : "";
  return `<details class="key-teaching" data-key-teaching>
    <summary>Your key lives in an environment variable. MnemoSeed reads it from there — you never paste the key here and it is never stored.</summary>
    <p class="toolbar-note">${esc(where)}set ${esc(keyEnv)} — on macOS/Linux: export ${esc(keyEnv)}=…; on Windows: setx ${esc(keyEnv)} …. Remember: the daemon reads env vars from its own startup environment. If you set a new one, restart MnemoSeed.</p>
  </details>`;
}

function llmEditFormHtml(role, drivers) {
  const maxTokens = configRoleMaxTokens(role.role);
  const activeId = llmActiveProviderId(role);
  const isOAuth = String(activeId).startsWith("oauth:");
  const activeProvider = isOAuth ? null : llmProviderById(activeId);
  const baseUrl = llmEffectiveBaseUrl(role);
  const keyEnv = llmEffectiveKeyEnv(role);
  const roleFallback = LLM_ROLE_KEY_ENV[role.role] || "";
  const keyProvider = activeProvider && activeProvider.keyEnv ? activeProvider : null;
  const cards =
    LLM_PROVIDERS.map((provider) => llmEditorProviderCard(role, provider, activeProvider)).join("") +
    llmEditorOAuthCards(state.llm.oauth, activeId);
  const modelValue =
    state.llm.editModel[role.role] != null ? String(state.llm.editModel[role.role]) : role.model || "";
  return `<form class="card" data-llm-route-form data-role="${esc(role.role)}">
    <h3>Edit route — ${esc(role.role)}</h3>
    ${isOAuth ? `<p class="toolbar-note">This route uses the ${esc(role.provider || "host")} login on this machine — no key needed. Pick a provider below to change it.</p>` : ""}
    <input type="hidden" name="driver" value="${isOAuth ? "" : esc(role.driver)}" />
    <h4>Which provider?</h4>
    <div class="filter-grid">${cards}</div>
    ${llmOauthPasteHtml(state.llm.oauth, activeId)}
    <div class="filter-grid">
      <div class="field"><label for="llm-model-${esc(role.role)}">model</label>
        <input type="text" id="llm-model-${esc(role.role)}" name="model" list="llm-models-${esc(role.role)}" value="${esc(modelValue)}" placeholder="type or pick a model" required autocomplete="off" />
        <datalist id="llm-models-${esc(role.role)}">${llmEditorModelOptions(activeProvider)}</datalist>
        <div class="toolbar">
          <button class="btn" type="button" data-act="llm-load-models" data-role="${esc(role.role)}">Load model list</button>
          <span class="toolbar-note">Runs a connection probe to fetch the provider's model catalog.</span>
        </div>
      </div>
      <div class="field" data-endpoint-field><label for="llm-url-${esc(role.role)}">endpoint</label><input type="text" id="llm-url-${esc(role.role)}" name="base_url" value="${esc(baseUrl)}" placeholder="blank = provider default" autocomplete="off" /></div>
      <div class="field" data-key-field><label for="llm-env-${esc(role.role)}">api key env var</label><input type="text" id="llm-env-${esc(role.role)}" name="api_key_env" value="${esc(keyEnv)}" placeholder="${esc(roleFallback || "MY_API_KEY")}" autocomplete="off" />${keyProvider ? llmKeyTeachingHtml(keyProvider, role.role) : ""}</div>
      <div class="field" data-tokens-field><label for="llm-tokens-${esc(role.role)}">max tokens</label><input type="number" id="llm-tokens-${esc(role.role)}" name="max_tokens" value="${esc(maxTokens === null ? "" : String(maxTokens))}" min="1" placeholder="blank = role default" autocomplete="off" /></div>
    </div>
    <p class="toolbar-note" data-residency-note hidden>Your memories leave this machine to the provider's servers.</p>
    <p class="toolbar-note" data-llm-gate-note hidden></p>
    <div class="toolbar">
      <span class="toolbar-note">Remember: the daemon reads env vars from its own startup environment. If you set a new one, restart MnemoSeed.</span>
      <span class="spacer"></span>
      <button class="btn" type="button" data-act="llm-test-edit" data-role="${esc(role.role)}">Test connection</button>
      <button class="btn btn-primary" type="submit">Save route</button>
    </div>
    <output class="feedback" data-llm-feedback></output>
  </form>`;
}

function renderLLM() {
  const view = document.getElementById("view");
  if (!view) return;
  if (!state.llm.routes) {
    loadLLM();
    return;
  }
  view.innerHTML = llmShellHtml(state.llm.routes, state.llm.oauth, null);
  if (state.llm.editingRole) {
    const form = Array.from(view.querySelectorAll("[data-llm-route-form]")).find(
      (candidate) => candidate.dataset.role === state.llm.editingRole,
    );
    if (form) {
      const activeId = llmActiveProviderId(state.llm.editingRole);
      llmApplyEditorProvider(form, activeId, false);
      llmBindOauthPaste(form, activeId);
      llmSyncEditorGate(form, activeId);
    }
  }
  setUpdatedAt();
}

function findLLMRoute(role) {
  const routes = state.llm.routes;
  return routes && routes.roles ? routes.roles.find((entry) => entry.role === role) : null;
}

function configRoleMaxTokens(role) {
  const cfg = state.llm.config;
  if (!cfg || !cfg.dream || !cfg.dream.llm || !cfg.dream.llm[role]) return null;
  const value = cfg.dream.llm[role].max_tokens;
  return value === null || value === undefined || value === "" ? null : Number(value);
}

// The probe signature covers exactly the fields the connectivity probe sees;
// max_tokens does not affect reachability and deliberately does not gate a save.
function llmProbeSignature(driver, model, baseUrl, apiKeyEnv, provider) {
  return JSON.stringify([driver, model, baseUrl, apiKeyEnv, provider]);
}

async function testRoute(role, driver, model, baseUrl, apiKeyEnv, provider, feedbackEl) {
  if (!driver || !model) {
    if (feedbackEl) feedbackEl.innerHTML = errorInline("driver and model are required to probe");
    return;
  }
  // JH: a route whose host login is expired or absent is blocked until
  // availability returns — the probe never fires for THAT route, the fix
  // message shows instead (other routes are unaffected).
  if (driver === "oauth") {
    if (!llmOauthLive(provider)) {
      delete state.llm.probeOk[role];
      if (feedbackEl) feedbackEl.innerHTML = errorInline(llmOauthBlockMessage(provider));
      return;
    }
  }
  const payload = { role, driver, model, base_url: baseUrl || "" };
  if (apiKeyEnv && String(apiKeyEnv).trim()) payload.api_key_env = String(apiKeyEnv).trim();
  if (provider && String(provider).trim()) payload.provider = String(provider).trim();
  const providerMeta = llmProviderFor(driver, provider);
  const probeLabel = providerMeta ? providerMeta.label.replace(" (recommended)", "") : driver;
  if (feedbackEl) {
    feedbackEl.innerHTML = `<span class="dim">Testing connection to ${esc(probeLabel)}…</span>`;
  }
  try {
    const result = await api("/api/v1/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (result.ok) {
      // A passing probe for the exact current form values arms the save gate.
      state.llm.probeOk[role] = llmProbeSignature(driver, model, baseUrl || "", apiKeyEnv || "", provider || "");
      // §7.2: the model combobox catalog refreshes from the probe's models list
      // and is cached per provider (so a card switch back re-lists it).
      const models =
        result.detail && Array.isArray(result.detail.models)
          ? result.detail.models.filter((candidate) => typeof candidate === "string")
          : null;
      if (providerMeta && models && models.length) {
        state.llm.catalog[providerMeta.id] = models;
      }
      if (models && models.length) {
        const datalist = document.querySelector(`datalist[id="llm-models-${role}"]`);
        if (datalist) {
          datalist.innerHTML = models.map((candidate) => `<option value="${esc(candidate)}"></option>`).join("");
        }
      }
    } else {
      delete state.llm.probeOk[role];
    }
    if (feedbackEl) {
      feedbackEl.innerHTML = result.ok
        ? '<span class="badge badge-ok">connected</span>'
        : errorInline(esc(llmProbeMessage(result, payload, providerMeta)));
    }
  } catch (error) {
    delete state.llm.probeOk[role];
    if (feedbackEl) feedbackEl.innerHTML = errorInline(`test failed: ${error.message}`);
  }
}

function llmRoleConfig(role) {
  const route = findLLMRoute(role);
  return {
    driver: route ? route.driver || "" : "",
    model: route ? route.model || "" : "",
    base_url: route ? route.base_url || "" : "",
    api_key_env: route ? route.api_key_env || "" : "",
    provider: route ? route.provider || "" : "",
    max_tokens: configRoleMaxTokens(role),
  };
}

// FR-7.11 / G-AC2: saves flow through the versioned config service one key at a
// time (the llm routes endpoint does not accept max_tokens and is kept read-only
// here). A save is blocked unless a probe passed for the exact current form
// values; only keys that differ from the resolved config are written.
async function saveRoute(role, form) {
  const feedback = form.querySelector("[data-llm-feedback]");
  const data = new FormData(form);
  const driver = String(data.get("driver") || "").trim();
  const model = String(data.get("model") || "").trim();
  if (!driver || !model) {
    if (feedback) feedback.innerHTML = errorInline("pick a provider and model to save — the oauth login route needs no key; choose a provider to change it");
    return;
  }
  const baseUrl = String(data.get("base_url") || "").trim();
  const apiKeyEnv = String(data.get("api_key_env") || "").trim();
  const providerRadio = form.querySelector('input[name="llm-provider"]:checked');
  const provider = providerRadio ? String(providerRadio.value).replace(/^oauth:/, "") : "";
  // JH: an expired/absent host login blocks saving THAT oauth route until
  // availability returns; other routes are unaffected.
  if (driver === "oauth" && !llmOauthLive(provider)) {
    if (feedback) feedback.innerHTML = errorInline(llmOauthBlockMessage(provider));
    return;
  }
  const rawTokens = String(data.get("max_tokens") || "").trim();
  let maxTokens = null;
  if (rawTokens) {
    maxTokens = Number(rawTokens);
    if (!Number.isInteger(maxTokens) || maxTokens < 1) {
      if (feedback) feedback.innerHTML = errorInline("max tokens must be a positive integer, or blank");
      return;
    }
  }
  if (state.llm.probeOk[role] !== llmProbeSignature(driver, model, baseUrl, apiKeyEnv, provider)) {
    if (feedback) feedback.innerHTML = errorInline("Test the connection first — a route can only be saved after a passing probe of these exact values.");
    return;
  }
  const current = llmRoleConfig(role);
  const sets = [];
  const keyPath = (leaf) => `dream.llm.${role}.${leaf}`;
  if (current.driver !== driver) sets.push({ key_path: keyPath("driver"), value: driver });
  if (current.model !== model) sets.push({ key_path: keyPath("model"), value: model });
  if ((current.base_url || "") !== baseUrl) sets.push({ key_path: keyPath("base_url"), value: baseUrl || null });
  if ((current.api_key_env || "") !== apiKeyEnv) sets.push({ key_path: keyPath("api_key_env"), value: apiKeyEnv || null });
  if ((current.provider || "") !== provider) sets.push({ key_path: keyPath("provider"), value: provider || null });
  if ((current.max_tokens ?? null) !== maxTokens) sets.push({ key_path: keyPath("max_tokens"), value: maxTokens });
  if (!sets.length) {
    if (feedback) feedback.innerHTML = '<span class="ok-inline">no changes — the resolved route already matches</span>';
    return;
  }
  if (feedback) feedback.innerHTML = '<span class="dim">saving…</span>';
  try {
    let lastVersion = null;
    let restartRequired = false;
    for (const set of sets) {
      const result = await api("/api/v1/config/set", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(set),
      });
      lastVersion = result.version_id;
      if (result.restart_required === true) restartRequired = true;
    }
    delete state.llm.probeOk[role];
    state.llm.editingRole = null;
    state.llm.message = `route ${role} saved — config version ${lastVersion} (audited)${restartRequired ? "; restart required for the change to take effect" : ""}`;
    await loadLLM();
  } catch (error) {
    if (feedback) feedback.innerHTML = errorInline(`save failed: ${error.message}`);
  }
}

// ---------------------------------------------------------------- events (delegation)
function handleClick(event) {
  const el = event.target.closest("[data-act]");
  if (!el) return;
  switch (el.dataset.act) {
    case "dream-once":
      dreamOnce();
      break;
    case "browse-tab":
      switchTab(el.dataset.tab);
      break;
    case "browse-refresh":
      loadBrowse();
      break;
    case "browse-reset":
      state.browse.filters = {};
      state.browse.offset = 0;
      renderBrowseShell();
      loadBrowse();
      break;
    case "browse-page": {
      const offset = Math.max(0, Number(el.dataset.offset) || 0);
      state.browse.offset = offset;
      loadBrowse();
      break;
    }
    case "open-detail": {
      const type = el.dataset.type === "node" ? "node" : "chunk";
      location.hash = `#/detail/${type}/${encodeURIComponent(el.dataset.id || "")}`;
      break;
    }
    case "go-browse":
      location.hash = "#/browse";
      break;
    case "go-home":
      location.hash = "#/dashboard";
      break;
    case "review-refresh":
      loadReview();
      break;
    case "conflict-refresh":
      loadConflicts();
      break;
    case "review-verdict": {
      const index = Number(el.dataset.index);
      const verdict = el.dataset.verdict;
      const triple = state.review.data && state.review.data.triples && state.review.data.triples[index];
      if (!triple || !verdict || !state.review.runId) break;
      submitReviewVerdict(state.review.runId, triple, verdict);
      break;
    }
    case "conflict-resolve":
      resolveConflict(Number(el.dataset.index));
      break;
    case "llm-refresh":
      loadLLM();
      break;
    case "llm-edit": {
      const editRole = el.dataset.role;
      state.llm.editingRole = state.llm.editingRole === editRole ? null : editRole;
      // Any re-entry into the editor invalidates a previously passing probe.
      delete state.llm.probeOk[editRole];
      if (state.llm.editingRole === editRole) {
        const route = findLLMRoute(editRole);
        state.llm.editModel[editRole] = route ? route.model || "" : "";
        const detail = route && route.connectivity && route.connectivity.detail;
        const models =
          detail && Array.isArray(detail.models)
            ? detail.models.filter((candidate) => typeof candidate === "string")
            : [];
        const provider = llmProviderFor(route ? route.driver : "", route ? route.provider : "");
        // Seed the provider-scoped catalog from the saved route's last probe so
        // the datalist opens with the route's provider already populated.
        if (provider && models.length) state.llm.catalog[provider.id] = models;
      }
      renderLLM();
      break;
    }
    case "llm-test": {
      const testRole = el.dataset.role;
      const route = findLLMRoute(testRole);
      if (!route) break;
      const feedback = document.querySelector(
        `[data-llm-feedback][data-feedback-role="${testRole}"]`,
      );
      testRoute(
        testRole,
        route.driver,
        route.model,
        route.base_url || "",
        route.api_key_env || "",
        route.provider || "",
        feedback,
      );
      break;
    }
    case "llm-test-edit": {
      const editForm = el.closest("form");
      const editRole = el.dataset.role;
      if (!editForm) break;
      const editData = new FormData(editForm);
      const editRadio = editForm.querySelector('input[name="llm-provider"]:checked');
      testRoute(
        editRole,
        String(editData.get("driver") || "").trim(),
        String(editData.get("model") || "").trim(),
        String(editData.get("base_url") || "").trim(),
        String(editData.get("api_key_env") || "").trim(),
        editRadio ? String(editRadio.value).replace(/^oauth:/, "") : "",
        editForm.querySelector("[data-llm-feedback]"),
      );
      break;
    }
    case "llm-load-models": {
      const editForm = el.closest("form");
      if (!editForm) break;
      const editRole = el.dataset.role;
      const editData = new FormData(editForm);
      const editRadio = editForm.querySelector('input[name="llm-provider"]:checked');
      testRoute(
        editRole,
        String(editData.get("driver") || "").trim(),
        String(editData.get("model") || "").trim(),
        String(editData.get("base_url") || "").trim(),
        String(editData.get("api_key_env") || "").trim(),
        editRadio ? String(editRadio.value).replace(/^oauth:/, "") : "",
        editForm.querySelector("[data-llm-feedback]"),
      );
      break;
    }
    case "llm-key-paste": {
      const editForm = el.closest("form");
      if (editForm) llmKeyPaste(editForm, el.dataset.provider);
      break;
    }
    case "wz-next": {
      const wizard = state.llm.wizard;
      if (!wizard || !wizard.providerId) break;
      wizard.step = 2;
      renderWizardPanel();
      break;
    }
    case "wz-back": {
      const wizard = state.llm.wizard;
      if (!wizard) break;
      if (wizard.step === 2 && wizard.oauthProvider) {
        wizard.oauthProvider = null;
        wizard.step = 1;
      } else {
        wizard.step = Math.max(1, wizard.step - 1);
      }
      wizard.probeOk = false;
      renderWizardPanel();
      break;
    }
    case "wz-oauth": {
      const wizard = state.llm.wizard;
      if (!wizard) break;
      const entry = (state.llm.oauth && state.llm.oauth.providers || []).find(
        (candidate) => candidate.provider === el.dataset.provider,
      );
      if (!entry || entry.present !== true || entry.expired === true) break;
      wizard.oauthProvider = entry.provider;
      wizard.step = 2;
      renderWizardPanel();
      break;
    }
    case "wz-test": {
      const wizardForm = el.closest("form") || document.querySelector("[data-llm-wizard-form]");
      if (wizardForm) wizardTest(wizardForm);
      break;
    }
    case "wz-endpoint-reset": {
      const wizard = state.llm.wizard;
      const provider = wizard ? llmProviderById(wizard.providerId) : null;
      const form = el.closest("form");
      if (provider && form && form.elements.base_url) {
        form.elements.base_url.value = provider.baseUrl || "";
      }
      break;
    }
    case "wz-skip":
      finishDreamSetup("Skipped — MnemoSeed keeps capturing sessions, dreaming stays off until a model is configured. You can set one any time in Models.");
      break;
    case "retry":
      render();
      break;
    case "sign-out":
      signOut();
      break;
    case "profiles-refresh":
      loadProfiles();
      break;
    case "profile-archive": {
      toggleArchive(el.dataset.profileId, el.dataset.archived === "true");
      break;
    }
    case "token-issue":
      issueToken(el.dataset.profileId);
      break;
    case "token-revoke":
      revokeToken(el.dataset.tokenId);
      break;
    case "token-copy":
      copyToken(el.dataset.secret);
      break;
    case "detail-forget": {
      const forgetBtn = el;
      if (forgetBtn.dataset.armed !== "true") {
        forgetBtn.dataset.armed = "true";
        forgetBtn.textContent = "confirm forget";
        forgetBtn.title = "click again to erase this memory (audited)";
        break;
      }
      forgetDetail(el.dataset.type, el.dataset.id);
      break;
    }
    case "detail-pin":
      togglePin(el.dataset.id, el.dataset.pinned === "true");
      break;
    case "settings-refresh":
      loadSettings();
      break;
    case "config-rollback":
      rollbackConfig(el.dataset.versionId);
      break;
    case "audit-refresh":
      loadAudit();
      break;
    case "audit-reset":
      state.audit.filters = {};
      state.audit.offset = 0;
      loadAudit();
      break;
    case "audit-page":
      state.audit.offset = Number(el.dataset.offset || 0);
      loadAudit();
      break;
    default:
      break;
  }
}

function handleChange(event) {
  const target = event.target;
  if (!target) return;
  if (target.id === "profile-select") {
    state.profileId = target.value || null;
    if (state.profileId) store.set("mnemoseed.profile", state.profileId);
    else store.set("mnemoseed.profile", "");
    render();
    return;
  }
  if (target.dataset && target.dataset.act === "toggle-auto") {
    setAutoTrigger(target.checked === true);
  }
  if (target.name === "wizard-provider") {
    const wizard = state.llm.wizard;
    if (!wizard) return;
    wizard.providerId = target.value;
    wizard.oauthProvider = null;
    renderWizardPanel();
    return;
  }
  if (target.name === "wizard-share") {
    const wizard = state.llm.wizard;
    if (wizard) wizard.share = target.checked === true;
    return;
  }
  if (target.name === "llm-provider") {
    // The editor's provider picker morphs the route fields to the chosen
    // provider's defaults (oauth card → oauth mode); the provider id itself is
    // carried by the card, never by a text input.
    const form = target.closest("form");
    if (!form) return;
    const role = form.dataset.role;
    const isOAuth = String(target.value).startsWith("oauth:");
    llmApplyEditorProvider(form, target.value, true);
    // The datalist follows the picked card (curated + that provider's probe
    // catalog) — never the stale catalog of a previously saved route.
    if (role) {
      const datalist = form.querySelector(`datalist[id="llm-models-${role}"]`);
      if (datalist) {
        const provider = isOAuth ? null : llmProviderById(target.value);
        datalist.innerHTML = llmEditorModelOptions(provider);
      }
      updateModelTile(role);
    }
    llmBindOauthPaste(form, target.value);
    llmSyncEditorGate(form, target.value);
    return;
  }
  if (target.name === "model") {
    const form = target.closest("form");
    if (form && form.hasAttribute("data-llm-route-form") && form.dataset.role) {
      // Keep the role card's model tile in lockstep while typing.
      state.llm.editModel[form.dataset.role] = target.value;
      updateModelTile(form.dataset.role);
    }
    return;
  }
  if (target.hasAttribute && target.hasAttribute("data-resolution-branch")) {
    const index = target.dataset.index;
    const scopeInput = document.querySelector(`[data-resolution-scope][data-index="${index}"]`);
    if (scopeInput) scopeInput.hidden = target.value !== "coexist";
    const note = document.querySelector(`[data-scope-note][data-index="${index}"]`);
    if (note) {
      note.textContent =
        target.value === "coexist"
          ? "both sides keep their statement; the scope annotation lands on both version chains."
          : "reinforce / invalidate pick one side; coexist needs a scope annotation.";
    }
  }
}

function handleSubmit(event) {
  const form = event.target;
  if (!form) return;
  if (form.hasAttribute("data-browse-form")) {
    event.preventDefault();
    state.browse.filters = readFilters(form);
    state.browse.offset = 0;
    loadBrowse();
  }
  if (form.hasAttribute("data-llm-route-form")) {
    event.preventDefault();
    saveRoute(form.dataset.role, form);
  }
  if (form.hasAttribute("data-llm-wizard-form")) {
    event.preventDefault();
    const wizard = state.llm.wizard;
    if (!wizard) return;
    if (wizard.step === 2) {
      wizardCollect(form, wizard);
      wizard.step = 3;
      renderWizardPanel();
    } else if (wizard.step === 3) {
      wizardSave(form);
    }
    return;
  }
  if (form.hasAttribute("data-weight-form")) {
    event.preventDefault();
    adjustWeight(form);
  }
  if (form.hasAttribute("data-profiles-create-form")) {
    event.preventDefault();
    createProfile(form);
  }
  if (form.hasAttribute("data-profile-rename-form")) {
    event.preventDefault();
    renameProfile(form);
  }
  if (form.hasAttribute("data-settings-form")) {
    event.preventDefault();
    saveSettings(form);
  }
  if (form.hasAttribute("data-audit-form")) {
    event.preventDefault();
    applyAuditFilters(form);
  }
  if (form.dataset && form.dataset.authForm === "setup") {
    event.preventDefault();
    submitSetup(form);
  } else if (form.dataset && form.dataset.authForm === "login") {
    event.preventDefault();
    submitLogin(form);
  }
}

// ---------------------------------------------------------------- graph view (④ FR-7.8)
// Hand-rolled three.js instanced layer (design/07 §4, approved 2026-08-12):
// one THREE.Points custom-shader draw for nodes, one InstancedMesh of quads
// for edges, canvas-sprite labels for the top-60 centrality nodes, Raycaster
// picking, precomputed clustered layout — no runtime force simulation. The
// three.js build is VENDORED under /console/vendor (never a CDN). Node
// opacity = decay_weight, color = type, size = centrality, edge thickness =
// weight; filters profile/type/time/Tier; click → Memory Detail.
const GRAPH_TYPE_RGB = {
  PREFERENCE: [0x34, 0xd3, 0x99],
  HABIT: [0x2d, 0xd4, 0xbf],
  EPISODE: [0x60, 0xa5, 0xfa],
  SKILL_SEQUENCE: [0xfb, 0x92, 0x3c],
  DECISION: [0xa7, 0x8b, 0xfa],
  INTENTION: [0xf8, 0x71, 0x71],
  CONSTRAINT: [0xf4, 0xbf, 0x4f],
  ANIMA: [0xf0, 0x62, 0x92],
  USER: [0x8b, 0xd3, 0xc7],
  PROJECT: [0x5a, 0xc8, 0xfa],
  TOOL: [0xc0, 0x9c, 0x8c],
};

const GRAPH_STATE = {
  data: null, // { nodes, edges, byId, centrality, positions }
  handle: null, // buildGraphScene handle (scene, renderer, cleanup, ...)
  typeCounts: new Map(),
  typeFilter: "",
  tierFilter: "",
  timeFilter: "all",
  kinds: new Set(["relation", "cooccurrence"]),
  degraded: false,
  notice: null,
  three: null,
  scene: null,
  camera: null,
  renderer: null,
  points: null,
  edgeMesh: null,
  labels: null,
  controls: null,
  cleanup: null,
};

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function sleepGraph(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function graphCentrality(nodes, edges) {
  // Degree centrality, normalized to [0,1] (appendix B.2: no standalone
  // centrality query in M0 — the console derives it from the edge set).
  const counts = new Map();
  for (const n of nodes) counts.set(n.node_id, 0);
  for (const e of edges) {
    counts.set(e.src, (counts.get(e.src) || 0) + 1);
    counts.set(e.dst, (counts.get(e.dst) || 0) + 1);
  }
  let max = 1;
  for (const value of counts.values()) if (value > max) max = value;
  const centrality = new Map();
  for (const [id, value] of counts) centrality.set(id, value / max);
  return centrality;
}

function graphLayout(nodes, centrality) {
  // Precomputed clustered layout: communities are (node type, tier) groups,
  // group centers sit on a golden-spiral sphere, members jitter around their
  // center with a seeded PRNG (deterministic for the same data). Cheap: one
  // O(n) pass, no force simulation at runtime.
  const groups = new Map();
  for (const n of nodes) {
    const key = `${n.node_type}|${n.cognitive_tier}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(n);
  }
  const groupList = [...groups.values()];
  const groupCount = groupList.length;
  const WORLD_RADIUS = 260;
  const GROUP_RADIUS = 72;
  const golden = Math.PI * (3 - Math.sqrt(5));
  const centers = [];
  for (let i = 0; i < groupCount; i++) {
    const y = 1 - (i / Math.max(1, groupCount - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * i;
    centers.push({
      x: WORLD_RADIUS * r * Math.cos(theta),
      y: WORLD_RADIUS * y,
      z: WORLD_RADIUS * r * Math.sin(theta),
    });
  }
  const rng = mulberry32(42);
  const positions = new Map();
  groupList.forEach((members, groupIndex) => {
    const c = centers[groupIndex];
    for (const n of members) {
      const hubPull = (centrality.get(n.node_id) || 0) > 0.5 ? 0.45 : 1;
      const ring = Math.sqrt(rng()) * GROUP_RADIUS * 0.9 * hubPull;
      const angle = rng() * Math.PI * 2;
      const jitter = (rng() * 2 - 1) * 10;
      positions.set(n.node_id, {
        x: c.x + Math.cos(angle) * ring + jitter,
        y: c.y + (rng() * 2 - 1) * GROUP_RADIUS * 0.4 * hubPull,
        z: c.z + Math.sin(angle) * ring + jitter,
      });
    }
  });
  return positions;
}

function graphLabel(node) {
  return `${node.statement}${node.conflict_flag ? " ⚠" : ""}`;
}

async function loadGraph() {
  const profileId = await ensureProfile();
  const view = document.getElementById("view");
  if (!profileId) {
    view.innerHTML = errorPanel("No profile selected — pick one in the header.");
    return;
  }
  const params = new URLSearchParams(location.search);
  if (params.has("perf")) {
    view.innerHTML = '<p class="loading">Running graph perf bench…</p>';
    runGraphPerf(view);
    return;
  }
  try {
    const nodes = [];
    const edges = [];
    const base = `/api/v1/graph/subgraph?profile_id=${encodeURIComponent(profileId)}&limit=2000`;
    const first = await api(base);
    nodes.push(...first.nodes);
    edges.push(...first.edges);
    GRAPH_STATE.degraded = Boolean(first.degraded);
    GRAPH_STATE.notice = first.notice || null;
    let offset = first.paging.limit;
    while (offset < first.paging.total) {
      const page = await api(`${base}&offset=${offset}`);
      edges.push(...page.edges);
      if (page.edges.length === 0) break;
      offset += page.paging.limit;
    }
    renderGraph(view, { nodes, edges });
  } catch (error) {
    view.innerHTML = errorPanel(`Graph unavailable: ${error.message}`);
  }
}

function graphShellHtml(meta) {
  const typeOptions = ["", ...meta.types].map(
    (t) => `<option value="${esc(t)}" ${t === GRAPH_STATE.typeFilter ? "selected" : ""}>${t ? esc(t) : "all types"}</option>`
  ).join("");
  const tierOptions = ["", "1", "2", "3"].map(
    (t) => `<option value="${t}" ${t === GRAPH_STATE.tierFilter ? "selected" : ""}>${t ? `Tier ${t}` : "all tiers"}</option>`
  ).join("");
  const kinds = ["relation", "cooccurrence"].map(
    (k) =>
      `<label class="check-row"><input type="checkbox" data-graph-filter="kind" value="${k}" ${GRAPH_STATE.kinds.has(k) ? "checked" : ""} /> ${k}</label>`
  ).join("");
  return `<section class="graph-shell">
    <div class="graph-toolbar">
      <span class="graph-title">memory graph</span>
      <span class="graph-count">${fmtNum(meta.nodes)} nodes · ${fmtNum(meta.edges)} edges</span>
      <button class="btn" type="button" data-act="graph-refresh">Refresh</button>
      <button class="btn" type="button" data-act="graph-fit">Fit view</button>
    </div>
    <div class="graph-filters">
      <label class="graph-filter-label">type
        <select data-graph-filter="type">${typeOptions}</select>
      </label>
      <label class="graph-filter-label">tier
        <select data-graph-filter="tier">${tierOptions}</select>
      </label>
      <label class="graph-filter-label">time
        <select data-graph-filter="time">
          <option value="all" ${GRAPH_STATE.timeFilter === "all" ? "selected" : ""}>all time</option>
          <option value="30" ${GRAPH_STATE.timeFilter === "30" ? "selected" : ""}>last 30 days</option>
          <option value="90" ${GRAPH_STATE.timeFilter === "90" ? "selected" : ""}>last 90 days</option>
          <option value="365" ${GRAPH_STATE.timeFilter === "365" ? "selected" : ""}>last year</option>
          <option value="old" ${GRAPH_STATE.timeFilter === "old" ? "selected" : ""}>older than a year</option>
        </select>
      </label>
      <span class="graph-filter-label">edges ${kinds}</span>
    </div>
    <div class="graph-notice" id="graph-notice" hidden></div>
    <div class="graph-layout">
      <div id="graph-stage" class="graph-stage"></div>
      <aside class="graph-detail" id="graph-detail" hidden></aside>
    </div>
    <div class="graph-legend" id="graph-legend"></div>
  </section>`;
}

function graphPerfShellHtml() {
  return `<section class="graph-shell">
    <div class="graph-perf-hud" id="graph-perf-hud">warming up…</div>
    <div id="graph-stage" class="graph-stage"></div>
  </section>`;
}

function graphMeta(data) {
  const types = new Set();
  const typeCounts = new Map();
  for (const n of data.nodes) {
    types.add(n.node_type);
    typeCounts.set(n.node_type, (typeCounts.get(n.node_type) || 0) + 1);
  }
  return {
    nodes: data.nodes.length,
    edges: data.edges.length,
    types: [...types].sort(),
    typeCounts,
  };
}

async function renderGraph(view, data) {
  const THREE = await import("/console/vendor/three.module.js");
  GRAPH_STATE.three = THREE;
  if (!data.nodes.length) {
    view.innerHTML =
      '<p class="loading">No long-term memories yet — captured sessions consolidate into graph nodes after a dream run.</p>';
    return;
  }
  const meta = graphMeta(data);
  GRAPH_STATE.typeCounts = meta.typeCounts;
  const centrality = graphCentrality(data.nodes, data.edges);
  GRAPH_STATE.data = {
    nodes: data.nodes,
    edges: data.edges,
    byId: new Map(data.nodes.map((n) => [n.node_id, n])),
    centrality,
    positions: graphLayout(data.nodes, centrality),
  };
  view.innerHTML = graphShellHtml(meta);
  const notice = view.querySelector("#graph-notice");
  if (GRAPH_STATE.degraded && notice) {
    notice.textContent = GRAPH_STATE.notice || "graph.edge_list unavailable — degraded to per-node traversal.";
    notice.hidden = false;
  }
  wireGraphFilters(view);
  renderGraphLegend(view);
  const handle = buildGraphScene(view, GRAPH_STATE.data, THREE);
  if (handle) {
    GRAPH_STATE.handle = handle;
    GRAPH_STATE.points = handle.points;
    GRAPH_STATE.edgeMesh = handle.edgeMesh;
    GRAPH_STATE.labels = handle.labels;
    GRAPH_STATE.renderer = handle.renderer;
    GRAPH_STATE.scene = handle.scene;
    GRAPH_STATE.camera = handle.camera;
    GRAPH_STATE.controls = handle.controls;
    handle.fit();
    GRAPH_STATE.cleanup = handle.cleanup;
    const refresh = () => loadGraph();
    view.querySelector('[data-act="graph-refresh"]').addEventListener("click", refresh);
    view.querySelector('[data-act="graph-fit"]').addEventListener("click", () => handle.fit());
  }
}

function renderGraphLegend(view) {
  const legend = view.querySelector("#graph-legend");
  if (!legend) return;
  legend.innerHTML = [...GRAPH_STATE.typeCounts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([type, count]) => {
      const rgb = GRAPH_TYPE_RGB[type] || [0x88, 0x88, 0x88];
      const hex = `#${rgb.map((c) => c.toString(16).padStart(2, "0")).join("")}`;
      return `<span class="legend-chip"><span class="legend-dot" style="background:${hex}"></span>${esc(type)} (${fmtNum(count)})</span>`;
    })
    .join("");
}

function wireGraphFilters(view) {
  const filters = view.querySelector(".graph-filters");
  if (!filters) return;
  filters.addEventListener("change", (event) => {
    const target = event.target;
    if (!target || !target.hasAttribute("data-graph-filter")) return;
    const kind = target.getAttribute("data-graph-filter");
    if (kind === "type") GRAPH_STATE.typeFilter = target.value;
    else if (kind === "tier") GRAPH_STATE.tierFilter = target.value;
    else if (kind === "time") GRAPH_STATE.timeFilter = target.value;
    else if (kind === "kind") {
      if (target.checked) GRAPH_STATE.kinds.add(target.value);
      else GRAPH_STATE.kinds.delete(target.value);
    }
    applyGraphFilters();
  });
}

function graphNodeVisible(node) {
  if (GRAPH_STATE.typeFilter && node.node_type !== GRAPH_STATE.typeFilter) return false;
  if (GRAPH_STATE.tierFilter && String(node.cognitive_tier) !== GRAPH_STATE.tierFilter) return false;
  const ageDays = (Date.now() / 1000 - node.created_at) / 86400;
  if (GRAPH_STATE.timeFilter === "30" && ageDays > 30) return false;
  if (GRAPH_STATE.timeFilter === "90" && ageDays > 90) return false;
  if (GRAPH_STATE.timeFilter === "365" && ageDays > 365) return false;
  if (GRAPH_STATE.timeFilter === "old" && ageDays <= 365) return false;
  return true;
}

function applyGraphFilters() {
  const handle = GRAPH_STATE;
  if (!handle.data || !handle.points || !handle.edgeMesh) return;
  const THREE = handle.three;
  const data = handle.data;
  const hidden = new Set();
  const visibleAttr = handle.points.geometry.attributes.aVisible;
  data.nodes.forEach((n, i) => {
    const ok = graphNodeVisible(n);
    visibleAttr.array[i] = ok ? 1 : 0;
    if (!ok) hidden.add(n.node_id);
  });
  visibleAttr.needsUpdate = true;
  const scale = new THREE.Vector3();
  const position = new THREE.Vector3();
  const quaternion = new THREE.Quaternion();
  const matrix = new THREE.Matrix4();
  data.edges.forEach((e, i) => {
    const showKind = GRAPH_STATE.kinds.has(e.kind);
    const showEndpoints = !hidden.has(e.src) && !hidden.has(e.dst);
    handle.edgeMesh.getMatrixAt(i, matrix);
    matrix.decompose(position, quaternion, scale);
    scale.x = showKind && showEndpoints ? 1 : 0;
    matrix.compose(position, quaternion, scale);
    handle.edgeMesh.setMatrixAt(i, matrix);
  });
  handle.edgeMesh.instanceMatrix.needsUpdate = true;
  for (const [id, sprite] of handle.labels) sprite.visible = !hidden.has(id);
}

function buildGraphScene(view, data, THREE, opts) {
  opts = opts || {};
  const stage = view.querySelector("#graph-stage");
  if (!stage) return null;
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
  } catch (_err) {
    stage.innerHTML = errorPanel("WebGL is unavailable in this browser — the graph view needs WebGL2.");
    return null;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(stage.clientWidth || 900, stage.clientHeight || 560);
  stage.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0e14);
  const camera = new THREE.PerspectiveCamera(50, stage.clientWidth / (stage.clientHeight || 560), 1, 50000);
  camera.position.set(0, 0, 1400);

  const { nodes, edges, byId, centrality, positions } = data;
  const N = nodes.length;
  const L = edges.length;

  // --- node points: one draw call, custom shader (screen-space point size) ---
  const positionsArr = new Float32Array(N * 3);
  const colors = new Float32Array(N * 3);
  const sizes = new Float32Array(N);
  const baseSizes = new Float32Array(N);
  const opacities = new Float32Array(N);
  const visibles = new Float32Array(N);

  nodes.forEach((n, i) => {
    const p = positions.get(n.node_id) || { x: 0, y: 0, z: 0 };
    positionsArr[i * 3] = p.x;
    positionsArr[i * 3 + 1] = p.y;
    positionsArr[i * 3 + 2] = p.z;
    const rgb = GRAPH_TYPE_RGB[n.node_type] || [0x88, 0x88, 0x88];
    colors[i * 3] = rgb[0] / 255;
    colors[i * 3 + 1] = rgb[1] / 255;
    colors[i * 3 + 2] = rgb[2] / 255;
    baseSizes[i] = 5 + (centrality.get(n.node_id) || 0) * 24;
    sizes[i] = baseSizes[i];
    opacities[i] = n.never_decay ? 1 : Math.max(0.05, n.decay_weight);
    visibles[i] = 1;
  });

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(positionsArr, 3));
  geo.setAttribute("aColor", new THREE.BufferAttribute(colors, 3));
  geo.setAttribute("aSize", new THREE.BufferAttribute(sizes, 1));
  geo.setAttribute("aOpacity", new THREE.BufferAttribute(opacities, 1));
  geo.setAttribute("aVisible", new THREE.BufferAttribute(visibles, 1));
  geo.computeBoundingSphere();

  const ptScale = (stage.clientHeight || 560) / (2 * Math.tan(THREE.MathUtils.degToRad(25)));
  const pointMat = new THREE.ShaderMaterial({
    transparent: true,
    depthWrite: false,
    vertexShader: `
      attribute float aSize;
      attribute vec3 aColor;
      attribute float aOpacity;
      attribute float aVisible;
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        vec4 mv = modelViewMatrix * vec4(position, 1.0);
        gl_PointSize = aSize * (${ptScale.toFixed(1)} / -mv.z);
        gl_Position = projectionMatrix * mv;
        vColor = aColor;
        vAlpha = aOpacity * aVisible;
      }
    `,
    fragmentShader: `
      varying vec3 vColor;
      varying float vAlpha;
      void main() {
        float d = length(gl_PointCoord - 0.5);
        float a = smoothstep(0.5, 0.40, d) * vAlpha;
        if (a < 0.02) discard;
        gl_FragColor = vec4(vColor, a);
      }
    `,
  });
  const points = new THREE.Points(geo, pointMat);
  points.frustumCulled = false;

  // --- edge quads: one InstancedMesh draw, thickness = weight ---
  const edgeGeo = new THREE.BufferGeometry();
  edgeGeo.setAttribute(
    "position",
    new THREE.BufferAttribute(new Float32Array([-0.5, -0.5, 0, 0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, 0.5, 0]), 3)
  );
  const edgeMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.5, depthWrite: false });
  const edgeMesh = new THREE.InstancedMesh(edgeGeo, edgeMat, Math.max(L, 1));
  edgeMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);

  const UNIT_X = new THREE.Vector3(1, 0, 0);
  const _a = new THREE.Vector3();
  const _b = new THREE.Vector3();
  const _dir = new THREE.Vector3();
  const _mid = new THREE.Vector3();
  const _quat = new THREE.Quaternion();
  const _scale = new THREE.Vector3();
  const _m = new THREE.Matrix4();
  const _col = new THREE.Color();

  function edgeMatrixAt(i, edge) {
    const na = byId.get(edge.src);
    const nb = byId.get(edge.dst);
    if (!na || !nb) {
      _m.makeScale(0, 1, 1);
      edgeMesh.setMatrixAt(i, _m);
      return;
    }
    const pa = positions.get(edge.src) || { x: 0, y: 0, z: 0 };
    const pb = positions.get(edge.dst) || { x: 0, y: 0, z: 0 };
    _a.set(pa.x, pa.y, pa.z);
    _b.set(pb.x, pb.y, pb.z);
    _dir.subVectors(_b, _a);
    const len = _dir.length();
    _dir.normalize();
    _mid.addVectors(_a, _b).multiplyScalar(0.5);
    _quat.setFromUnitVectors(UNIT_X, _dir);
    _scale.set(len, 0.3 + edge.weight, 1);
    _m.compose(_mid, _quat, _scale);
    edgeMesh.setMatrixAt(i, _m);
  }

  edges.forEach((edge, i) => {
    edgeMatrixAt(i, edge);
    _col.setRGB(0.42 + edge.weight * 0.35, 0.5 + edge.weight * 0.3, 0.62 + edge.weight * 0.25);
    edgeMesh.setColorAt(i, _col);
  });
  edgeMesh.instanceMatrix.needsUpdate = true;
  if (edgeMesh.instanceColor) edgeMesh.instanceColor.needsUpdate = true;

  // --- labels: canvas sprites for the top-60 nodes by centrality ---
  const TOP_LABELS = 60;
  const labelNodes = [...nodes].sort(
    (a, b) => (centrality.get(b.node_id) || 0) - (centrality.get(a.node_id) || 0)
  ).slice(0, TOP_LABELS);
  const labels = new Map();
  const labelGroup = new THREE.Group();
  for (const n of labelNodes) {
    const canvas = document.createElement("canvas");
    canvas.width = 256;
    canvas.height = 64;
    const ctx = canvas.getContext("2d");
    ctx.font = "bold 30px monospace";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(225,235,245,0.95)";
    ctx.fillText(truncate(graphLabel(n), 24), 128, 32);
    const sprite = new THREE.Sprite(
      new THREE.SpriteMaterial({
        map: new THREE.CanvasTexture(canvas),
        transparent: true,
        depthTest: false,
        sizeAttenuation: true,
      })
    );
    const p = positions.get(n.node_id) || { x: 0, y: 0, z: 0 };
    sprite.scale.set(52, 13, 1);
    sprite.renderOrder = 20;
    sprite.position.set(p.x, p.y + 10 + (centrality.get(n.node_id) || 0) * 8, p.z);
    labelGroup.add(sprite);
    labels.set(n.node_id, sprite);
  }

  scene.add(edgeMesh);
  scene.add(points);
  scene.add(labelGroup);
  edgeMesh.frustumCulled = false;

  // --- minimal orbit control (rotate by drag, zoom by wheel) ---
  const controls = new GraphOrbit(camera, renderer.domElement, THREE);

  function fit() {
    const box = new THREE.Box3();
    const v = new THREE.Vector3();
    for (const n of nodes) {
      const p = positions.get(n.node_id) || { x: 0, y: 0, z: 0 };
      box.expandByPoint(v.set(p.x, p.y, p.z));
    }
    const center = box.getCenter(new THREE.Vector3());
    const radius = box.getBoundingSphere(new THREE.Sphere()).radius || 1;
    const dist = (radius / Math.tan(THREE.MathUtils.degToRad(25))) * 1.25;
    camera.position.copy(center).add(new THREE.Vector3(0, 0, dist));
    camera.lookAt(center);
    controls.target.copy(center);
  }

  // --- raycast picking → Memory Detail side panel ---
  const raycaster = new THREE.Raycaster();
  raycaster.params.Points.threshold = 14;
  let hoveredIndex = -1;
  renderer.domElement.addEventListener("mousemove", (event) => {
    const rect = renderer.domElement.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const py = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(new THREE.Vector2(px, py), camera);
    const hits = raycaster.intersectObject(points, false);
    if (hoveredIndex >= 0) sizes[hoveredIndex] = baseSizes[hoveredIndex];
    hoveredIndex = hits.length ? hits[0].index : -1;
    if (hoveredIndex >= 0) sizes[hoveredIndex] = baseSizes[hoveredIndex] * 1.7;
    geo.attributes.aSize.needsUpdate = true;
    renderer.domElement.style.cursor = hoveredIndex >= 0 ? "pointer" : "grab";
  });
  renderer.domElement.addEventListener("click", (event) => {
    const rect = renderer.domElement.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const py = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(new THREE.Vector2(px, py), camera);
    const hits = raycaster.intersectObject(points, false);
    if (hits.length) {
      const node = nodes[hits[0].index];
      graphOpenDetail(view, node.node_id);
    }
  });

  let disposed = false;
  let rafHandle = 0;
  function cleanup() {
    if (disposed) return;
    disposed = true;
    cancelAnimationFrame(rafHandle);
    window.removeEventListener("resize", onResize);
    geo.dispose();
    pointMat.dispose();
    edgeGeo.dispose();
    edgeMat.dispose();
    for (const [, sprite] of labels) sprite.material.map.dispose();
    renderer.dispose();
    if (stage.contains(renderer.domElement)) stage.removeChild(renderer.domElement);
  }

  function onResize() {
    const width = stage.clientWidth || 900;
    const height = stage.clientHeight || 560;
    renderer.setSize(width, height);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
  }
  window.addEventListener("resize", onResize);

  function loop() {
    if (disposed) return;
    renderer.render(scene, camera);
    rafHandle = requestAnimationFrame(loop);
  }

  fit();
  renderer.compile(scene, camera);
  if (opts.autostart !== false) rafHandle = requestAnimationFrame(loop);

  return {
    fit,
    cleanup,
    points,
    edgeMesh,
    labels,
    renderer,
    scene,
    camera,
    controls,
    three: THREE,
    data,
    cancelLoop() {
      cancelAnimationFrame(rafHandle);
    },
  };
}

function graphOpenDetail(view, nodeId) {
  const panel = view.querySelector("#graph-detail");
  if (!panel) return;
  api(`/api/v1/nodes/${encodeURIComponent(nodeId)}?profile_id=${encodeURIComponent(state.profileId || "")}`)
    .then((dossier) => {
      panel.innerHTML = `
        <h3>memory detail</h3>
        <p class="graph-detail-statement">${esc(dossier.content.statement || dossier.node_id)}</p>
        <dl class="kv">
          ${kvList([
            ["type", esc(dossier.node_type)],
            ["decay weight", decayMeter(dossier.weights.decay_weight)],
            ["confidence", fmtNum(dossier.weights.confidence)],
            ["tier", String(dossier.metadata ? dossier.metadata.cognitive_tier : "—")],
            ["hit count", fmtNum(dossier.usage.hit_count)],
            ["updated", fmtEpoch(dossier.updated_at)],
          ])}
        </dl>
        <a class="btn" href="#/detail/node/${encodeURIComponent(nodeId)}">open full dossier →</a>`;
      panel.hidden = false;
    })
    .catch(() => {
      panel.innerHTML = `<p class="dim">detail unavailable</p>`;
      panel.hidden = false;
    });
}

// ------------------------------------------------------- graph perf bench mode
// Reuses the bench architecture (graphview-three, 2026-08-13) at 5k nodes to
// log fps in a CI-skip-safe form: open the page with ?perf=1, let it run, and
// read window.__GRAPH_PERF (or the document.title). Not a CI gate — GPU
// numbers need a real display.

function generatePerfGraph(nodeCount) {
  const TYPES = ["PREFERENCE", "HABIT", "EPISODE", "SKILL_SEQUENCE", "DECISION", "INTENTION"];
  const rng = mulberry32(20240814);
  const rand = (lo, hi) => lo + rng() * (hi - lo);
  const clamp01 = (v) => Math.max(0, Math.min(1, v));
  function gauss() {
    let u = 0;
    let v = 0;
    while (u === 0) u = rng();
    while (v === 0) v = rng();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }
  const PROFILES = 8;
  const golden = Math.PI * (3 - Math.sqrt(5));
  const centers = [];
  for (let i = 0; i < PROFILES; i++) {
    const y = 1 - (i / (PROFILES - 1)) * 2;
    const r = Math.sqrt(Math.max(0, 1 - y * y));
    const theta = golden * i;
    centers.push({ x: 260 * r * Math.cos(theta), y: 260 * y, z: 260 * r * Math.sin(theta) });
  }
  const now = Date.now() / 1000;
  const day = 86400;
  const nodes = [];
  const perProfile = Math.floor(nodeCount / PROFILES);
  for (let p = 0; p < PROFILES; p++) {
    const c = centers[p];
    const count = p === PROFILES - 1 ? nodeCount - perProfile * (PROFILES - 1) : perProfile;
    for (let i = 0; i < count; i++) {
      const type = TYPES[Math.floor(rng() * TYPES.length)];
      let centrality = Math.pow(rng(), 3) * 0.92 + 0.01;
      const ageDays = Math.pow(rng(), 1.4) * 365;
      const created_at = now - ageDays * day;
      let decay_weight = clamp01(0.88 - (ageDays / 365) * 0.62 + (centrality > 0.7 ? 0.12 : 0) + gauss() * 0.06);
      decay_weight = clamp01(decay_weight);
      const tier = centrality > 0.7 ? 1 : centrality > 0.35 ? 2 : 3;
      nodes.push({
        node_id: `n${nodes.length}`,
        node_type: type,
        statement: `memory ${nodes.length}`,
        cognitive_tier: tier,
        decay_weight: +decay_weight.toFixed(3),
        created_at,
        profile: `profile-${p}`,
      });
    }
  }
  const edges = [];
  const edgeSet = new Set();
  const byProfile = new Map();
  for (const n of nodes) {
    if (!byProfile.has(n.profile)) byProfile.set(n.profile, []);
    byProfile.get(n.profile).push(n);
  }
  const addEdge = (a, b, weight, kind) => {
    if (a === b) return;
    const key = a < b ? `${a}|${b}` : `${b}|${a}`;
    if (edgeSet.has(key)) return;
    edgeSet.add(key);
    edges.push({
      edge_id: `e${edges.length}`,
      src: a,
      dst: b,
      weight: +clamp01(weight).toFixed(3),
      kind,
      created_at: now - rand(0, 365 * day),
    });
  };
  for (const group of byProfile.values()) {
    for (const n of group) {
      const degree = 2 + Math.round((n.centrality || 0.1) * 5);
      let added = 0;
      let attempts = 0;
      while (added < degree && attempts < 40) {
        attempts++;
        const cand = group[Math.floor(rng() * group.length)];
        if (cand.node_id === n.node_id) continue;
        addEdge(
          n.node_id,
          cand.node_id,
          0.45 + (n.centrality + cand.centrality) * 0.3 + rng() * 0.2,
          rng() < 0.72 ? "relation" : "cooccurrence"
        );
        added++;
      }
    }
  }
  const hubs = [];
  for (const group of byProfile.values()) {
    const sorted = [...group].sort((a, b) => b.centrality - a.centrality);
    hubs.push(...sorted.slice(0, 8));
  }
  for (const h of hubs) {
    const other = hubs[Math.floor(rng() * hubs.length)];
    if (other && other.node_id !== h.node_id) {
      addEdge(h.node_id, other.node_id, rand(0.25, 0.5), rng() < 0.5 ? "relation" : "cooccurrence");
    }
  }
  return { nodes, edges };
}

function applyGraphDecay(handle) {
  // The decay showcase: every tick fades node opacity a step and nudges ~5%
  // of edge weights (the bench S2 pattern) — a single attribute upload + a
  // handful of matrix writes.
  const attr = handle.points.geometry.attributes.aOpacity;
  for (let i = 0; i < attr.array.length; i++) attr.array[i] = Math.max(0.02, attr.array[i] * 0.9975);
  attr.needsUpdate = true;
  const rng = mulberry32(11);
  const THREE = handle.three;
  const _m = new THREE.Matrix4();
  const _q = new THREE.Quaternion();
  const _p = new THREE.Vector3();
  const _s = new THREE.Vector3();
  let changed = 0;
  for (let i = 0; i < handle.data.edges.length; i++) {
    if (rng() < 0.05) {
      const edge = handle.data.edges[i];
      edge.weight = Math.max(0.05, edge.weight * (0.97 + rng() * 0.06));
      handle.edgeMesh.getMatrixAt(i, _m);
      _m.decompose(_p, _q, _s);
      _s.y = 0.3 + edge.weight;
      _m.compose(_p, _q, _s);
      handle.edgeMesh.setMatrixAt(i, _m);
      changed++;
    }
  }
  if (changed) handle.edgeMesh.instanceMatrix.needsUpdate = true;
  return changed;
}

async function runGraphPerf(view) {
  const THREE = await import("/console/vendor/three.module.js");
  const data = generatePerfGraph(5000);
  const meta = graphMeta(data);
  const centrality = graphCentrality(data.nodes, data.edges);
  const positions = graphLayout(data.nodes, centrality);
  GRAPH_STATE.data = {
    nodes: data.nodes,
    edges: data.edges,
    byId: new Map(data.nodes.map((n) => [n.node_id, n])),
    centrality,
    positions,
  };
  GRAPH_STATE.three = THREE;
  view.innerHTML = graphPerfShellHtml();
  const hud = view.querySelector("#graph-perf-hud");
  const handle = buildGraphScene(view, GRAPH_STATE.data, THREE, { autostart: false });
  if (!handle) return;
  await sleepGraph(1200); // shader/state warmup
  const frames = [];
  let last = 0;
  const start = performance.now();
  let count = 0;
  const DURATION = 20000;
  const decayTimer = setInterval(() => applyGraphDecay(handle), 250);
  const tick = (t) => {
    handle.renderer.render(handle.scene, handle.camera);
    if (last) frames.push(t - last);
    last = t;
    count++;
    const elapsed = t - start;
    const avg = count / (elapsed / 1000);
    if (hud) hud.textContent = `perf — ${avg.toFixed(1)} fps avg (${meta.nodes} nodes / ${meta.edges} edges)`;
    if (elapsed >= DURATION) {
      clearInterval(decayTimer);
      frames.sort((a, b) => a - b);
      const p95 = frames[Math.max(1, Math.floor(frames.length * 0.95))];
      const results = {
        nodes: meta.nodes,
        edges: meta.edges,
        durationMs: DURATION,
        avgFps: +(count / (elapsed / 1000)).toFixed(2),
        p5Fps: +(1000 / p95).toFixed(2),
      };
      window.__GRAPH_PERF = results;
      document.title = `MnemoSeed perf — avg ${results.avgFps} fps / p5 ${results.p5Fps} fps`;
      if (hud) hud.textContent = `done — avg ${results.avgFps} fps / p5 ${results.p5Fps} fps (window.__GRAPH_PERF)`;
      return;
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

function disposeGraphView() {
  if (GRAPH_STATE.cleanup) {
    GRAPH_STATE.cleanup();
    GRAPH_STATE.cleanup = null;
  }
  GRAPH_STATE.handle = null;
  GRAPH_STATE.scene = null;
  GRAPH_STATE.camera = null;
  GRAPH_STATE.renderer = null;
  GRAPH_STATE.points = null;
  GRAPH_STATE.edgeMesh = null;
  GRAPH_STATE.labels = null;
  GRAPH_STATE.controls = null;
  GRAPH_STATE.data = null;
}

// Minimal orbit camera control (rotate by drag, zoom by wheel) — hand-rolled so
// the graph view depends on zero framework code beyond the vendored three.js.
class GraphOrbit {
  constructor(camera, dom, THREE) {
    this.camera = camera;
    this.dom = dom;
    this.THREE = THREE;
    this.target = new THREE.Vector3();
    this._spherical = new THREE.Spherical();
    this._offset = new THREE.Vector3();
    let dragging = false;
    let px = 0;
    let py = 0;
    dom.addEventListener("mousedown", (event) => {
      dragging = true;
      px = event.clientX;
      py = event.clientY;
    });
    window.addEventListener("mouseup", () => {
      dragging = false;
    });
    dom.addEventListener("mousemove", (event) => {
      if (!dragging) return;
      this.rotate(event.clientX - px, event.clientY - py);
      px = event.clientX;
      py = event.clientY;
    });
    dom.addEventListener(
      "wheel",
      (event) => {
        event.preventDefault();
        this.zoom(event.deltaY > 0 ? 1.12 : 0.9);
      },
      { passive: false }
    );
  }
  rotate(dx, dy) {
    const THREE = this.THREE;
    const offset = this._offset.copy(this.camera.position).sub(this.target);
    const spherical = this._spherical.setFromVector3(offset);
    spherical.theta -= dx * 0.005;
    spherical.phi -= dy * 0.005;
    spherical.phi = Math.max(0.05, Math.min(Math.PI - 0.05, spherical.phi));
    offset.setFromSpherical(spherical);
    this.camera.position.copy(this.target).add(offset);
    this.camera.lookAt(this.target);
  }
  zoom(factor) {
    const offset = this._offset.copy(this.camera.position).sub(this.target).multiplyScalar(factor);
    const length = offset.length();
    if (length < 5 || length > 50000) return;
    this.camera.position.copy(this.target).add(offset);
    this.camera.lookAt(this.target);
  }
}

// ---------------------------------------------------------------- boot
document.addEventListener("click", handleClick);
document.addEventListener("change", handleChange);
document.addEventListener("submit", handleSubmit);
window.addEventListener("hashchange", render);

// The identity gate decides the first paint: setup wizard, login view, or the
// app. A deep link to /console/#/setup (the gate's setup_url) resolves through
// the same probe: setup mode shows the wizard, otherwise login.
boot();
