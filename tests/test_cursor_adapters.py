"""PRD-06 T4 -- Cursor adapter (FR-6.3b).

Template validity, the fail-open hook contract, and the installer's project
artifact path.

Cursor has no per-turn prompt injection (``beforeSubmitPrompt`` can only block),
so the adapter's read paths are the session-start warm-up (``additional_context``),
the ``.cursor/rules/mnemoseed.mdc`` standing guidance, and the model's own
``memory.recall`` MCP calls (design/06 section 2.5).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mnemoseed.installer import install, plan_cursor_project, uninstall
from mnemoseed.installer.cursorfiles import (
    HOOK_SCRIPT_RELS,
    HOOKS_JSON_REL,
    RULES_REL,
    adapter_templates_dir,
    artifact_texts,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_ROOT = _REPO_ROOT / "adapters" / "cursor"
_TEMPLATE_ROOT = _ADAPTER_ROOT / "templates"
_HOOK_DIR = _TEMPLATE_ROOT / "hooks"
sys.path.insert(0, str(_HOOK_DIR))

from mnemoseed_hook_client import estimate_tokens  # noqa: E402

_PROFILE = "prof-cursor"
_SESSION = "sess-cursor"
_RESPONSE = "Let me summarize the current state of the memory pipeline.\n\nDone."

_RECALL_BODY = {
    "memory": {
        "entries": [
            {
                "kind": "chunk",
                "id": "chunk-1",
                "source": "user",
                "text": "I prefer pnpm for dependency management.",
                "score": 0.9,
                "tokens": 4,
                "flags": [],
                "conflict_group": None,
                "recent_evidence": [],
            }
        ],
        "dropped_count": 0,
        "budget_tokens": 800,
        "tokens_used": 4,
        "coverage": 1.0,
    }
}


class _Recorder:
    """Thread-safe log of every request the fake daemon handled."""

    def __init__(self) -> None:
        self._records: list[dict] = []
        self._lock = threading.Lock()

    def append(self, record: dict) -> None:
        with self._lock:
            self._records.append(record)

    def bodies(self, path: str) -> list[dict]:
        with self._lock:
            return [record["body"] for record in self._records if record["path"] == path]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class _FakeDaemonHandler(BaseHTTPRequestHandler):
    recorder: _Recorder | None = None
    routes: dict[str, tuple[int, dict]] = {}

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body: dict | None = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
                body = parsed if isinstance(parsed, dict) else None
            except (ValueError, UnicodeDecodeError):
                body = None
        if self.recorder is not None:
            self.recorder.append({"path": self.path, "body": body})
        status, payload = self.routes.get(self.path, (404, {"detail": "no route"}))
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:  # a client timing out mid-response is not a failure here
            pass


class FakeDaemon:
    """A scriptable localhost HTTP daemon for hook subprocesses to hit."""

    def __init__(self) -> None:
        self.recorder = _Recorder()
        handler_type = _FakeDaemonHandler
        handler_type.recorder = self.recorder
        handler_type.routes = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def route(self, path: str, *, status: int = 200, payload: dict | None = None) -> None:
        _FakeDaemonHandler.routes[path] = (status, payload if payload is not None else {})

    def bodies(self, path: str) -> list[dict]:
        return self.recorder.bodies(path)

    def __len__(self) -> int:
        return len(self.recorder)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _base_env(base_url: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_")}
    env["MNEMOSEED_BASE_URL"] = base_url
    env["MNEMOSEED_PROFILE_ID"] = _PROFILE
    return env


def _run_hook(name: str, stdin_text: str, env: dict[str, str]) -> tuple[int, str]:
    script = _HOOK_DIR / f"{name}.py"
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=env,
    )
    return proc.returncode, proc.stdout.decode("utf-8").strip()


def _parse(stdout: str) -> dict:
    assert stdout, "expected a JSON response on stdout"
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict), "response must be a JSON object"
    return parsed


def _session_start_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "sessionStart",
            "session_id": _SESSION,
            "cwd": str(_REPO_ROOT),
            "workspace_path": str(_REPO_ROOT),
            "params": {"reason": "newAgent", "directory": str(_REPO_ROOT), "sessionID": _SESSION},
        }
    )


def _post_tool_use_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "postToolUse",
            "request_id": "req-1",
            "session_id": _SESSION,
            "cwd": str(_REPO_ROOT),
            "params": {
                "toolName": "bash",
                "toolInput": '{"command": "pytest -q"}',
                "toolResponse": "4 passed in 1.2s",
            },
        }
    )


def _after_agent_response_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "afterAgentResponse",
            "request_id": "req-2",
            "session_id": _SESSION,
            "cwd": str(_REPO_ROOT),
            "params": {"response": _RESPONSE, "responseType": "chat_completion", "didAskAgent": False},
        }
    )


# ------------------------------------------------------------ template validity


def test_hooks_json_template_wires_design_events() -> None:
    hooks = json.loads((_TEMPLATE_ROOT / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # Design/06 Cursor row: native events afterAgentResponse / postToolUse /
    # sessionStart (per-turn injection is not available on Cursor).
    assert set(hooks) == {"sessionStart", "postToolUse", "afterAgentResponse"}
    expected_script = {
        "sessionStart": "session_start",
        "postToolUse": "post_tool_use",
        "afterAgentResponse": "after_agent_response",
    }
    for event, script in expected_script.items():
        blocks = hooks[event]
        assert blocks, event
        hook = blocks[0]["hooks"][0]
        assert hook["type"] == "command", event
        assert isinstance(hook["timeout"], int), event
        assert ".cursor/hooks/mnemoseed/py.sh" in hook["command"], event
        assert f"{script}.py" in hook["command"], event


def test_rules_mdc_frontmatter_valid_with_guidance() -> None:
    text = (_TEMPLATE_ROOT / "rules" / "mnemoseed.mdc").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    boundary = text.index("---", 3)
    frontmatter = text[3:boundary]
    assert "description:" in frontmatter
    assert "alwaysApply: true" in frontmatter
    body = text[boundary + 3 :]
    assert "memory.recall" in body
    assert "memory.remember" in body


def test_hook_scripts_exist_for_each_event() -> None:
    for name in ("session_start", "post_tool_use", "after_agent_response"):
        script = _HOOK_DIR / f"{name}.py"
        assert script.is_file(), name
        assert script.read_text(encoding="utf-8").startswith('"""')


