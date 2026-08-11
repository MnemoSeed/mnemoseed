"""Codex host config editing (``~/.codex/config.toml``).

Codex keeps its MCP servers in ``[mcp_servers.<name>]`` tables inside the
TOML config (per developers.openai.com/codex/mcp), not a separate JSON file.
Editing is text-surgical: tomllib only locates and validates; the mnemoseed
table is inserted, replaced or removed line-wise so every other byte
(comments, key order, line endings, other servers) is untouched. Errors mirror
the JSON hosts: nothing is written unless the whole file parses.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from mnemoseed.installer.hosts import MCP_SERVERS_TOML_KEY, MNEMOSEED_KEY, HostConfigError

_TABLE_RE = re.compile(r"^\s*\[(\[?)\s*([^\]]+?)\s*(\]?)\]\s*(?:#.*)?$")


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def parse_toml(text: str, path: Path) -> dict[str, Any]:
    """Parse and validate TOML text; a typed error on empty/invalid/non-table."""
    if not text.strip():
        raise HostConfigError(path, "config file is empty; refusing to overwrite")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HostConfigError(path, f"invalid TOML ({exc})") from exc
    if not isinstance(data, dict):
        raise HostConfigError(path, "config is not a TOML table")
    return data


def load_host_toml(path: Path, *, missing_ok: bool) -> dict[str, Any]:
    """Read a Codex config as a TOML table (mirrors the JSON host loader)."""
    if not path.exists():
        if missing_ok:
            return {}
        raise HostConfigError(path, "config file not found")
    try:
        return parse_toml(path.read_text(encoding="utf-8"), path)
    except OSError as exc:
        raise HostConfigError(path, f"unreadable config ({exc})") from exc


def _header_path(line: str) -> str | None:
    match = _TABLE_RE.match(line)
    if match is None:
        return None
    inner = match.group(2).strip()
    if inner.startswith('"') or inner.startswith("'"):
        return None  # quoted names are left alone
    return inner


def _is_header(line: str) -> bool:
    return _header_path(line) is not None


def _find_header(lines: list[str], dotted_path: str) -> int:
    for index, line in enumerate(lines):
        if _header_path(line) == dotted_path:
            return index
    return -1


def _section_end(lines: list[str], start: int) -> int:
    index = start + 1
    while index < len(lines) and not _is_header(lines[index]):
        index += 1
    return index


def _toml_value(value: Any) -> str:
    """Render a scalar or array the way TOML spells it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        # json.dumps escapes are a subset of TOML basic-string escapes; never
        # emits \\/ which TOML rejects.
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


def render_codex_block(entry: dict[str, Any], eol: str) -> str:
    """The exact text of the ``[mcp_servers.mnemoseed]`` table (plus its env)."""
    command = entry.get("command", "mnemoseed")
    args = entry.get("args") or []
    lines: list[str] = [
        f"[{MCP_SERVERS_TOML_KEY}.{MNEMOSEED_KEY}]{eol}",
        f"command = {_toml_value(command)}{eol}",
        f"args = {_toml_value(args)}{eol}",
    ]
    env = entry.get("env")
    if isinstance(env, dict) and env:
        lines.append(f"{eol}[{MCP_SERVERS_TOML_KEY}.{MNEMOSEED_KEY}.env]{eol}")
        for key, value in env.items():
            lines.append(f"{key} = {_toml_value(value)}{eol}")
    return "".join(lines)


def merge_codex(text: str, entry: dict[str, Any]) -> str:
    """Insert or replace the mnemoseed table, preserving every other byte.

    ``text`` is the current config ("" for a fresh file). The mnemoseed table
    is appended right after the last existing ``[mcp_servers.*]`` section, or
    at the end of the file when none exists; an existing mnemoseed table is
    replaced in place (its old body, including any old env sub-table, goes).
    """
    eol = _eol(text)
    lines = text.splitlines(keepends=True)
    block = render_codex_block(entry, eol)
    block_lines = block.splitlines(keepends=True)

    header_idx = _find_header(lines, f"{MCP_SERVERS_TOML_KEY}.{MNEMOSEED_KEY}")
    if header_idx < 0:
        insertion = _insertion_index(lines)
        before = lines[:insertion]
        blank = [eol] if insertion > 0 and lines[insertion - 1].strip() != "" else []
        after = lines[insertion:]
        separator = [eol] if after else []
        rebuilt = "".join(before + blank + block_lines + separator + after)
        return rebuilt

    end = _section_end(lines, header_idx)
    while end > header_idx + 1 and lines[end - 1].strip() == "":
        end -= 1
    rebuilt = "".join(lines[:header_idx] + block_lines + lines[end:])
    return rebuilt


def _insertion_index(lines: list[str]) -> int:
    """Splice point for a new mcp_servers child: end of the last such section."""
    where = len(lines)
    for index, line in enumerate(lines):
        path = _header_path(line)
        if path is None or not (path == MCP_SERVERS_TOML_KEY or path.startswith(MCP_SERVERS_TOML_KEY + ".")):
            continue
        end = _section_end(lines, index)
        while end > index and lines[end - 1].strip() == "":
            end -= 1
        where = end
    return where


def remove_codex(text: str) -> str:
    """Byte-surgical removal of the mnemoseed table (including its env sub-table)."""
    lines = text.splitlines(keepends=True)
    header_idx = _find_header(lines, f"{MCP_SERVERS_TOML_KEY}.{MNEMOSEED_KEY}")
    if header_idx < 0:
        return text
    end = _section_end(lines, header_idx)
    while end > header_idx + 1 and lines[end - 1].strip() == "":
        end -= 1
    start = header_idx
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1
    return "".join(lines[:start] + lines[end:])
