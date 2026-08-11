"""Host detection and the per-host registration entry.

Detection is marker based: Claude Code lives in ``~/.claude.json``, Cursor
under ``~/.cursor/``, Codex CLI under ``~/.codex/config.toml``. The user home
is never hardcoded deep in the logic: every function takes an explicit
``home`` path and the CLI resolves it from the environment
(``MNEMOSEED_USER_HOME``, defaulting to the OS user home), so tests point
every file effect at a tmp directory.

Design/06 section 6 keeps host-side state a thin registration: one ``mnemoseed``
MCP server entry whose profile identity is carried as env (design/06 section
2.6).

Writing is text-surgical: JSON files are re-serialized without re-sorting keys
and with the original indentation and line endings preserved; Codex's config.toml
is edited line-wise. Nothing is ever written through a path that would mangle
line endings on Windows.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mnemoseed import config as _config

USER_HOME_ENV = "MNEMOSEED_USER_HOME"

# The env keys carrying profile identity inside the registration (design/06 2.6).
PROFILE_ID_ENV = "MNEMOSEED_PROFILE_ID"
TOKEN_ENV = "MNEMOSEED_TOKEN"

MCP_SERVERS_KEY = "mcpServers"
MCP_SERVERS_TOML_KEY = "mcp_servers"
MNEMOSEED_KEY = "mnemoseed"

_WS = " \t\r\n"


class HostConfigError(ValueError):
    """A host config file cannot be read or merged safely (never data loss)."""

    def __init__(self, path: Path, message: str) -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


@dataclass(frozen=True)
class HostSpec:
    """One supported host, where it lives under the user home, and the config
    file format (``"json"`` or ``"toml"``) the registration is written in."""

    name: str
    display: str
    config: Path  # the config file that receives the mnemoseed registration
    marker: Path  # path whose existence marks the host as installed
    format: str = "json"

    def describe(self) -> str:
        return f"{self.display} ({self.name})"


def resolve_home(home: Path | None = None) -> Path:
    """The user home hosting host configs; env-overridable for tests/CLI."""
    if home is not None:
        return Path(home)
    raw = os.environ.get(USER_HOME_ENV)
    return Path(raw).expanduser() if raw else Path.home()


def resolve_data_dir(data_dir: Path | None = None) -> Path:
    """The MnemoSeed data dir (backups / state / pidfile live under it).

    Referenced through the config module so monkeypatched test overrides of
    ``mnemoseed.config.CONFIG_DIR`` are honoured.
    """
    return data_dir if data_dir is not None else _config.CONFIG_DIR


def host_specs(home: Path) -> tuple[HostSpec, ...]:
    """Every supported host's config target and detection marker."""
    return (
        HostSpec("claude-code", "Claude Code", home / ".claude.json", home / ".claude.json"),
        HostSpec("cursor", "Cursor", home / ".cursor" / "mcp.json", home / ".cursor"),
        HostSpec("codex", "Codex CLI", home / ".codex" / "config.toml", home / ".codex", format="toml"),
    )


def detect_hosts(home: Path | None = None) -> list[HostSpec]:
    """Detect installed hosts by their config markers (FR-6.1)."""
    home = resolve_home(home)
    return [spec for spec in host_specs(home) if spec.marker.exists()]


