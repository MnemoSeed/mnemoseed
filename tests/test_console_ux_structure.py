"""Console Models & Routing UX structure guards (模型路由配置-UX §7, §11).

Js-dom-free structure tests: they read the static assets and the CLI/onboard
sources as plain text and assert the spec's verbatim UI strings plus the
absence of dead inputs (a provider text input, a stub picker option, the
old key teaching line). Real wire behavior is covered by
``tests/test_console_llm_e2e.py``; these guards only pin the spec copy and
the form skeleton so a future refactor that keeps them green needs no
rewrites here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_JS = REPO / "src" / "mnemoseed" / "console" / "static" / "app.js"
STYLES_CSS = REPO / "src" / "mnemoseed" / "console" / "static" / "styles.css"
SERVICE = REPO / "src" / "mnemoseed" / "onboard" / "service.py"
CLI = REPO / "src" / "mnemoseed" / "cli.py"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def service() -> str:
    return SERVICE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles_css() -> str:
    return STYLES_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cli() -> str:
    return CLI.read_text(encoding="utf-8")


def _providers_block(app_js: str) -> str:
    match = re.search(r"const LLM_PROVIDERS = \[(.*?)\n\];", app_js, re.DOTALL)
    assert match is not None, "LLM_PROVIDERS constant not found in app.js"
    return match.group(1)


# ---------------------------------------------------------------- provider pickers


def test_wizard_provider_picker_lists_the_five_providers(app_js: str) -> None:
    """§11.1: the wizard picker is exactly Fireworks/OpenRouter/Anthropic/
    Ollama/Other — never stub, never an oauth card."""
    block = _providers_block(app_js)
    for label in (
        "Fireworks (recommended)",
        "OpenRouter",
        "Anthropic (Claude)",
        "Ollama on this computer",
        "Another OpenAI-compatible API",
    ):
        assert label in block
    assert "stub" not in block
    assert "oauth" not in block


def test_provider_catalog_notes_are_verbatim(app_js: str) -> None:
    block = _providers_block(app_js)
    for note in (
        "Recommended starting point — MnemoSeed's default models run here.",
        "One API key, hundreds of models from many labs.",
        "Requires an Anthropic API key from platform.claude.com.",
        "Free and offline. Runs entirely on this machine; lower synthesis quality.",
        "Point at any other endpoint that speaks the OpenAI chat API.",
    ):
        assert note in block


def test_editor_never_renders_a_provider_text_input(app_js: str) -> None:
    """§11.2: the editor carries the provider through the picked card only —
    no oauth/provider free-text field survives anywhere in the console."""
    assert 'placeholder="codex | grok"' not in app_js
    assert 'name="provider"' not in app_js
    assert "oauth provider" not in app_js


def test_editor_form_has_the_route_fields(app_js: str) -> None:
    for field in ('name="model"', 'name="base_url"', 'name="api_key_env"', 'name="max_tokens"'):
        assert field in app_js


def test_dead_wizard_inputs_removed(app_js: str) -> None:
    assert 'placeholder="e.g. claude-opus-5"' not in app_js
    assert "the daemon reads the key from the named env var at run time" not in app_js


# ------------------------------------------------- ⑧ editor host-login cards (§6.3)


def test_editor_oauth_cards_cover_all_visibility_states(app_js: str) -> None:
    """§6.3: the editor renders a "Reuse <provider> login" card per oauth
    provider in every availability state — live selectable, expired and absent
    visible-but-disabled with a re-login command; never a free-text field."""
    for string in (
        "Reuse ",
        "login expired — run ",
        " CLI login detected — log in first",
        "codex login",
        "grok login",
    ):
        assert string in app_js


def test_editor_oauth_paste_a_token_affordance(app_js: str) -> None:
    """The paste-a-token path under the oauth cards: an openable key paste
    field bound to the provider, the daemon key endpoint, and the official
    docs link for the token (verified: developers.openai.com/codex/auth)."""
    for string in (
        "paste a token instead",
        'data-act="llm-key-paste"',
        "/api/v1/llm/key",
        "developers.openai.com/codex/auth",
    ):
        assert string in app_js


def test_editor_oauth_gate_blocks_expired_route(app_js: str) -> None:
    """JH: SAVE/TEST is blocked for a route whose oauth login is expired or
    absent until availability returns; the block is per-route, enforced in the
    probe and save paths, with the fix message rendered inline."""
    assert "llmOauthLive" in app_js
    assert "llmOauthBlockMessage" in app_js
    assert "llmSyncEditorGate" in app_js


# ------------------------------------------------- provider-scoped model picker (§3.2/§7.2)


def test_provider_scoped_curated_model_ids_are_verified(app_js: str) -> None:
    """§7.2/D9: curated suggestions are per-provider and every id was verified
    against a live catalog or the provider's official docs before shipping
    (Fireworks live catalog, openrouter.ai/api/v1/models keyless fetch,
    platform.claude.com model docs, ollama library tags)."""
    block = _providers_block(app_js)
    for string in (
        # fireworks — default routes, verified in config.py comments
        "accounts/fireworks/models/kimi-k3",
        "accounts/fireworks/models/deepseek-v4-flash-0731",
        # openrouter — served ids confirmed on openrouter.ai/api/v1/models
        "deepseek/deepseek-v4-flash",
        "moonshotai/kimi-k3",
        "anthropic/claude-opus-5",
        "qwen/qwen3-coder-plus",
        # anthropic — current API ids from the official models overview
        "claude-opus-5",
        "claude-sonnet-5",
        # ollama — ollama.com/library tags
        "llama3.1:8b",
        "qwen3:8b",
        "deepseek-r1:8b",
    ):
        assert string in block


def test_load_model_list_control_present(app_js: str) -> None:
    """§7.2: before any probe the picker offers curated suggestions plus a
    "Load model list" button that runs the probe to fetch the catalog."""
    for string in ("Load model list", 'data-act="llm-load-models"'):
        assert string in app_js


def test_editor_model_datalist_follows_selected_provider(app_js: str) -> None:
    """The editor datalist is provider-scoped (per-provider probe catalog in
    state), never the stale route's catalog when the card switches."""
    assert "state.llm.catalog" in app_js
    assert "llmRoleDefaultModel" in app_js