def test_shim_resolves_an_interpreter() -> None:
    shim = (_HOOK_DIR / "py.sh").read_text(encoding="utf-8")
    for probe in ("python3", "python", "py"):
        assert probe in shim
    assert "exec" in shim


def test_readme_honest_about_per_turn_injection() -> None:
    text = (_ADAPTER_ROOT / "README.md").read_text(encoding="utf-8").casefold()
    assert "per-turn injection is unavailable" in text
    assert "session-start warm-up" in text
    assert "memory.recall" in text
    assert ".cursor/rules/mnemoseed.mdc" in text


# ------------------------------------------------------------ fail-open hook contract


def test_session_start_emits_warmup_within_budget() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/memory/recall", payload=_RECALL_BODY)
        code, stdout = _run_hook("session_start", _session_start_stdin(), _base_env(daemon.base_url))
        assert code == 0
        output = _parse(stdout)
        context = output["additional_context"]
        assert context.startswith("<memory>\n- [chunk] I prefer pnpm")
        assert estimate_tokens(context) <= 800
    finally:
        daemon.close()


def test_post_tool_use_captures_tool_from_params() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        code, stdout = _run_hook("post_tool_use", _post_tool_use_stdin(), _base_env(daemon.base_url))
        assert code == 0
        assert _parse(stdout) == {}
        body = daemon.bodies("/ingest")[0]
        assert body["event"] == "tool_use"
        assert body["host"] == "cursor"
        assert body["profile_id"] == _PROFILE
        assert body["session_id"] == _SESSION
        content = body["content"]
        assert content["tool_name"] == "bash"
        assert content["input"] == {"command": "pytest -q"}
        assert "4 passed" in content["output"]
    finally:
        daemon.close()


def test_after_agent_response_captures_full_text() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        code, stdout = _run_hook(
            "after_agent_response", _after_agent_response_stdin(), _base_env(daemon.base_url)
        )
        assert code == 0
        assert _parse(stdout) == {}
        body = daemon.bodies("/ingest")[0]
        assert body["event"] == "assistant_message"
        assert body["host"] == "cursor"
        assert body["content"]["text"] == _RESPONSE
    finally:
        daemon.close()


def test_hooks_fail_open_daemon_down() -> None:
    env = _base_env(f"http://127.0.0.1:{_free_port()}")
    for name in ("session_start", "post_tool_use", "after_agent_response"):
        code, stdout = _run_hook(name, _session_start_stdin(), env)
        assert code == 0, name
        assert _parse(stdout) == {}, name


def test_hooks_fail_open_malformed_stdin() -> None:
    env = _base_env(f"http://127.0.0.1:{_free_port()}")
    cases = {
        "malformed": '{ "params": {',
        "truncated": '{"hook_event_name": "afterAgentResp',
        "empty": "",
        "wrong_type": "[1, 2, 3]",
    }
    for label, stdin_text in cases.items():
        for name in ("session_start", "post_tool_use", "after_agent_response"):
            code, stdout = _run_hook(name, stdin_text, env)
            assert code == 0, f"{name}:{label}"
            assert _parse(stdout) == {}, f"{name}:{label}"


# ------------------------------------------------------------ installer: project artifacts


