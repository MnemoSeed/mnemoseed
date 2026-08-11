"""PRD-06 T5 -- Gemini CLI extension (FR-6.3d).

Template validity (extension.json + GEMINI.md + commands), the fail-open hook
contract across SessionStart / BeforeAgent / AfterTool, and the per-turn
injection path.

Gemini read paths (design/06 section 2.1/2.5): SessionStart warm-up and
BeforeAgent per-turn injection (<=200 tokens) are the read channels; GEMINI.md
carries the standing guidance. Capture rides BeforeAgent (incoming request) and
AfterTool (tool results). The extension ships as a template directory because
Gemini CLI installs extensions as packages, not user-level drop-in files.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_ROOT = _REPO_ROOT / "adapters" / "gemini"
_TEMPLATE_ROOT = _ADAPTER_ROOT / "templates"
_HOOK_DIR = _TEMPLATE_ROOT / "hooks"

_PROFILE = "prof-gemini"
_SESSION = "sess-gemini"
_PROMPT = "What did we decide about pnpm?"


def _load_client(source: Path, module_name: str):
    """Import the adapter's stdlib client under a unique module name so the
    three adapters' same-named client files never collide in sys.modules."""
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.dont_write_bytecode = True  # loading templates must not litter them with .pyc
_client = _load_client(_HOOK_DIR / "mnemoseed_hook_client.py", "gemini_hook_client_under_test")
estimate_tokens = _client.estimate_tokens

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
        "budget_tokens": 200,
        "tokens_used": 4,
        "coverage": 1.0,
    }
}

_BIG_RECALL_BODY = {
    "memory": {
        "entries": [
            {
                "kind": "chunk",
                "id": "chunk-big",
                "source": "user",
                "text": "x" * 20_000,
                "score": 0.5,
                "tokens": 5000,
                "flags": [],
                "conflict_group": None,
                "recent_evidence": [],
            }
        ],
        "dropped_count": 0,
        "budget_tokens": 200,
        "tokens_used": 5000,
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
    hook_env = dict(env)
    # Template scripts executed as subprocesses would otherwise write
    # ``__pycache__`` back into the shipped template trees.
    hook_env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        timeout=30,
        env=hook_env,
    )
    return proc.returncode, proc.stdout.decode("utf-8").strip()


def _parse(stdout: str) -> dict:
    assert stdout, "expected a JSON response on stdout"
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict), "response must be a JSON object"
    return parsed


def _injected(stdout: str) -> dict:
    output = _parse(stdout)
    inner = output.get("hookSpecificOutput")
    assert isinstance(inner, dict) and inner.get("additionalContext"), "expected injected context"
    assert isinstance(inner["additionalContext"], str)
    return inner


def _session_start_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "SessionStart",
            "session_id": _SESSION,
            "cwd": str(_REPO_ROOT),
            "params": {"reason": "new", "directory": str(_REPO_ROOT)},
        }
    )


def _before_agent_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "BeforeAgent",
            "session_id": _SESSION,
            "params": {"prompt": _PROMPT, "directory": str(_REPO_ROOT)},
        }
    )


def _after_tool_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "AfterTool",
            "session_id": _SESSION,
            "params": {
                "tool_name": "Bash",
                "tool_input": '{"command": "pytest -q"}',
                "tool_output": "42 passed in 1.2s",
            },
        }
    )


# ------------------------------------------------------------ template validity