def test_role_card_model_reflects_live_editor_model(app_js: str) -> None:
    """Picking a provider in the editor must re-seed the model and the role
    card tile must reflect that live value — no 'anthropic picked, kimi-k3
    still shown' state."""
    assert "data-model-tile" in app_js
    assert "state.llm.editModel" in app_js


# ------------------------------------------------- overflow containment (role-card values)


def test_role_card_overflow_css_rules_present(styles_css: str) -> None:
    """Long unbroken role-card values (model ids, env chains) must wrap or be
    clamped, never spill out of the tile or the card at any width."""
    tile_value_rule = re.search(r"\.tile \.tile-value \{([^}]*)\}", styles_css)
    assert tile_value_rule is not None, ".tile .tile-value rule missing"
    assert "overflow-wrap: anywhere" in tile_value_rule.group(1)
    assert "word-break: break-word" in tile_value_rule.group(1)
    tile_rule = re.search(r"\.tile \{([^}]*)\}", styles_css)
    assert tile_rule is not None, ".tile rule missing"
    assert "min-width: 0" in tile_rule.group(1)


# ---------------------------------------------------------------- ⑧ models & routing copy (§11.2)


def test_routing_page_verbatim_copy(app_js: str) -> None:
    for string in (
        "models & routing",
        (
            "What each role does, and which model serves it. Key values never appear here — "
            "only the env-var names MnemoSeed reads them from."
        ),
        "fully offline — nothing leaves this machine",
        "Model routing is system-scoped — set by the owner/admin and applies to every user.",
        "Test the connection first — a route can only be saved after a passing probe of these exact values.",
        (
            "Remember: the daemon reads env vars from its own startup environment. "
            "If you set a new one, restart MnemoSeed."
        ),
        "host logins:",
        "defaults",
    ):
        assert string in app_js


def test_role_card_subtitles_are_verbatim(app_js: str) -> None:
    for string in (
        (
            "The careful model. Reads your recent sessions and writes the distilled facts "
            "into long-term memory. Use the strongest model you can afford here."
        ),
        "The quick model. Handles the frequent small consolidation passes. Use a fast, low-cost model.",
        "lower synthesis quality than cloud models — you accept this for privacy or cost.",
    ):
        assert string in app_js


def test_role_card_probe_and_key_line(app_js: str) -> None:
    for string in ("connected", "needs attention", "key:"):
        assert string in app_js
    assert "→" in app_js  # the env-var name -> env-var name key line


def test_offline_badge_is_derived_from_the_routes(app_js: str) -> None:
    """§6.5: offline is a derived state (every role on ollama), never a stored flag."""
    assert "isFullyOffline" in app_js


# ---------------------------------------------------------------- first-run wizard copy (§11.1)