def _approve_all(plan) -> bool:
    return True


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_plan_cursor_project_is_read_only(tmp_path) -> None:
    project = tmp_path / "planning-probe"
    plans = plan_cursor_project(project)
    assert [plan.host for plan in plans] == ["cursor-hooks", "cursor-rules"]
    assert all(plan.changed for plan in plans)
    assert len(plans[0].files) == len(HOOK_SCRIPT_RELS)
    assert plans[1].files == ()
    assert not project.exists()  # read-only planning creates nothing


def test_install_cursor_project_writes_all_artifacts(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"
    (home / ".cursor").mkdir(parents=True)  # Cursor detected as a host

    report = install(home, data, cursor_project=project, approve=_approve_all)
    assert report.written == 3  # cursor MCP registration + hooks item + rules item

    # The T2 cursor path still writes the mcp.json registration.
    assert (home / ".cursor" / "mcp.json").exists()

    hooks = _load(project / HOOKS_JSON_REL)
    assert set(hooks["hooks"]) == {"sessionStart", "postToolUse", "afterAgentResponse"}
    rules = (project / RULES_REL).read_text(encoding="utf-8")
    assert rules.startswith("---\n") and "alwaysApply: true" in rules

    install_texts = artifact_texts(adapter_templates_dir())
    for rel in HOOK_SCRIPT_RELS:
        assert (project / rel).read_text(encoding="utf-8") == install_texts[rel]


def test_install_cursor_project_is_idempotent(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"

    first = install(home, data, cursor_project=project, approve=_approve_all)
    assert first.written == 2  # Cursor not detected as a host; only the two artifact items

    plans = plan_cursor_project(project)
    assert all(not plan.changed for plan in plans)
    second = install(home, data, cursor_project=project, approve=_approve_all)
    assert second.written == 0


def test_install_cursor_project_merges_existing_hooks_json(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"
    existing = {
        "description": "my own hooks",
        "hooks": {"sessionEnd": [{"hooks": [{"type": "command", "command": "echo goodbye", "timeout": 10}]}]},
    }
    _write(project / ".cursor" / "hooks.json", json.dumps(existing, indent=2))

    install(home, data, cursor_project=project, approve=_approve_all)

    merged = _load(project / HOOKS_JSON_REL)
    assert merged["description"] == "my own hooks"
    assert merged["hooks"]["sessionEnd"] == existing["hooks"]["sessionEnd"]
    assert set(merged["hooks"]) == {"sessionEnd", "sessionStart", "postToolUse", "afterAgentResponse"}


def test_uninstall_removes_exactly_cursor_artifacts(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"
    unrelated = _write(project / "notes.txt", "keep me\n")
    (home / ".cursor").mkdir(parents=True)

    install(home, data, cursor_project=project, approve=_approve_all)
    assert (project / HOOKS_JSON_REL).exists()
    assert (project / RULES_REL).exists()

    report = uninstall(home, data)
    removed = {roll.host: roll.outcome for roll in report.rolls}
    assert removed["cursor-hooks"] == "removed"
    assert removed["cursor-rules"] == "removed"
    for rel in (HOOKS_JSON_REL, *HOOK_SCRIPT_RELS, RULES_REL):
        assert not (project / rel).exists()
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"
    assert not (data / "installer" / "state.json").exists() or (
        _load(data / "installer" / "state.json").get("registrations") == {}
    )


def test_uninstall_restores_pre_existing_hooks_json(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"
    original = b'{"hooks": {"sessionEnd": []}}\n'
    config = project / ".cursor" / "hooks.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(original)

    install(home, data, cursor_project=project, approve=_approve_all)
    assert (project / HOOKS_JSON_REL).read_bytes() != original

    report = uninstall(home, data)
    assert {roll.host: roll.outcome for roll in report.rolls}["cursor-hooks"] == "restored"
    assert (project / HOOKS_JSON_REL).read_bytes() == original


def test_cli_install_cursor_project_writes_artifacts(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"

    env = dict(os.environ)
    env["MNEMOSEED_USER_HOME"] = str(home)
    env["MNEMOSEED_HOME"] = str(data)
    env.pop("STORAGE_MODE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "mnemoseed.cli", "install", "--yes", "--cursor-project", str(project)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cursor-hooks: written" in proc.stdout
    assert "cursor-rules: written" in proc.stdout
    assert _load(project / HOOKS_JSON_REL)["hooks"]["sessionStart"]
    assert (project / RULES_REL).exists()


def test_state_round_trip_preserves_companion_files(tmp_path) -> None:
    from mnemoseed.installer.state import State, load_state, save_state

    home = tmp_path / "home"
    data = tmp_path / "data"
    project = tmp_path / "project"
    install(home, data, cursor_project=project, approve=_approve_all)

    state = load_state(data)
    record = state.registrations["cursor-hooks"]
    assert len(record.files) == len(HOOK_SCRIPT_RELS)
    assert all(backup is None for _path, backup in record.files)  # freshly created

    save_state(data, State(registrations=state.registrations))
    reloaded = load_state(data)
    assert reloaded.registrations["cursor-hooks"].files == record.files
