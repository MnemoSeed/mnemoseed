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
  llm: { routes: null, oauth: null, config: null, editingRole: null, message: null, probeOk: {} },
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
  if (view) view.innerHTML = dreamSetupHtml(routes, oauth);
}

function dreamSetupHtml(routes, oauth) {
  const providers = (oauth && oauth.providers) || [];
  const oauthRows = providers.length
    ? providers
        .map((entry) => {
          const live = entry.present === true && entry.expired !== true;
          const mark = live
            ? '<span class="badge badge-ok">logged in</span>'
            : entry.present === true
              ? '<span class="badge badge-warn">expired</span>'
              : '<span class="badge">not detected</span>';
          return `<div class="resolve-row">
            ${mark} <span class="mono">${esc(entry.provider)}</span>${entry.expires_at ? ` <span class="dim">· ${esc(fmtEpoch(entry.expires_at))}</span>` : ""}
            <span class="spacer"></span>
            <button class="btn btn-primary" data-act="llm-wizard-oauth" data-provider="${esc(entry.provider)}" ${live ? "" : "disabled"} title="fill the route below with ${esc(entry.provider)} OAuth">use ${esc(entry.provider)} oauth</button>
          </div>`;
        })
        .join("")
    : "";
  const drivers = routes.drivers || [];
  const driverOptions = drivers
    .map((d) => `<option value="${esc(d.name)}">${esc(d.name)}</option>`)
    .join("");
  return `<div class="auth-panel card">
    <h2>dream model</h2>
    <p class="toolbar-note">Owner created. Pick the model the dream pipeline uses to distill memories — or skip and keep the defaults. Routes are editable any time from the Models view.</p>
    <h3>host OAuth</h3>
    <p class="toolbar-note">The console only detects whether this machine is already signed in to a provider CLI. Picking one fills the route below — enter the model name your subscription uses, then save. No key value is read, sent, or stored.</p>
    ${oauthRows || emptyPanel("No host OAuth login detected (~/.codex or ~/.grok).")}
    <h3>bring your own key</h3>
    <form data-llm-wizard-form>
      <div class="filter-grid">
        <div class="field"><label for="wz-driver">driver</label><select id="wz-driver" name="driver">${driverOptions}</select></div>
        <div class="field"><label for="wz-model">model</label><input type="text" id="wz-model" name="model" required placeholder="e.g. claude-opus-5" autocomplete="off" /></div>
        <div class="field"><label for="wz-base-url">base URL</label><input type="text" id="wz-base-url" name="base_url" placeholder="blank = driver default" autocomplete="off" /></div>
        <div class="field"><label for="wz-apikeyenv">api key env var</label><input type="text" id="wz-apikeyenv" name="api_key_env" placeholder="e.g. ANTHROPIC_API_KEY" autocomplete="off" /></div>
        <div class="field"><label for="wz-provider">oauth provider</label><input type="text" id="wz-provider" name="provider" placeholder="codex | grok" autocomplete="off" /></div>
      </div>
      <div class="toolbar">
        <button class="btn btn-primary" type="submit">save model</button>
        <span class="toolbar-note">the daemon reads the key from the named env var at run time</span>
      </div>
      <output class="feedback" data-wz-feedback></output>
    </form>
    <div class="toolbar"><button class="btn" data-act="llm-wizard-skip">skip — keep defaults</button></div>
  </div>`;
}

function applyWizardOAuth(provider) {
  const form = document.querySelector("[data-llm-wizard-form]");
  if (!form) return;
  form.elements.driver.value = "oauth";
  form.elements.provider.value = provider;
  form.elements.model.placeholder =
    provider === "codex" ? "e.g. gpt-5.6-codex" : "the model name your grok subscription uses";
  const modelInput = form.elements.model;
  modelInput.focus();
  modelInput.select();
}