def mnemoseed_mcp_entry(
    command: str = "mnemoseed",
    *,
    profile_id: str | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    """The single ``mnemoseed`` MCP server entry written into every host config.

    Design/06 section 2.6: the host UI sees exactly one mnemoseed MCP entry and
    identity is carried as env inside it. Token issuance is FR-6.1b/c (later);
    the structure is ready, but the env keys are only present once a profile_id
    and a token are supplied.
    """
    entry: dict[str, Any] = {"command": command, "args": ["mcp"]}
    env: dict[str, str] = {}
    if profile_id is not None:
        env[PROFILE_ID_ENV] = profile_id
    if token is not None:
        env[TOKEN_ENV] = token
    if env:
        entry["env"] = env
    return entry


def load_host_json(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    """Read a host config as a JSON object.

    ``missing_ok=True`` returns an empty object for an absent file (a fresh
    registration target). An existing file must be a JSON object; empty or
    invalid content raises :class:`HostConfigError` so nothing is ever
    overwritten behind the user's back.
    """
    if not path.exists():
        if missing_ok:
            return {}
        raise HostConfigError(path, "config file not found")
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        raise HostConfigError(path, "config file is empty; refusing to overwrite")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HostConfigError(path, f"invalid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise HostConfigError(path, "config is not a JSON object")
    return data


def _detect_indent(text: str) -> int:
    """The indent unit (in spaces) used by an existing JSON document."""
    leads: list[int] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.lstrip(" \t").startswith("}"):
            continue
        lead = line[: len(line) - len(line.lstrip(" \t"))]
        if lead and "\t" not in lead:
            leads.append(len(lead))
        elif "\t" in lead:
            return 2
    return min(leads) if leads else 2


def json_text(data: dict[str, Any], *, raw: str = "") -> str:
    """Serialization for writes and diffs.

    Key order follows the ``data`` dict (insertion order from the parsed
    original, never re-sorted); indentation is detected from the existing file
    text and the original line ending preserved, so an edit to an already
    formatted file stays byte-stable apart from the edited entry.
    """
    indent = _detect_indent(raw) if raw else 2
    eol = "\r\n" if "\r\n" in raw else "\n"
    rendered = json.dumps(data, indent=indent, ensure_ascii=False, sort_keys=False)
    if eol == "\r\n":
        rendered = rendered.replace("\n", eol)
    return rendered


def json_file_text(data: dict[str, Any], *, raw: str = "") -> str:
    """The exact text ``write_json_file`` would write for ``data``."""
    text = json_text(data, raw=raw)
    if raw:
        eol = "\r\n" if "\r\n" in raw else "\n"
        if raw.endswith(eol):
            text += eol
    else:
        text += "\n"
    return text


def write_json_file(path: Path, data: dict[str, Any], *, raw: str = "") -> None:
    """Write a host config, preserving original key order / indent / line ending.

    `raw` is the pre-edit file text (the backup source) so the written bytes
    keep the file's existing newline convention; a fresh file uses LF. Binary
    write avoids any platform newline translation.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_file_text(data, raw=raw).encode("utf-8"))


def diff_json(before: dict[str, Any], after: dict[str, Any], label: str, *, raw: str = "") -> str:
    """Unified diff between two host config objects; reflects the real bytes."""
    from difflib import unified_diff

    lines_before = json_text(before, raw=raw).splitlines(keepends=True)
    lines_after = json_text(after, raw=raw).splitlines(keepends=True)
    return "".join(
        unified_diff(
            lines_before,
            lines_after,
            fromfile=f"{label} (current)",
            tofile=f"{label} (planned)",
        )
    )


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WS:
        index += 1
    return index


def _json_field_spans(text: str, obj_start: int) -> list[tuple[str, int, int, int, int | None]]:
    """Members of the JSON object opening at ``obj_start``.

    Each member is ``(key, key_start, value_start, value_end, comma_end)`` where
    ``comma_end`` is one past this member's trailing comma (``None`` for the
    last member). Malformed structure returns ``[]`` so callers refuse to edit.
    """
    decoder = json.JSONDecoder()
    index = _skip_ws(text, obj_start + 1)
    if index >= len(text) or text[index] == "}":
        return []
    spans: list[tuple[str, int, int, int, int | None]] = []
    while index < len(text):
        key_start = index
        try:
            key, index = decoder.raw_decode(text, index)
        except ValueError:
            return []
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            return []
        index = _skip_ws(text, index + 1)
        value_start = index
        try:
            _, value_end = decoder.raw_decode(text, index)
        except ValueError:
            return []
        comma_end: int | None = None
        index = _skip_ws(text, value_end)
        if index < len(text) and text[index] == ",":
            comma_end = index + 1
        spans.append((key, key_start, value_start, value_end, comma_end))
        if comma_end is None:
            break
        index = _skip_ws(text, comma_end)
        if index < len(text) and text[index] == "}":
            break
    return spans


def _drop_json_member(
    text: str,
    obj_start: int,
    spans: list[tuple[str, int, int, int, int | None]],
    index: int,
) -> str:
    """Remove the member at ``index`` from the object opening at ``obj_start``.

    Only the removed member (and, when it was the last member, its separator
    comma) changes: every other byte is kept. A member set collapsing to empty
    is replaced by ``{}``.
    """
    _, _, _, value_end, comma_end = spans[index]
    if index == 0 and len(spans) == 1:
        closing = _skip_ws(text, spans[0][3])
        if closing < len(text) and text[closing] == "}":
            closing += 1
        return text[:obj_start] + "{}" + text[closing:]
    if index == len(spans) - 1:
        if index == 0:
            cut_start = obj_start + 1
        else:
            prev_comma = spans[index - 1][4]
            cut_start = (prev_comma - 1) if prev_comma is not None else value_end
        return text[:cut_start] + text[value_end:]
    cut_start = (spans[index - 1][4] or obj_start + 1) if index > 0 else obj_start + 1
    cut_end = comma_end if comma_end is not None else value_end
    return text[:cut_start] + text[cut_end:]


def surgical_remove_mnemoseed(text: str, path: Path) -> str | None:
    """Byte-surgical removal of the top-level mnemoseed MCP entry.

    The result re-parses and no longer contains the entry; ``None`` means the
    entry was absent. Any other byte (key order, indentation, line endings,
    unrelated entries) is preserved exactly.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HostConfigError(path, f"invalid JSON ({exc.msg})") from exc
    if not isinstance(data, dict):
        raise HostConfigError(path, "config is not a JSON object")
    root = _skip_ws(text, 0)
    if root >= len(text) or text[root] != "{":
        raise HostConfigError(path, "config is not a JSON object")
    root_spans = _json_field_spans(text, root)
    mcp = [index for index, span in enumerate(root_spans) if span[0] == MCP_SERVERS_KEY]
    if not mcp:
        return None
    mcp_index = mcp[0]
    _, _, mcp_value_start, _, _ = root_spans[mcp_index]
    if mcp_value_start >= len(text) or text[mcp_value_start] != "{":
        return None
    server_spans = _json_field_spans(text, mcp_value_start)
    entry = [index for index, span in enumerate(server_spans) if span[0] == MNEMOSEED_KEY]
    if not entry:
        return None
    if len(server_spans) == 1:
        text = _drop_json_member(text, root, root_spans, mcp_index)
    else:
        text = _drop_json_member(text, mcp_value_start, server_spans, entry[0])
    try:
        cleaned = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HostConfigError(path, f"uninstall produced invalid JSON ({exc})") from exc
    servers = cleaned.get(MCP_SERVERS_KEY)
    if isinstance(servers, dict) and MNEMOSEED_KEY in servers:
        raise HostConfigError(path, "uninstall failed to remove the mnemoseed entry")
    return text
