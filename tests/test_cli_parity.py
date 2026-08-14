"""CLI capability parity (PRD-07 FR-7.12, W2): every console action scriptable.

Drives the CLI with a mocked daemon REST (``httpx`` routed into a canned
responder), so the tests assert the wire contract and the rendered output only:

- ``mnemoseed console`` opens the browser at ``{baseurl}/console`` (FR-7.1).
- ``mnemoseed status`` renders the /api/v1/status dashboard row (design/06 5).
- ``mnemoseed recall`` / ``remember`` reuse the MCP /memory endpoints.
- ``mnemoseed dream --once`` / ``export`` / ``diff`` / ``forget`` wrap daemon
  capabilities through the same REST surface.
- ``mnemoseed link`` / ``unlink`` bind/unbind a profile per agent into the
  host's native config (backup + diff + confirm, NFR-6.3).
- ``mnemoseed audit`` queries the audit log with actor/action/time filters.
- Every call forwards ``X-MnemoSeed-Actor: cli``; ``--json`` switches to JSON.
- G-AC5: every CLI verb listed in docs/cli-parity-matrix.md is registered.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlencode

import httpx
import pytest

from mnemoseed.cli import main
from mnemoseed.identity.session import AuthSession, save_session

BASE_URL = "http://localhost:7788"
TOKEN = "parity-test-token"


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> object:
        return self._body


class FakeDaemon:
    """Canned daemon REST responder; records every call the CLI made."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self._routes: dict[tuple[str, str], _FakeResponse] = {}

    def on(self, method: str, path: str, *, status: int = 200, body: object = None) -> None:
        route = (method, path.split("?", 1)[0])
        self._routes[route] = _FakeResponse(status, body if body is not None else {})

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _dispatch(
            method: str, url: str, payload: object, headers: dict[str, str] | None, params: object
        ) -> _FakeResponse:
            query = urlencode(params) if params else ""
            full_url = f"{url}?{query}" if query else url
            self.calls.append(
                {
                    "method": method,
                    "url": full_url,
                    "body": payload,
                    "headers": headers or {},
                    "params": params,
                }
            )
            response = self._routes.get((method, url.split("?", 1)[0]))
            if response is None:
                raise AssertionError(f"no canned route for {method} {full_url}")
            return response

        def get(
            url: str, params: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del timeout
            return _dispatch("GET", url, None, headers, params)

        def post(
            url: str, json: object = None, headers: dict[str, str] | None = None, timeout: object = None
        ) -> _FakeResponse:
            del timeout
            return _dispatch("POST", url, json, headers, None)

        monkeypatch.setattr(httpx, "get", get)
        monkeypatch.setattr(httpx, "post", post)


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, session: AuthSession | None = None) -> Path:
    """Isolate config dir + session file; baseurl stays loopback by default."""
    monkeypatch.setattr("mnemoseed.config.CONFIG_DIR", tmp_path)
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'preset = "embedded"\nbaseurl = "{BASE_URL}"\n', encoding="utf-8")
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_TOKEN", raising=False)
    monkeypatch.delenv("MNEMOSEED_PROFILE_ID", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    token_path = tmp_path / "token.json"
    monkeypatch.setattr("mnemoseed.identity.session.TOKEN_PATH", token_path)
    if session is not None:
        save_session(session, path=token_path)
    return cfg


def _session(profile_id: str = "default") -> AuthSession:
    return AuthSession(base_url=BASE_URL, username="owner", profile_id=profile_id, token=TOKEN)


# ---------------------------------------------------------------- console


def test_console_opens_browser_at_console(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(str(url)) or True)
    code = main(["console"])
    captured = capsys.readouterr()
    assert code == 0
    assert opened == [f"{BASE_URL}/console"]
    assert "console" in captured.out


def test_console_uses_explicit_baseurl(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(str(url)) or True)
    code = main(["console", "--baseurl", "http://127.0.0.1:9000"])
    assert code == 0
    assert opened == ["http://127.0.0.1:9000/console"]


# ---------------------------------------------------------------- status


_STATUS_PAYLOAD = {
    "daemon": {
        "version": "0.1.1",
        "preset": "embedded",
        "drivers": {"vector": "lancedb_embedded", "meta": "sqlite_meta"},
        "gate": {"ok": True},
    },
    "profiles": [
        {
            "profile_id": "default",
            "dream": {"state": "idle", "auto_trigger": False},
            "pool": {"balance": 4.0, "watermark": None},
            "counts": {
                "chunks": 12,
                "nodes": 3,
                "needs_reconcile": 1,
                "pending_consolidation": 0,
            },
            "tokens": {"ledger": {"used_usd": 0.0}},
        }
    ],
}


def test_status_renders_dashboard_table(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on("GET", f"{BASE_URL}/api/v1/status", body=_STATUS_PAYLOAD)
    daemon.install(monkeypatch)
    code = main(["status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "preset embedded" in captured.out
    assert "profile: default" in captured.out
    assert re.search(r"chunks:\s+12", captured.out)
    assert re.search(r"nodes:\s+3", captured.out)
    assert re.search(r"needs_reconcile:\s+1", captured.out)
    assert re.search(r"pending_consolidation:\s+0", captured.out)
    assert re.search(r"pool balance:\s+4.0", captured.out)
    assert re.search(r"dream state:\s+idle", captured.out)


def test_status_json_mode_prints_payload(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.on("GET", f"{BASE_URL}/api/v1/status", body=_STATUS_PAYLOAD)
    daemon.install(monkeypatch)
    code = main(["status", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["daemon"]["preset"] == "embedded"


def test_status_daemon_down_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    daemon = FakeDaemon()
    daemon.install(monkeypatch)

    def fail(url, params=None, headers=None, timeout=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", fail)
    code = main(["status"])
    captured = capsys.readouterr()
    assert code == 1
    assert "cannot reach" in captured.err


# ---------------------------------------------------------------- recall / remember


def test_recall_posts_memory_recall_with_session_identity(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    body = {
        "memory": {
            "entries": [
                {"kind": "chunk", "id": "c1", "text": "user prefers pnpm", "score": 0.81, "flags": []},
                {"kind": "node", "id": "n1", "text": "stack: pnpm", "score": 0.7, "flags": ["pending"]},
            ],
            "coverage": {"vector_hits": 1, "graph_hits": 1, "profile_chunks": 5},
        }
    }
    daemon.on("POST", f"{BASE_URL}/memory/recall", body=body)
    daemon.install(monkeypatch)
    code = main(["recall", "what package manager do I use?"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == f"{BASE_URL}/memory/recall"
    assert call["body"] == {"profile_id": "default", "query": "what package manager do I use?"}
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    assert "user prefers pnpm" in captured.out
    assert "coverage" in captured.out


def test_recall_forwards_top_k(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on("POST", f"{BASE_URL}/memory/recall", body={"memory": {"entries": [], "coverage": {}}})
    daemon.install(monkeypatch)
    code = main(["recall", "q", "--top-k", "7"])
    assert code == 0
    assert daemon.calls[0]["body"]["top_k"] == 7


def test_recall_requires_profile(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)  # no session, no env profile
    daemon = FakeDaemon()
    daemon.install(monkeypatch)
    code = main(["recall", "q"])
    captured = capsys.readouterr()
    assert code == 1
    assert "profile" in captured.err.lower()


def test_remember_posts_memory_remember_and_prints_outcome(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on("POST", f"{BASE_URL}/memory/remember", body={"outcome": "new_chunk", "chunk_id": "abc123"})
    daemon.install(monkeypatch)
    code = main(["remember", "from now on I use pnpm"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"] == f"{BASE_URL}/memory/remember"
    assert call["body"] == {"profile_id": "default", "text": "from now on I use pnpm"}
    assert "new_chunk" in captured.out
    assert "abc123" in captured.out


# ---------------------------------------------------------------- dream / export / diff / forget


def test_dream_once_posts_console_dream_once(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on("POST", f"{BASE_URL}/api/v1/dream/once", body={"launched": True, "state": "dreaming"})
    daemon.install(monkeypatch)
    code = main(["dream", "--once"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"] == f"{BASE_URL}/api/v1/dream/once"
    assert call["body"] == {"profile_id": "default"}
    assert "launched: True" in captured.out


def test_dream_status_gets_trigger_state(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/dream/status?profile_id=default",
        body={"profile_id": "default", "state": "idle", "pending_manual": 2, "queue_depth": 2},
    )
    daemon.install(monkeypatch)
    code = main(["dream", "status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "idle" in captured.out
    assert "pending_manual" in captured.out
    call = daemon.calls[0]
    assert call["params"] == {"profile_id": "default"}


def test_export_posts_memory_export_and_prints_summary(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/memory/export",
        body={
            "schema": "mnemoseed.memory.export/1",
            "profile_id": "default",
            "chunks": [{"chunk_id": "c1"}],
            "nodes": [],
            "paging": {"chunk_total": 1, "node_total": 0, "offset": 0, "limit": 50},
        },
    )
    daemon.install(monkeypatch)
    code = main(["export"])
    captured = capsys.readouterr()
    assert code == 0
    assert "chunks: 1" in captured.out
    assert "nodes: 0" in captured.out
    assert daemon.calls[0]["body"] == {"profile_id": "default", "offset": 0, "limit": 50}


def test_export_json_mode_dumps_full_payload(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/memory/export",
        body={
            "schema": "mnemoseed.memory.export/1",
            "chunks": [{"chunk_id": "c1"}],
            "nodes": [],
            "paging": {},
        },
    )
    daemon.install(monkeypatch)
    code = main(["export", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["chunks"][0]["chunk_id"] == "c1"


def test_diff_diffs_two_versions_of_a_node(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/memory/audit",
        body={
            "target": {"type": "node", "id": "n1"},
            "versions": [
                {"version": 1, "props": {"subject": "user", "predicate": "uses", "object": "npm"}},
                {"version": 2, "props": {"subject": "user", "predicate": "uses", "object": "pnpm"}},
            ],
        },
    )
    daemon.install(monkeypatch)
    code = main(["diff", "n1"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["body"] == {"profile_id": "default", "node_id": "n1"}
    assert "- user uses npm" in captured.out
    assert "+ user uses pnpm" in captured.out


def test_forget_posts_forget_this_for_a_node(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/memory/forget_this",
        body={"removed": {"chunks": [], "nodes": ["n1"]}},
    )
    daemon.install(monkeypatch)
    code = main(["forget", "n1"])
    captured = capsys.readouterr()
    assert code == 0
    assert daemon.calls[0]["body"] == {"profile_id": "default", "node_id": "n1"}
    assert "forgotten" in captured.out


def test_forget_entity_kind(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/memory/forget_this",
        body={"removed": {"chunks": ["c1"], "nodes": ["n1"]}},
    )
    daemon.install(monkeypatch)
    code = main(["forget", "user", "--kind", "entity"])
    assert code == 0
    assert daemon.calls[0]["body"] == {"profile_id": "default", "entity": "user"}


# ---------------------------------------------------------------- pin / weight / conflicts (FR-7.9 / FR-7.7)


def test_pin_posts_console_pin(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/pin",
        body={"node_id": "n1", "profile_id": "default", "pinned": True},
    )
    daemon.install(monkeypatch)
    code = main(["pin", "n1"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"] == f"{BASE_URL}/api/v1/pin"
    assert call["body"] == {"profile_id": "default", "node_id": "n1", "pinned": True}
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    assert "pinned" in captured.out


def test_pin_off_unpins(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on("POST", f"{BASE_URL}/api/v1/pin", body={"node_id": "n1", "pinned": False})
    daemon.install(monkeypatch)
    code = main(["pin", "n1", "--off"])
    assert code == 0
    assert daemon.calls[0]["body"] == {"profile_id": "default", "node_id": "n1", "pinned": False}


def test_weight_posts_console_weights(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/weights",
        body={"kind": "node", "target_id": "n1", "decay_weight": 0.3, "old_decay_weight": 0.9},
    )
    daemon.install(monkeypatch)
    code = main(["weight", "n1", "0.3"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"] == f"{BASE_URL}/api/v1/weights"
    assert call["body"] == {"profile_id": "default", "kind": "node", "target_id": "n1", "decay_weight": 0.3}
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    assert "0.9 -> 0.3" in captured.out


def test_weight_chunk_kind(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on("POST", f"{BASE_URL}/api/v1/weights", body={"kind": "chunk", "decay_weight": 0.5})
    daemon.install(monkeypatch)
    code = main(["weight", "c1", "0.5", "--kind", "chunk"])
    assert code == 0
    assert daemon.calls[0]["body"] == {
        "profile_id": "default",
        "kind": "chunk",
        "target_id": "c1",
        "decay_weight": 0.5,
    }


def test_conflicts_list_gets_inbox(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/conflicts?profile_id=default",
        body={
            "groups": [
                {
                    "group_id": "g1",
                    "sides": [
                        {
                            "node_id": "a-tabs",
                            "node_type": "preference",
                            "statement": "prefers tabs",
                            "decay_weight": 0.9,
                        },
                        {
                            "node_id": "b-spaces",
                            "node_type": "preference",
                            "statement": "prefers spaces",
                            "decay_weight": 0.9,
                        },
                    ],
                }
            ]
        },
    )
    daemon.install(monkeypatch)
    code = main(["conflicts", "list"])
    captured = capsys.readouterr()
    assert code == 0
    assert daemon.calls[0]["params"] == {"profile_id": "default"}
    assert "group g1" in captured.out
    assert "prefers tabs" in captured.out


def test_conflicts_resolve_posts_resolution(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/conflicts/g1/resolve",
        body={
            "group_id": "g1",
            "branch": "invalidate",
            "node_id": "b-spaces",
            "written": 1,
            "invalidated": 1,
        },
    )
    daemon.install(monkeypatch)
    code = main(["conflicts", "resolve", "g1", "--branch", "invalidate", "--node", "b-spaces"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"] == f"{BASE_URL}/api/v1/conflicts/g1/resolve"
    assert call["body"] == {"profile_id": "default", "branch": "invalidate", "node_id": "b-spaces"}
    assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    assert "resolved g1" in captured.out


def test_conflicts_resolve_coexist_sends_scope(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/conflicts/g1/resolve",
        body={"branch": "coexist", "scope": "s", "written": 2},
    )
    daemon.install(monkeypatch)
    code = main(["conflicts", "resolve", "g1", "--branch", "coexist", "--cues", "s"])
    assert code == 0
    assert daemon.calls[0]["body"] == {
        "profile_id": "default",
        "branch": "coexist",
        "scope": "s",
    }


def test_conflicts_list_accepts_flags_after_subcommand(tmp_path, monkeypatch, capsys) -> None:
    """--baseurl / --json work in BOTH positions for the conflicts group, and a
    flag before the subcommand is not clobbered by the subparser's default."""
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/conflicts?profile_id=default",
        body={"groups": [{"group_id": "g1", "sides": []}]},
    )
    daemon.install(monkeypatch)
    code = main(["conflicts", "list", "--baseurl", BASE_URL, "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["groups"][0]["group_id"] == "g1"
    assert daemon.calls[0]["url"].startswith(f"{BASE_URL}/api/v1/conflicts")

    code = main(["conflicts", "--baseurl", BASE_URL, "list", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["groups"][0]["group_id"] == "g1"
    assert daemon.calls[1]["url"].startswith(f"{BASE_URL}/api/v1/conflicts")


def test_conflicts_resolve_accepts_baseurl_after_subcommand(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "POST",
        f"{BASE_URL}/api/v1/conflicts/g1/resolve",
        body={"branch": "invalidate", "written": 1, "invalidated": 1},
    )
    daemon.install(monkeypatch)
    code = main(["conflicts", "resolve", "g1", "--branch", "invalidate", "--baseurl", BASE_URL])
    captured = capsys.readouterr()
    assert code == 0
    assert "resolved g1" in captured.out
    assert daemon.calls[0]["url"] == f"{BASE_URL}/api/v1/conflicts/g1/resolve"


def test_config_accepts_baseurl_before_subcommand(tmp_path, monkeypatch, capsys) -> None:
    """config --baseurl X <subcommand> works alongside config <subcommand> --baseurl X,
    and the value survives the subparser's parse either way."""
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/config",
        body={"config": {"scoring": {"w1": 0.9}}, "restart_required": {}},
    )
    daemon.install(monkeypatch)
    code = main(["config", "--baseurl", BASE_URL, "get", "scoring.w1"])
    captured = capsys.readouterr()
    assert code == 0
    assert "0.9" in captured.out
    assert daemon.calls[0]["url"].startswith(f"{BASE_URL}/api/v1/config")

    code = main(["config", "get", "scoring.w1", "--baseurl", BASE_URL, "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["config"]["scoring"]["w1"] == 0.9
    assert daemon.calls[1]["url"].startswith(f"{BASE_URL}/api/v1/config")


# ---------------------------------------------------------------- audit


def test_audit_queries_with_filters(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/audit?actor=cli&action=remember&offset=0&limit=5",
        body={
            "items": [
                {
                    "id": "e1",
                    "actor": "cli",
                    "action": "remember",
                    "detail": {"chunk_id": "c1"},
                    "at": 1700000000.0,
                }
            ],
            "paging": {"total": 1, "offset": 0, "limit": 5},
        },
    )
    daemon.install(monkeypatch)
    code = main(["audit", "--actor", "cli", "--action", "remember", "--limit", "5"])
    captured = capsys.readouterr()
    assert code == 0
    call = daemon.calls[0]
    assert call["url"].split("?", 1)[0] == f"{BASE_URL}/api/v1/audit"
    assert call["params"] == {"actor": "cli", "action": "remember", "offset": 0, "limit": 5}
    assert "remember" in captured.out
    assert "c1" in captured.out


def test_audit_json_mode_prints_items(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on(
        "GET",
        f"{BASE_URL}/api/v1/audit?offset=0&limit=50",
        body={"items": [{"id": "e1", "actor": "cli", "action": "x", "detail": {}, "at": 1.0}], "paging": {}},
    )
    daemon.install(monkeypatch)
    code = main(["audit", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["items"][0]["actor"] == "cli"


# ---------------------------------------------------------------- link / unlink


def _home_with_claude(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    claude = home / ".claude.json"
    claude.write_text('{"mcpServers": {"other": {"command": "x"}}}', encoding="utf-8")
    return home


def test_link_writes_profile_identity_with_backup_and_diff(tmp_path, monkeypatch, capsys) -> None:
    home = _home_with_claude(tmp_path)
    _env(tmp_path, monkeypatch, session=_session(profile_id="work"))
    monkeypatch.setenv("MNEMOSEED_USER_HOME", str(home))
    code = main(["link", "--yes"])
    captured = capsys.readouterr()
    assert code == 0
    text = (home / ".claude.json").read_text(encoding="utf-8")
    entry = json.loads(text)["mcpServers"]["mnemoseed"]
    assert entry["env"]["MNEMOSEED_PROFILE_ID"] == "work"
    assert entry["env"]["MNEMOSEED_TOKEN"] == TOKEN
    # backup + diff + confirm discipline (NFR-6.3): a timestamped backup exists
    data_dir = tmp_path / "backups"
    backups = list(data_dir.rglob("*"))
    assert backups, "no backup written"
    assert "-" in captured.out  # the diff preview is printed


def test_link_requires_session(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)  # no session
    code = main(["link"])
    captured = capsys.readouterr()
    assert code == 1
    assert "login" in captured.err


def test_unlink_removes_host_registrations(tmp_path, monkeypatch, capsys) -> None:
    home = _home_with_claude(tmp_path)
    claude = home / ".claude.json"
    claude.write_text(
        '{"mcpServers": {"mnemoseed": {"command": "mnemoseed", "args": ["mcp"]}, "other": {"command": "x"}}}',
        encoding="utf-8",
    )
    _env(tmp_path, monkeypatch)
    monkeypatch.setenv("MNEMOSEED_USER_HOME", str(home))
    code = main(["unlink"])
    captured = capsys.readouterr()
    assert code == 0
    data = json.loads(claude.read_text(encoding="utf-8"))
    assert "mnemoseed" not in data["mcpServers"]
    assert data["mcpServers"]["other"]["command"] == "x"  # unrelated entries untouched
    assert "claude-code" in captured.out


# ---------------------------------------------------------------- actor header + matrix


def test_actor_header_forwarded_on_get_and_post(tmp_path, monkeypatch) -> None:
    _env(tmp_path, monkeypatch, session=_session())
    daemon = FakeDaemon()
    daemon.on("GET", f"{BASE_URL}/api/v1/status", body=_STATUS_PAYLOAD)
    daemon.on("POST", f"{BASE_URL}/memory/remember", body={"outcome": "new_chunk"})
    daemon.install(monkeypatch)
    assert main(["status"]) == 0
    assert main(["remember", "fact"]) == 0
    for call in daemon.calls:
        assert call["headers"]["X-MnemoSeed-Actor"] == "cli"
    # the profile bearer token rides along from the stored session
    assert daemon.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_matrix_cli_verbs_all_registered(tmp_path, monkeypatch) -> None:
    """G-AC5: every CLI verb listed in docs/cli-parity-matrix.md is registered."""
    from mnemoseed.cli import build_parser

    matrix = Path(__file__).resolve().parent.parent / "docs" / "cli-parity-matrix.md"
    assert matrix.exists(), "docs/cli-parity-matrix.md is missing"
    text = matrix.read_text(encoding="utf-8")
    # capture the leading `mnemoseed <verb> [<subcommand>]` words inside each
    # backticked token; trailing args ("<query>", --flags, [key], ...) are ignored.
    tokens = re.findall(r"`mnemoseed ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", text)
    assert tokens, "no `mnemoseed <verb>` tokens found in the matrix"

    parser = build_parser()
    sub_action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))

    def nested_choices(subparser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
        for action in subparser._actions:
            if isinstance(action, argparse._SubParsersAction):
                return dict(action.choices)
        return {}

    for token in tokens:
        parts = token.split()
        verb = parts[0]
        assert verb in sub_action.choices, f"top-level verb {verb!r} from the matrix is not registered"
        if len(parts) == 2:
            children = nested_choices(sub_action.choices[verb])
            sub = parts[1]
            assert sub in children, f"subcommand {token!r} from the matrix is not registered under {verb!r}"