def test_wizard_verbatim_copy(app_js: str) -> None:
    for string in (
        "dream model",
        (
            "Pick the model that distills your sessions into long-term memory. "
            "One model gets you started — you can change any role later in Models."
        ),
        "Which provider do you use?",
        "api key env var",
        (
            "Your key lives in an environment variable. MnemoSeed reads it from there — "
            "you never paste the key here and it is never stored."
        ),
        "Advanced: endpoint",
        "reset to Fireworks default",
        "type or pick a model",
        "No models listed — pick a suggestion or type the exact model id.",
        "Testing connection to ",
        "Connected — key in ",
        "dream model configured: deep reflection → ",
        "deep reflection + short increment → ",
        "Skip for now — capture-only (dreaming stays off)",
        (
            "Skipped — MnemoSeed keeps capturing sessions, dreaming stays off until a model is "
            "configured. You can set one any time in Models."
        ),
        "Lower synthesis quality than cloud models — you accept this for privacy or cost.",
    ):
        assert string in app_js


def test_wizard_share_checkbox_copy(app_js: str) -> None:
    for string in (
        "also apply to short_increment",
        "Uses the same provider and key for the quick consolidation model.",
    ):
        assert string in app_js


def test_wizard_share_writes_both_roles_in_spa(app_js: str) -> None:
    """§6/D4: checking the share box must make wizardSave POST the payload to
    BOTH roles — this block is the only transport for the share semantics."""
    assert "if (wizard.share)" in app_js
    assert '"/api/v1/llm/routes/short_increment"' in app_js
    assert '"/api/v1/llm/routes/deep_reflection"' in app_js


def test_wizard_oauth_panel_copy(app_js: str) -> None:
    for string in (
        "Or reuse a login already on this computer",
        "MnemoSeed uses that login's access — you don't paste a key. No key value is read, sent, or stored.",
        "login found — sign in is current.",
        "login found but expired — sign in again with the ${providerName} CLI, then return here.",
        "No ${providerName} login detected on this machine.",
        "Use ${providerName} login",
    ):
        assert string in app_js


# ---------------------------------------------------------------- probe plain-language mapping (§7.1)


def test_probe_plain_language_messages_present(app_js: str) -> None:
    for string in (
        "rejected the key in ",
        "— it's missing, wrong, or expired",
        "Can't reach Ollama at ",
        "— is the Ollama app running? Install from ollama.com, then pull a model (ollama pull llama3.1:8b).",
        "Couldn't reach ",
        "Check your internet connection or firewall, then try again.",
        "Timed out talking to ",
        "The endpoint may be slow or blocked — check ",
        "That connection type isn't built in — go back and pick a provider.",
    ):
        assert string in app_js


# ---------------------------------------------------------------- onboard CLI step copy (§11.3)


def _joined_string_constants(source: str) -> str:
    """Reassemble every string literal in source order, including the constant
    fragments inside f-strings, so strings that the implementation splits
    across adjacent literals (line-length hygiene) still read as one text."""
    tree = ast.parse(source)

    def _collect(node: ast.AST, out: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                out.append(child.value)
            _collect(child, out)

    out: list[str] = []
    _collect(tree, out)
    return re.sub(r"\s+", " ", " ".join(out))


def test_onboard_llm_step_verbatim_copy(service: str) -> None:
    joined = _joined_string_constants(service)
    for string in (
        (
            "Pick the model that distills your sessions into long-term memory. "
            "One model gets you started; change any role later with 'mnemoseed llm set'."
        ),
        "1) Fireworks (recommended)",
        "2) OpenRouter",
        "3) Anthropic",
        "4) Ollama on this computer",
        "5) other OpenAI-compatible",
        "provider [1]:",
        "model [accounts/fireworks/models/kimi-k3]:",
        (
            "Paste your API key now — stored locally under ~/.mnemoseed/secrets, "
            "never shown again (no restart needed; the next dream run picks it up)."
        ),
        "Advanced: leave it empty to use the ",
        "testing connection to ",
        "connected — key works. saving…",
        "also apply to short_increment? [y/N]:",
        (
            "Ollama chosen for this role — lower synthesis quality than cloud models; "
            "you accept this for privacy or cost."
        ),
        "dream model configured (",
        (
            "as an env var) and re-run onboard (it resumes here); no restart "
            "needed, the next dream run picks the key up."
        ),
        "can't reach Ollama at ",
        "— is it running? Install from ollama.com and pull a model (ollama pull llama3.1:8b).",
        (
            "skipping the LLM wizard: the daemon stays capture-only "
            "(dreaming disabled until a model is configured)"
        ),
    ):
        assert re.sub(r"\s+", " ", string) in joined


# ---------------------------------------------------------------- llm set CLI


def test_cli_llm_set_help_names_two_roles_not_local_track(cli: str) -> None:
    assert "dream role (deep_reflection, short_increment)" in cli


def test_cli_local_track_removal_is_a_typed_error(cli: str) -> None:
    """§11.3: local_track was removed from the user surface; an explicit llm
    set of it must answer with a clean deprecation, not a 422 surprise."""
    assert "local_track" in cli
    assert "was removed" in cli