async function submitWizard(form) {
  const feedback = form.querySelector("[data-wz-feedback]");
  const data = new FormData(form);
  const driver = String(data.get("driver") || "").trim();
  const model = String(data.get("model") || "").trim();
  if (!driver || !model) {
    if (feedback) feedback.innerHTML = errorInline("driver and model are required");
    return;
  }
  const payload = { driver, model };
  for (const name of ["base_url", "api_key_env", "provider"]) {
    const value = String(data.get(name) || "").trim();
    if (value) payload[name] = value;
  }
  if (feedback) feedback.innerHTML = '<span class="dim">testing connectivity…</span>';
  try {
    // MUST-FIX 2: connectivity-test-before-persist. The route endpoint rejects
    // a persist that was not probed first, so probe the exact signature and
    // refuse to save a failed route.
    const probe = await api("/api/v1/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: "deep_reflection", ...payload }),
    });
    if (!probe.ok) {
      const detail = probe.detail || {};
      const message =
        detail && typeof detail === "object" && !Array.isArray(detail)
          ? detail.error
          : String(detail || "");
      if (feedback) {
        feedback.innerHTML = errorInline(
          `connectivity test failed${message ? `: ${message}` : ""}`
        );
      }
      return;
    }
    if (feedback) feedback.innerHTML = '<span class="dim">saving…</span>';
    const result = await api("/api/v1/llm/routes/deep_reflection", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    finishDreamSetup(`deep reflection → ${result.driver} · ${result.model}`);
  } catch (error) {
    if (feedback) feedback.innerHTML = errorInline(`save failed: ${error.message}`);
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
  return `${banner}
    <div class="toolbar">
      <button class="btn" data-act="go-home">← dashboard</button>
      <button class="btn" data-act="llm-refresh">Refresh</button>
      <span class="spacer"></span>
      <span class="toolbar-note">routes reference env-var NAMES — key values never leave the machine</span>
    </div>
    <div class="card">
      <h2>models &amp; routing</h2>
      <p class="toolbar-note">per-role dream routes: driver, model, endpoint, and the api-key env-var chain. The connectivity probe is live but cached briefly on the daemon (checked ${esc(fmtEpoch(routes.checked_at))}).</p>
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
      const cls = live ? "badge-ok" : entry.present === true ? "badge-warn" : "";
      const label = live
        ? `${entry.provider}: logged in`
        : entry.present === true
          ? `${entry.provider}: expired`
          : `${entry.provider}: not detected`;
      return `<span class="badge ${cls}">${esc(label)}</span>`;
    })
    .join(" ");
  return `<h3>host OAuth</h3><p>${bits}</p>`;
}

function llmRoleCard(role, drivers) {
  const conn = role.connectivity || {};
  const ok = conn.ok === true;
  const editing = state.llm.editingRole === role.role;
  const detail = conn.detail;
  const detailHtml = detail
    ? ` <span class="mono">${esc(typeof detail === "string" ? detail : JSON.stringify(detail))}</span>`
    : "";
  const probe = ok
    ? `probe: <span class="badge badge-ok">reachable</span>${detailHtml}`
    : `probe: <span class="badge badge-err">unreachable</span>${detailHtml}`;
  const envRows = (role.api_key_env || "").split(",").map((name) => name.trim()).filter(Boolean);
  const unknownDriver = drivers.some((d) => d.name === role.driver) ? null : role.driver;
  const driverOptions = []
    .concat(
      unknownDriver
        ? [`<option value="${esc(unknownDriver)}" selected>${esc(unknownDriver)} (unknown)</option>`]
        : [],
    )
    .concat(
      drivers.map(
        (d) =>
          `<option value="${esc(d.name)}" ${d.name === role.driver ? "selected" : ""}>${esc(d.name)}</option>`,
      ),
    )
    .join("");
  return `<div class="card">
    <h2>${esc(role.role)} ${role.explicit ? '<span class="badge badge-accent">configured</span>' : ""}</h2>
    <div class="tiles">
      ${tile(`<span class="mono">${esc(role.driver || "—")}</span>`, "driver")}
      ${tile(`<span class="mono">${esc(role.model || "—")}</span>`, "model")}
      ${tile(esc(role.base_url || "default"), "base URL")}
      ${tile(esc(envRows.join(", ") || "—"), "api key env")}
      ${tile(esc(role.provider || "—"), "oauth provider")}
      ${tile(esc(fmtNum(configRoleMaxTokens(role.role))), "max tokens")}
    </div>
    <h3>connectivity</h3>
    <div class="toolbar">
      <span>${probe}</span>
      <span class="dim">checked ${esc(fmtEpoch(conn.checked_at))}</span>
      <span class="spacer"></span>
      <button class="btn" data-act="llm-test" data-role="${esc(role.role)}" title="probe this saved route now">test connection</button>
      <button class="btn" data-act="llm-edit" data-role="${esc(role.role)}" title="edit this route's config row">${editing ? "cancel edit" : "edit route"}</button>
    </div>
    <output class="feedback" data-llm-feedback data-feedback-role="${esc(role.role)}"></output>
    ${editing ? llmEditFormHtml(role, driverOptions) : ""}
  </div>`;
}

function llmEditFormHtml(role, driverOptions) {
  const maxTokens = configRoleMaxTokens(role.role);
  return `<form class="card" data-llm-route-form data-role="${esc(role.role)}">
    <h3>edit route · ${esc(role.role)}</h3>
    <div class="filter-grid">
      <div class="field"><label for="llm-driver-${esc(role.role)}">driver</label><select id="llm-driver-${esc(role.role)}" name="driver">${driverOptions}</select></div>
      <div class="field"><label for="llm-model-${esc(role.role)}">model</label><input type="text" id="llm-model-${esc(role.role)}" name="model" value="${esc(role.model || "")}" required autocomplete="off" /></div>
      <div class="field"><label for="llm-url-${esc(role.role)}">base URL</label><input type="text" id="llm-url-${esc(role.role)}" name="base_url" value="${esc(role.base_url || "")}" placeholder="blank = default" autocomplete="off" /></div>
      <div class="field"><label for="llm-env-${esc(role.role)}">api key env var</label><input type="text" id="llm-env-${esc(role.role)}" name="api_key_env" value="${esc(role.api_key_env || "")}" placeholder="MY_API_KEY" autocomplete="off" /></div>
      <div class="field"><label for="llm-provider-${esc(role.role)}">oauth provider</label><input type="text" id="llm-provider-${esc(role.role)}" name="provider" value="${esc(role.provider || "")}" placeholder="codex | grok" autocomplete="off" /></div>
      <div class="field"><label for="llm-tokens-${esc(role.role)}">max tokens</label><input type="number" id="llm-tokens-${esc(role.role)}" name="max_tokens" value="${esc(maxTokens === null ? "" : String(maxTokens))}" min="1" placeholder="blank = role default" autocomplete="off" /></div>
    </div>
    <div class="toolbar">
      <span class="toolbar-note">blank base URL / env var / max tokens clears the override, restoring defaults</span>
      <span class="spacer"></span>
      <button class="btn" type="button" data-act="llm-test-edit" data-role="${esc(role.role)}">test connection</button>
      <button class="btn btn-primary" type="submit">save route</button>
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
  const payload = { role, driver, model, base_url: baseUrl || "" };
  if (apiKeyEnv && String(apiKeyEnv).trim()) payload.api_key_env = String(apiKeyEnv).trim();
  if (provider && String(provider).trim()) payload.provider = String(provider).trim();
  if (feedbackEl) feedbackEl.innerHTML = '<span class="dim">probing…</span>';
  try {
    const result = await api("/api/v1/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const detail = String(result.detail && typeof result.detail === "object" ? JSON.stringify(result.detail) : result.detail || "");
    if (result.ok) {
      // A passing probe for the exact current form values arms the save gate.
      state.llm.probeOk[role] = llmProbeSignature(driver, model, baseUrl || "", apiKeyEnv || "", provider || "");
    } else {
      delete state.llm.probeOk[role];
    }
    if (feedbackEl) {
      feedbackEl.innerHTML = result.ok
        ? `<span class="ok-inline">reachable — ${esc(detail)}</span>`
        : errorInline(`unreachable — ${esc(detail)}`);
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
    if (feedback) feedback.innerHTML = errorInline("driver and model are required to save");
    return;
  }
  const baseUrl = String(data.get("base_url") || "").trim();
  const apiKeyEnv = String(data.get("api_key_env") || "").trim();
  const provider = String(data.get("provider") || "").trim();
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
    if (feedback) feedback.innerHTML = errorInline("test connection first — a route may only be saved after a passing probe of these exact values");
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
      testRoute(
        editRole,
        String(editData.get("driver") || "").trim(),
        String(editData.get("model") || "").trim(),
        String(editData.get("base_url") || "").trim(),
        String(editData.get("api_key_env") || "").trim(),
        String(editData.get("provider") || "").trim(),
        editForm.querySelector("[data-llm-feedback]"),
      );
      break;
    }
    case "llm-wizard-oauth": {
      const provider = el.dataset.provider;
      const entry = (state.llm.oauth && state.llm.oauth.providers || []).find(
        (candidate) => candidate.provider === provider,
      );
      if (!entry || entry.present !== true || entry.expired === true) break;
      applyWizardOAuth(provider);
      break;
    }
    case "llm-wizard-skip":
      finishDreamSetup("defaults kept — no route was changed; configure one any time in the Models view");
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
    submitWizard(form);
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

// ---------------------------------------------------------------- boot
document.addEventListener("click", handleClick);
document.addEventListener("change", handleChange);
document.addEventListener("submit", handleSubmit);
window.addEventListener("hashchange", render);

// The identity gate decides the first paint: setup wizard, login view, or the
// app. A deep link to /console/#/setup (the gate's setup_url) resolves through
// the same probe: setup mode shows the wizard, otherwise login.
boot();