def test_extension_json_wires_design_events() -> None:
    manifest = json.loads((_TEMPLATE_ROOT / "extension.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "mnemoseed"
    # Design/06 Gemini row: the three extension events.
    assert set(manifest["hooks"]) == {"SessionStart", "BeforeAgent", "AfterTool"}
    assert manifest["context"] == ["GEMINI.md"]
    assert manifest["commands"] == "./commands"
    assert manifest["mcpServers"]["mnemoseed"] == {"command": "mnemoseed", "args": ["mcp"]}


def test_extension_hook_commands_reference_shim_and_script() -> None:
    manifest = json.loads((_TEMPLATE_ROOT / "extension.json").read_text(encoding="utf-8"))
    expected_script = {
        "SessionStart": "session_start",
        "BeforeAgent": "before_agent",
        "AfterTool": "after_tool",
    }
    for event, script in expected_script.items():
        blocks = manifest["hooks"][event]
        assert blocks, event
        hook = blocks[0]["hooks"][0]
        assert hook["type"] == "command", event
        assert isinstance(hook["timeout"], int), event
        assert "hooks/py.sh" in hook["command"], event
        assert f"{script}.py" in hook["command"], event


def test_gemini_md_fragment_contains_standing_guidance() -> None:
    fragment = (_TEMPLATE_ROOT / "GEMINI.md").read_text(encoding="utf-8")
    assert fragment.startswith("# MnemoSeed memory")
    assert "memory.recall" in fragment
    assert "memory.remember" in fragment


def test_hook_scripts_exist_for_each_event() -> None:
    for name in ("session_start", "before_agent", "after_tool"):
        script = _HOOK_DIR / f"{name}.py"
        assert script.is_file(), name
        assert script.read_text(encoding="utf-8").startswith('"""')


def test_shim_resolves_an_interpreter() -> None:
    shim = (_HOOK_DIR / "py.sh").read_text(encoding="utf-8")
    for probe in ("python3", "python", "py"):
        assert probe in shim
    assert "exec" in shim


def test_command_definitions_exist_and_reference_scripts() -> None:
    commands_dir = _TEMPLATE_ROOT / "commands"
    for name in ("recall", "memory", "dream", "forget"):
        definition = (commands_dir / f"{name}.md").read_text(encoding="utf-8")
        assert definition.startswith("---\n")
        assert "description:" in definition
        assert "hooks/py.sh" in definition
        assert f"scripts/{name}.py" in definition
        assert (commands_dir.parent / "scripts" / f"{name}.py").is_file()


def test_readme_honest_about_install_and_capture() -> None:
    text = (_ADAPTER_ROOT / "README.md").read_text(encoding="utf-8")
    folded = text.casefold()
    assert "gemini extensions install" in text
    assert "BeforeAgent" in text
    assert "AfterTool" in text
    assert "per-turn recall" in folded
    assert "fail open" in folded


# ------------------------------------------------------------ fail-open hook contract


def test_session_start_emits_warmup_within_budget() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/memory/recall", payload=_RECALL_BODY)
        code, stdout = _run_hook("session_start", _session_start_stdin(), _base_env(daemon.base_url))
        assert code == 0
        inner = _injected(stdout)
        assert inner["hookEventName"] == "SessionStart"
        assert inner["additionalContext"].startswith("<memory>\n- [chunk] I prefer pnpm")
        assert estimate_tokens(inner["additionalContext"]) <= 800
    finally:
        daemon.close()


def test_before_agent_captures_and_injects_within_budget() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/memory/recall", payload=_RECALL_BODY)
        code, stdout = _run_hook("before_agent", _before_agent_stdin(), _base_env(daemon.base_url))
        assert code == 0
        inner = _injected(stdout)
        assert inner["hookEventName"] == "BeforeAgent"
        assert estimate_tokens(inner["additionalContext"]) <= 200  # Gemini per-turn budget (FR-6.3d)

        body = daemon.bodies("/ingest")[0]
        assert body["event"] == "user_prompt"
        assert body["host"] == "gemini_cli"
        assert body["profile_id"] == _PROFILE
        assert body["session_id"] == _SESSION
        assert body["content"]["text"] == _PROMPT
    finally:
        daemon.close()


def test_before_agent_truncates_oversized_recall_to_budget() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/memory/recall", payload=_BIG_RECALL_BODY)
        code, stdout = _run_hook("before_agent", _before_agent_stdin(), _base_env(daemon.base_url))
        assert code == 0
        inner = _injected(stdout)
        assert estimate_tokens(inner["additionalContext"]) <= 200
    finally:
        daemon.close()


def test_after_tool_captures_tool_use() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        code, stdout = _run_hook("after_tool", _after_tool_stdin(), _base_env(daemon.base_url))
        assert code == 0
        assert _parse(stdout) == {}

        body = daemon.bodies("/ingest")[0]
        assert body["event"] == "tool_use"
        assert body["host"] == "gemini_cli"
        assert body["session_id"] == _SESSION
        content = body["content"]
        assert content["tool_name"] == "Bash"
        assert content["input"] == {"command": "pytest -q"}
        assert "42 passed" in content["output"]
    finally:
        daemon.close()


def test_hooks_fail_open_daemon_down() -> None:
    env = _base_env(f"http://127.0.0.1:{_free_port()}")
    stdin_by_name = {
        "session_start": _session_start_stdin(),
        "before_agent": _before_agent_stdin(),
        "after_tool": _after_tool_stdin(),
    }
    for name, stdin_text in stdin_by_name.items():
        code, stdout = _run_hook(name, stdin_text, env)
        assert code == 0, name
        assert _parse(stdout) == {}, name


def test_hooks_fail_open_malformed_stdin() -> None:
    env = _base_env(f"http://127.0.0.1:{_free_port()}")
    cases = {
        "malformed": '{ "params": {',
        "truncated": '{"hook_event_name": "BeforeAg',
        "empty": "",
        "wrong_type": "[1, 2, 3]",
    }
    stdin_by_name = {
        "session_start": _session_start_stdin(),
        "before_agent": _before_agent_stdin(),
        "after_tool": _after_tool_stdin(),
    }
    for label, stdin_text in cases.items():
        for name in stdin_by_name:
            code, stdout = _run_hook(name, stdin_text, env)
            assert code == 0, f"{name}:{label}"
            assert _parse(stdout) == {}, f"{name}:{label}"
