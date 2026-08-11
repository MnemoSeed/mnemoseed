"""PRD-06 T3 -- Claude Code plugin manifest contract tests.

Validates the marketplace-ready layout against the Claude Code plugin spec:
plugin.json naming rules, ./-prefixed relative paths, the hooks.json wiring for
all six supported hook events, the four slash-command definitions, and the
marketplace manifest at the repo root.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "claude-code"
_REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "PreCompact",
    "Stop",
    "SessionEnd",
)

EXPECTED_COMMANDS = ("memory", "dream", "forget", "recall")


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_json_is_valid_and_well_named() -> None:
    manifest = _load_json(_PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "mnemoseed"
    assert re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", manifest["name"])
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert "description" in manifest


def test_plugin_json_relative_paths_start_with_dot_slash() -> None:
    manifest = _load_json(_PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    for key in ("commands", "hooks"):
        value = manifest[key]
        assert isinstance(value, str), key
        assert value.startswith("./"), key
    assert (_PLUGIN_ROOT / manifest["commands"]).is_dir()
    assert (_PLUGIN_ROOT / manifest["hooks"]).is_file()


def test_plugin_json_registers_the_mcp_server() -> None:
    manifest = _load_json(_PLUGIN_ROOT / ".claude-plugin" / "plugin.json")
    servers = manifest["mcpServers"]
    assert "mnemoseed" in servers
    entry = servers["mnemoseed"]
    assert entry["command"] == "mnemoseed"
    assert entry["args"] == ["mcp"]


def test_plugin_json_mcp_server_expands_profile_env() -> None:
    """D4: the plugin-managed MCP server inherits the profile identity and any
    daemon token through ${VAR} expansion in its env block, so the plugin stays
    self-contained and profile-correct without the installer."""
    entry = _load_json(_PLUGIN_ROOT / ".claude-plugin" / "plugin.json")["mcpServers"]["mnemoseed"]
    assert "env" in entry
    assert entry["env"] == {
        "MNEMOSEED_PROFILE_ID": "${MNEMOSEED_PROFILE_ID}",
        "MNEMOSEED_TOKEN": "${MNEMOSEED_TOKEN}",
    }


def test_hooks_json_wires_all_six_events_to_existing_scripts() -> None:
    hooks = _load_json(_PLUGIN_ROOT / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == set(EXPECTED_HOOK_EVENTS)
    hook_event_names = {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt_submit",
        "PostToolUse": "post_tool_use",
        "PreCompact": "pre_compact",
        "Stop": "stop",
        "SessionEnd": "session_end",
    }
    for event, script in hook_event_names.items():
        blocks = hooks[event]
        assert len(blocks) >= 1, event
        commands = [entry["type"] for block in blocks for entry in block["hooks"] if "type" in entry]
        assert commands, event
        command = blocks[0]["hooks"][0]["command"]
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" in command, event
        assert f"{script}.py" in command, event
        assert isinstance(blocks[0]["hooks"][0].get("timeout"), int)
        assert (_PLUGIN_ROOT / "hooks" / f"{script}.py").is_file()


def test_hooks_shim_resolves_an_interpreter() -> None:
    shim = (_PLUGIN_ROOT / "hooks" / "py.sh").read_text(encoding="utf-8")
    for probe in ("python3", "python", "py"):
        assert probe in shim
    assert "exec" in shim  # the resolved interpreter replaces this shell process


def test_all_four_slash_commands_are_defined_with_frontmatter() -> None:
    command_dir = _PLUGIN_ROOT / "commands"
    assert {path.stem for path in command_dir.glob("*.md")} == set(EXPECTED_COMMANDS)
    with_argument_hint = {"recall", "dream", "forget"}  # only arg-taking commands hint
    for name in EXPECTED_COMMANDS:
        text = (command_dir / f"{name}.md").read_text(encoding="utf-8")
        assert text.startswith("---"), name  # YAML frontmatter opens the file
        assert "description:" in text, name
        assert "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" in text, name
        assert (name in with_argument_hint) == ("argument-hint:" in text), name


def test_each_command_script_exists_under_scripts() -> None:
    for name in EXPECTED_COMMANDS:
        script = _PLUGIN_ROOT / "scripts" / f"{name}.py"
        assert script.is_file(), name
        assert script.read_text(encoding="utf-8").startswith('"""')


def test_marketplace_manifest_points_at_the_plugin() -> None:
    marketplace = _load_json(_REPO_ROOT / ".claude-plugin" / "marketplace.json")
    assert "plugins" in marketplace and marketplace["plugins"]
    entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "mnemoseed")
    source = entry["source"]
    assert source.startswith("./"), source
    assert (_REPO_ROOT / source).is_dir()
    assert (_REPO_ROOT / source / ".claude-plugin" / "plugin.json").is_file()
