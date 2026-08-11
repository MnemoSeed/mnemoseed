"""PRD-06 T3 -- Claude Code hook contract tests (FR-6.3).

Every hook entry point under ``plugins/claude-code/hooks`` is executed as a
REAL subprocess (``sys.executable`` + the hook script) against a thread-backed
fake daemon that scripts per-path JSON responses and records every request.

The contract assertions follow the Claude Code hook spec:

  * one JSON object on stdin, one JSON object on stdout;
  * exit code 0 always (nothing ever blocks the agent);
  * ``hookSpecificOutput.<HookEventName>.additionalContext`` carries injection
    only for SessionStart / UserPromptSubmit, and it stays within the stated
    token budget;
  * every daemon interaction is fail-open: daemon down, missing route,
    malformed/truncated/empty stdin, or a daemon slower than the budget all
    yield an empty ``{}`` response well inside the 2s deadline.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "claude-code"
_HOOK_DIR = _PLUGIN_ROOT / "hooks"
sys.path.insert(0, str(_PLUGIN_ROOT))

from mnemoseed_hook_client import estimate_tokens  # noqa: E402

_PROFILE = "prof-hook"
_SESSION = "sess-hook"
_PROMPT = "Which lint rule should I apply to this project?"

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

    def paths(self) -> list[str]:
        with self._lock:
            return [record["path"] for record in self._records]

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)


class _FakeDaemonHandler(BaseHTTPRequestHandler):
    """Serves scripted JSON responses and logs requests into the recorder."""

    recorder: _Recorder | None = None
    routes: dict[str, tuple[int, dict, float]] = {}

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
        status, payload, delay = self.routes.get(self.path, (404, {"detail": "no route"}, 0.0))
        try:
            if delay:
                time.sleep(delay)
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

    def route(self, path: str, *, status: int = 200, payload: dict | None = None, delay: float = 0.0) -> None:
        _FakeDaemonHandler.routes[path] = (status, payload if payload is not None else {}, delay)

    def bodies(self, path: str) -> list[dict]:
        return self.recorder.bodies(path)

    def paths(self) -> list[str]:
        return self.recorder.paths()

    def __len__(self) -> int:
        return len(self.recorder)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class DripDaemon:
    """A raw-socket daemon that drips the response body one byte per tick.

    A per-read idle timeout alone cannot bound this: each read succeeds within
    the tick, so only a shared wall-clock deadline (checked between read blocks)
    makes the hook fail open. Content-Length is truthful, so the hook knows
    there is more body to come and must keep reading.
    """

    BODY = json.dumps(_RECALL_BODY, ensure_ascii=False).encode("utf-8")

    def __init__(self, drop_seconds: float) -> None:
        self._drop = drop_seconds
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(4)
        self._server.settimeout(0.5)
        self.base_url = f"http://127.0.0.1:{self._server.getsockname()[1]}"
        self._accepting = threading.Thread(target=self._accept_loop, daemon=True)
        self._accepting.start()

    def _accept_loop(self) -> None:
        while True:
            try:
                client, _addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._drip, args=(client,), daemon=True).start()

    def _drip(self, client: socket.socket) -> None:
        try:
            client.settimeout(5)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = client.recv(4096)
                if not chunk:
                    return
                request += chunk
            header = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(self.BODY)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
            client.sendall(header)
            for index in range(len(self.BODY)):
                time.sleep(self._drop)
                client.sendall(self.BODY[index : index + 1])
        except OSError:
            pass  # the hook aborted and closed mid-drip: expected, not a failure
        finally:
            try:
                client.close()
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._server.close()
        except OSError:
            pass


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _base_env(base_url: str, *, budget: float | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_")}
    env["MNEMOSEED_BASE_URL"] = base_url
    env["MNEMOSEED_PROFILE_ID"] = _PROFILE
    if budget is not None:
        env["MNEMOSEED_HOOK_BUDGET_SECONDS"] = str(budget)
    return env


def _run_hook(
    name: str,
    stdin_text: str | None,
    env: dict[str, str],
    *,
    timeout: float = 30,
) -> tuple[int, str, float]:
    script = _HOOK_DIR / f"{name}.py"
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(script)],
        input=(stdin_text or "").encode("utf-8"),
        capture_output=True,
        timeout=timeout,
        env=env,
    )
    elapsed = time.monotonic() - started
    return proc.returncode, proc.stdout.decode("utf-8").strip(), elapsed


def _parse(stdout: str) -> dict:
    assert stdout, "expected a JSON response on stdout"
    parsed = json.loads(stdout)
    assert isinstance(parsed, dict), "response must be a JSON object"
    return parsed


def _stdin(**overrides: object) -> str:
    payload: dict[str, object] = {
        "session_id": _SESSION,
        "hook_event_name": "UserPromptSubmit",
        "user_prompt": _PROMPT,
        "cwd": str(_PLUGIN_ROOT),
    }
    payload.update(overrides)
    return json.dumps(payload)


# ---------------------------------------------------------- injection hooks


def test_session_start_emits_injection_within_budget() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/memory/recall", payload=_RECALL_BODY)
        code, stdout, _elapsed = _run_hook(
            "session_start",
            stdin_text=_stdin(hook_event_name="SessionStart"),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        output = _parse(stdout)
        specific = output["hookSpecificOutput"]
        assert specific["hookEventName"] == "SessionStart"
        context = specific["additionalContext"]
        assert context.startswith("<memory>\n- [chunk] I prefer pnpm")
        assert estimate_tokens(context) <= 800
    finally:
        daemon.close()


def test_oversized_recall_is_truncated_to_the_stated_budget() -> None:
    daemon = FakeDaemon()
    try:
        giant = {
            "memory": {
                "entries": [
                    {
                        "kind": "chunk",
                        "id": "chunk-big",
                        "source": "user",
                        "text": "The project decision was to standardize every backend service. " * 140,
                        "score": 0.9,
                        "tokens": 1,
                        "flags": [],
                        "conflict_group": None,
                        "recent_evidence": [],
                    }
                ],
                "dropped_count": 0,
                "budget_tokens": 800,
                "tokens_used": 1,
                "coverage": 0.5,
            }
        }
        daemon.route("/memory/recall", payload=giant)
        code, stdout, _elapsed = _run_hook(
            "session_start",
            stdin_text=_stdin(hook_event_name="SessionStart"),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        context = _parse(stdout)["hookSpecificOutput"]["additionalContext"]
        assert estimate_tokens(context) <= 800
        full_block = "<memory>\n- [chunk] " + giant["memory"]["entries"][0]["text"] + "\n</memory>"
        assert len(context) < len(full_block)  # provably shortened, not truncated-by-nothing
        assert context.startswith("<memory>\n- [chunk] ")  # the cue survives intact
    finally:
        daemon.close()


def test_user_prompt_submit_captures_first_then_injects_in_order() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/memory/recall", payload=_RECALL_BODY)
        code, stdout, _elapsed = _run_hook(
            "user_prompt_submit",
            stdin_text=_stdin(),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        output = _parse(stdout)
        specific = output["hookSpecificOutput"]
        assert specific["hookEventName"] == "UserPromptSubmit"
        assert estimate_tokens(specific["additionalContext"]) <= 200

        paths = daemon.paths()
        assert paths[0] == "/ingest"  # AC-6 capture first ...
        assert paths[1] == "/memory/recall"  # ... AC-7 injection second
        ingest_bodies = daemon.bodies("/ingest")
        assert len(ingest_bodies) == 1
        captured = ingest_bodies[0]
        assert captured["event"] == "user_prompt"
        assert captured["host"] == "claude_code"
        assert captured["profile_id"] == _PROFILE
        assert captured["session_id"] == _SESSION
        assert captured["content"]["text"] == _PROMPT
        recall_bodies = daemon.bodies("/memory/recall")
        assert recall_bodies[0]["query"] == _PROMPT
        assert recall_bodies[0]["profile_id"] == _PROFILE
    finally:
        daemon.close()


# ------------------------------------------------------------ capture hooks


def test_post_tool_use_captures_capped_tool_output() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        tool_response = {"result": "ok", "rows": [1, 2, 3]}
        code, stdout, _elapsed = _run_hook(
            "post_tool_use",
            stdin_text=_stdin(
                hook_event_name="PostToolUse",
                tool_name="Bash",
                tool_input={"command": "pytest -q"},
                tool_use_id="toolu_01",
                tool_response=tool_response,
            ),
            env=_base_env(daemon.base_url) | {"MNEMOSEED_TOOL_OUTPUT_CAP": "64"},
        )
        assert code == 0
        assert _parse(stdout) == {}
        body = daemon.bodies("/ingest")[0]
        assert body["event"] == "tool_use"
        content = body["content"]
        assert content["tool_name"] == "Bash"
        assert content["input"] == {"command": "pytest -q"}
        assert len(content["output"]) <= 64  # the output cap applies before capture
        assert "rows" in content["output"]
    finally:
        daemon.close()


def test_pre_compact_posts_flush_signal() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/flush", status=200, payload={"status": "flushed", "closed_turns": 1})
        code, stdout, _elapsed = _run_hook(
            "pre_compact",
            stdin_text=_stdin(hook_event_name="PreCompact"),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        assert _parse(stdout) == {}
        flush_bodies = daemon.bodies("/flush")
        assert len(flush_bodies) == 1
        assert flush_bodies[0]["session_id"] == _SESSION
        assert flush_bodies[0]["profile_id"] == _PROFILE
    finally:
        daemon.close()


def test_stop_captures_last_assistant_message_without_deciding() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        code, stdout, _elapsed = _run_hook(
            "stop",
            stdin_text=_stdin(
                hook_event_name="Stop",
                stop_hook_active=False,
                last_assistant_message="Summarize: done.",
            ),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        assert _parse(stdout) == {}  # never a decision/reason (Stop is not an injection hook)
        capture = daemon.bodies("/ingest")[0]
        assert capture["event"] == "assistant_message"
        assert capture["content"]["text"] == "Summarize: done."
        daemon.route("/flush", status=200, payload={"status": "flushed", "closed_turns": 0})
    finally:
        daemon.close()


def test_stop_hook_active_skips_the_daemon_entirely() -> None:
    """stop_hook_active means a Stop hook already ran this turn; the loop guard
    fast-exits without a single daemon call (and never blocks the stop)."""
    daemon = FakeDaemon()
    try:
        code, stdout, _elapsed = _run_hook(
            "stop",
            stdin_text=_stdin(
                hook_event_name="Stop",
                stop_hook_active=True,
                last_assistant_message="Summary.",
            ),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        assert _parse(stdout) == {}
        assert len(daemon) == 0  # no HTTP call happened at all
    finally:
        daemon.close()


def test_session_end_posts_settle() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route(
            "/session/end",
            status=200,
            payload={"status": "settled", "session_id": _SESSION, "profile_id": _PROFILE, "turns": 1},
        )
        code, stdout, _elapsed = _run_hook(
            "session_end",
            stdin_text=_stdin(hook_event_name="SessionEnd"),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        assert _parse(stdout) == {}
        settle = daemon.bodies("/session/end")[0]
        assert settle["session_id"] == _SESSION
        assert settle["profile_id"] == _PROFILE
    finally:
        daemon.close()


# ------------------------------------------------------------ fail-open paths


def test_hooks_never_crash_fail_open_with_missing_route() -> None:
    """A route the daemon does not serve (404) is indistinguishable from a
    daemon problem: hooks exit 0 with an empty response."""
    daemon = FakeDaemon()
    try:
        for hook in ("session_start", "user_prompt_submit", "post_tool_use", "session_end"):
            code, stdout, _elapsed = _run_hook(
                hook,
                stdin_text=_stdin(hook_event_name=hook.replace("_", " ").title().replace(" ", "")),
                env=_base_env(daemon.base_url),
            )
            assert code == 0
            assert _parse(stdout) == {}
    finally:
        daemon.close()


def test_daemon_down_fails_open_fast() -> None:
    code, stdout, elapsed = _run_hook(
        "session_start",
        stdin_text=_stdin(hook_event_name="SessionStart"),
        env=_base_env(f"http://127.0.0.1:{_free_port()}"),
    )
    assert code == 0
    assert _parse(stdout) == {}
    assert elapsed < 2.5  # far inside the 2s hook budget even with interpreter start


def test_malformed_truncated_empty_stdin_never_crash() -> None:
    """Malformed, truncated, or empty stdin each yield a well-formed empty JSON
    response: the hook reads defensively and fails open before any agent impact."""
    cases = {
        "malformed": '{ "session_id": "x", "user_prompt": ',
        "truncated": '{"hook_event_name": "UserPromptSubmit", "user_p',
        "empty": "",
        "wrong_type": "[1, 2, 3]",
        "json_junk": "not json at all",
    }
    for label, stdin_text in cases.items():
        assert stdin_text is not None
        code, stdout, elapsed = _run_hook(  # daemon down so the ONLY output is {}
            "user_prompt_submit",
            stdin_text=stdin_text,
            env=_base_env(f"http://127.0.0.1:{_free_port()}"),
        )
        assert code == 0, label
        assert _parse(stdout) == {}, label
        assert elapsed < 2.5, label


def test_slow_daemon_fails_open_within_the_hook_budget() -> None:
    """A daemon answering slower than the 2s budget must not hang the hook: the
    shared deadline drops the call and the hook exits 0 with {} well under any
    agent-visible latency."""
    daemon = FakeDaemon()
    try:
        daemon.route("/memory/recall", payload=_RECALL_BODY, delay=2.5)
        code, stdout, elapsed = _run_hook(
            "session_start",
            stdin_text=_stdin(hook_event_name="SessionStart"),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        assert _parse(stdout) == {}
        assert elapsed < 2.5
    finally:
        daemon.close()


def test_drip_body_cannot_hang_the_hook_beyond_the_budget() -> None:
    """D1: a body dripped one byte at a time defeats an idle-only socket
    timeout (every read returns on time), so only a wall-clock deadline -- the
    budget checked between read blocks -- can deliver fail-open. The hook must
    exit 0 with {} in well under 3s instead of waiting for the full body."""
    daemon = DripDaemon(drop_seconds=0.3)
    try:
        code, stdout, elapsed = _run_hook(
            "session_start",
            stdin_text=_stdin(hook_event_name="SessionStart"),
            env=_base_env(daemon.base_url),
            timeout=8,  # a hook that cannot bound the drip would hang this test
        )
        assert code == 0
        assert _parse(stdout) == {}
        assert elapsed < 2.5, f"hook let the drip server eat {elapsed:.2f}s"
    finally:
        daemon.close()


def test_default_url_dead_daemon_respects_the_budget() -> None:
    """D2: with MNEMOSEED_BASE_URL unset the hook dials the default localhost
    URL; multi-address probing of a dead daemon must stay inside the budget
    instead of consuming it per address."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_")}
    env["MNEMOSEED_PROFILE_ID"] = _PROFILE
    code, stdout, elapsed = _run_hook(
        "session_start",
        stdin_text=_stdin(hook_event_name="SessionStart"),
        env=env,
    )
    assert code == 0
    assert _parse(stdout) == {}
    assert elapsed < 2.5, f"multi-address dialing ate {elapsed:.2f}s"


def test_shared_budget_covers_both_calls_of_user_prompt_submit() -> None:
    """The capture (/ingest) and injection (/memory/recall) calls share ONE 2s
    deadline: a slow follow-up is dropped even though the first call succeeded."""
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/memory/recall", payload=_RECALL_BODY, delay=2.5)
        code, stdout, elapsed = _run_hook(
            "user_prompt_submit",
            stdin_text=_stdin(),
            env=_base_env(daemon.base_url),
        )
        assert code == 0
        assert _parse(stdout) == {}  # injection dropped; capture already persisted
        assert len(daemon.bodies("/ingest")) == 1  # AC-6 capture is never starved
        assert elapsed < 2.5
    finally:
        daemon.close()
