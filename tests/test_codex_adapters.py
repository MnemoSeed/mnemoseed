"""PRD-06 T5 -- Codex CLI adapter (FR-6.3c).

Template validity, the fail-open hook contract, the per-turn injection cap, the
SessionEnd transcript settle, and the installer's user-level artifact path
(hooks.json + AGENTS.md fragment with one-time trust guidance).

Codex read paths (design/06 section 2.1/2.5): SessionStart warm-up (<=800
tokens), UserPromptSubmit per-turn injection (<=2,500-token ``additionalContext``
cap), the AGENTS.md standing guidance fragment, and the SessionEnd transcript
settle reading the rollout.jsonl ``transcript_path``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mnemoseed.installer import install, plan_registrations, uninstall
from mnemoseed.installer.codexfiles import (
    AGENTS_REL,
    HOOK_SCRIPT_RELS,
    HOOKS_JSON_REL,
    adapter_templates_dir,
    artifact_texts,
    plan_codex_files,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ADAPTER_ROOT = _REPO_ROOT / "adapters" / "codex"
_TEMPLATE_ROOT = _ADAPTER_ROOT / "templates"
_HOOK_DIR = _TEMPLATE_ROOT / "hooks"

_PROFILE = "prof-codex"
_SESSION = "sess-codex"
_PROMPT = "I prefer pnpm for dependency management."


def _load_client(source: Path, module_name: str):
    """Import the adapter's stdlib client under a unique module name so the
    three adapters' same-named client files never collide in sys.modules."""
    spec = importlib.util.spec_from_file_location(module_name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.dont_write_bytecode = True  # loading templates must not litter them with .pyc
_client = _load_client(_HOOK_DIR / "mnemoseed_hook_client.py", "codex_hook_client_under_test")
estimate_tokens = _client.estimate_tokens
read_transcript = _client.read_transcript

_RECALL_BODY = {
    "memory": {
        "entries": [
            {
                "kind": "chunk",
                "id": "chunk-1",
                "source": "user",
                "text": _PROMPT,
                "score": 0.9,
                "tokens": 4,
                "flags": [],
                "conflict_group": None,
                "recent_evidence": [],
            }
        ],
        "dropped_count": 0,
        "budget_tokens": 2500,
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
        "budget_tokens": 2500,
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
            "params": {"memento": "newAgent"},
        }
    )


def _user_prompt_submit_stdin() -> str:
    return json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": _SESSION,
            "cwd": str(_REPO_ROOT),
            "user_prompt": _PROMPT,
        }
    )


def _session_end_stdin(transcript_path: str) -> str:
    return json.dumps(
        {
            "hook_event_name": "SessionEnd",
            "session_id": _SESSION,
            "cwd": str(_REPO_ROOT),
            "transcript_path": transcript_path,
        }
    )


# ------------------------------------------------------------ template validity


def test_hooks_json_template_wires_design_events() -> None:
    hooks = json.loads((_TEMPLATE_ROOT / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    # Design/06 Codex row: SessionStart warm-up, UserPromptSubmit per-turn
    # injection, SessionEnd transcript settle.
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}
    expected_script = {
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt_submit",
        "SessionEnd": "session_end",
    }
    for event, script in expected_script.items():
        blocks = hooks[event]
        assert blocks, event
        hook = blocks[0]["hooks"][0]
        assert hook["type"] == "command", event
        assert isinstance(hook["timeout"], int), event
        assert ".codex/mnemoseed/py.sh" in hook["command"], event
        assert f"{script}.py" in hook["command"], event


def test_agents_fragment_contains_standing_guidance() -> None:
    fragment = (_TEMPLATE_ROOT / "agents.md").read_text(encoding="utf-8")
    assert fragment.startswith("## MnemoSeed memory")
    assert "memory.recall" in fragment
    assert "memory.remember" in fragment
    assert "2,500" in fragment


def test_hook_scripts_exist_for_each_event() -> None:
    for name in ("session_start", "user_prompt_submit", "session_end"):
        script = _HOOK_DIR / f"{name}.py"
        assert script.is_file(), name
        assert script.read_text(encoding="utf-8").startswith('"""')


def test_shim_resolves_an_interpreter() -> None:
    shim = (_HOOK_DIR / "py.sh").read_text(encoding="utf-8")
    for probe in ("python3", "python", "py"):
        assert probe in shim
    assert "exec" in shim


def test_shim_resolves_tilde_path_against_home(tmp_path) -> None:
    """The hooks.json commands pass ``~/...`` paths; a quoted tilde is NOT a
    tilde-prefix so the host shell leaves it literal and the shim resolves the
    leading ``~/`` against ``$HOME`` (``$USERPROFILE`` fallback) before exec.

    The shim is invoked through ``bash -c`` because that matched how the host
    delivers the argument (the quoted tilde reaches bash unexpanded) -- passing
    ``~/.codex/...`` as a plain argv element would let MSYS pre-expand it on the
    process boundary and the shim's own resolution would never be exercised.
    """
    home = tmp_path / "home"
    script = home / ".codex" / "mnemoseed" / "session_start.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# probe\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "python3"
    launcher.write_text(
        '#!/usr/bin/env bash\nif [ -f "$1" ]; then printf "found"; else printf "missing:%s" "$1"; fi\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    bash_path = shutil.which("bash")
    assert bash_path, "bash is required to exercise the shim"
    env = dict(os.environ)
    # Forward-slash HOME keeps the resolved path unambiguous for msys test -f.
    env["HOME"] = str(home).replace("\\", "/")
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    command = 'exec "{}" "~/.codex/mnemoseed/session_start.py"'.format(
        str(_HOOK_DIR / "py.sh").replace("\\", "/")
    )
    proc = subprocess.run(
        [bash_path, "-c", command],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "found", proc.stdout + proc.stderr


def test_readme_honest_about_trust_review_and_capture() -> None:
    text = (_ADAPTER_ROOT / "README.md").read_text(encoding="utf-8")
    folded = text.casefold()
    assert "/hooks" in text
    assert "trust" in folded
    assert "trust review is required" in folded
    assert "2,500" in text
    assert "AGENTS.md" in text


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


def test_user_prompt_submit_captures_and_injects_within_cap() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/memory/recall", payload=_RECALL_BODY)
        code, stdout = _run_hook(
            "user_prompt_submit", _user_prompt_submit_stdin(), _base_env(daemon.base_url)
        )
        assert code == 0
        inner = _injected(stdout)
        assert inner["hookEventName"] == "UserPromptSubmit"
        assert estimate_tokens(inner["additionalContext"]) <= 2500  # Codex cap (FR-6.3c)

        body = daemon.bodies("/ingest")[0]
        assert body["event"] == "user_prompt"
        assert body["host"] == "codex_cli"
        assert body["profile_id"] == _PROFILE
        assert body["session_id"] == _SESSION
        assert body["content"]["text"] == _PROMPT
    finally:
        daemon.close()


def test_user_prompt_submit_truncates_oversized_recall_to_cap() -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/memory/recall", payload=_BIG_RECALL_BODY)
        code, stdout = _run_hook(
            "user_prompt_submit", _user_prompt_submit_stdin(), _base_env(daemon.base_url)
        )
        assert code == 0
        inner = _injected(stdout)
        assert estimate_tokens(inner["additionalContext"]) <= 2500
    finally:
        daemon.close()


def test_session_end_reads_transcript_and_settles(tmp_path) -> None:
    transcript = tmp_path / "rollout.jsonl"
    payload = json.dumps({"type": "rollout", "user_prompt": "hello"})
    transcript.write_text((payload + "\n") * 4, encoding="utf-8")
    expected = transcript.read_text(encoding="utf-8")

    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/session/end", status=200, payload={})
        code, stdout = _run_hook(
            "session_end", _session_end_stdin(str(transcript)), _base_env(daemon.base_url)
        )
        assert code == 0
        assert _parse(stdout) == {}

        bodies = daemon.bodies("/ingest")
        assert len(bodies) == 1
        body = bodies[0]
        assert body["event"] == "assistant_message"
        assert body["host"] == "codex_cli"
        assert body["session_id"] == _SESSION
        assert body["content"]["text"] == expected

        end_bodies = daemon.bodies("/session/end")
        assert len(end_bodies) == 1
        assert end_bodies[0]["session_id"] == _SESSION
        assert end_bodies[0]["profile_id"] == _PROFILE
    finally:
        daemon.close()


def test_session_end_missing_transcript_still_settles(tmp_path) -> None:
    daemon = FakeDaemon()
    try:
        daemon.route("/session/end", status=200, payload={})
        cases = [
            _session_end_stdin(str(tmp_path / "nope.jsonl")),
            json.dumps({"hook_event_name": "SessionEnd", "session_id": _SESSION}),
        ]
        for stdin_text in cases:
            code, stdout = _run_hook("session_end", stdin_text, _base_env(daemon.base_url))
            assert code == 0
            assert _parse(stdout) == {}
        assert daemon.bodies("/ingest") == []  # nothing captured without a transcript
        assert len(daemon.bodies("/session/end")) == len(cases)
    finally:
        daemon.close()


def test_session_end_transcript_read_is_bounded(tmp_path) -> None:
    transcript = tmp_path / "huge.jsonl"
    transcript.write_text("y" * 500_000, encoding="utf-8")

    daemon = FakeDaemon()
    try:
        daemon.route("/ingest", status=202, payload={"status": "accepted"})
        daemon.route("/session/end", status=200, payload={})
        code, stdout = _run_hook(
            "session_end", _session_end_stdin(str(transcript)), _base_env(daemon.base_url)
        )
        assert code == 0
        assert _parse(stdout) == {}
        assert len(daemon.bodies("/ingest")[0]["content"]["text"]) == 200_000
    finally:
        daemon.close()


def test_read_transcript_fail_open_missing_file(tmp_path: Path) -> None:
    assert read_transcript(str(tmp_path)) == ""


def test_hooks_fail_open_daemon_down() -> None:
    env = _base_env(f"http://127.0.0.1:{_free_port()}")
    stdin_by_name = {
        "session_start": _session_start_stdin(),
        "user_prompt_submit": _user_prompt_submit_stdin(),
        "session_end": _session_end_stdin(str(Path(_REPO_ROOT) / "nope.jsonl")),
    }
    for name, stdin_text in stdin_by_name.items():
        code, stdout = _run_hook(name, stdin_text, env)
        assert code == 0, name
        assert _parse(stdout) == {}, name


def test_hooks_fail_open_malformed_stdin() -> None:
    env = _base_env(f"http://127.0.0.1:{_free_port()}")
    cases = {
        "malformed": '{ "user_prompt":',
        "truncated": '{"hook_event_name": "Session',
        "empty": "",
        "wrong_type": "[1, 2, 3]",
    }
    stdin_by_name = {
        "session_start": _session_start_stdin(),
        "user_prompt_submit": _user_prompt_submit_stdin(),
        "session_end": _session_end_stdin(str(Path(_REPO_ROOT) / "nope.jsonl")),
    }
    for label, stdin_text in cases.items():
        for name, _standard_stdin in stdin_by_name.items():
            code, stdout = _run_hook(name, stdin_text, env)
            assert code == 0, f"{name}:{label}"
            assert _parse(stdout) == {}, f"{name}:{label}"


# ------------------------------------------------------------ installer: user-level artifacts


def _approve_all(plan) -> bool:
    return True


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_plan_codex_files_is_read_only(tmp_path) -> None:
    home = tmp_path / "home"
    plans = plan_codex_files(home)
    assert [plan.host for plan in plans] == ["codex-hooks", "codex-agents"]
    assert all(plan.changed for plan in plans)
    assert len(plans[0].files) == len(HOOK_SCRIPT_RELS)
    assert plans[1].files == ()
    assert not home.exists()  # read-only planning creates nothing


def test_install_codex_writes_all_artifacts(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / ".codex").mkdir(parents=True)  # Codex detected as a host

    report = install(home, data, approve=_approve_all)
    assert report.written == 3  # MCP config.toml + hooks item + AGENTS.md item

    hooks = _load(home / HOOKS_JSON_REL)
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}
    assert (home / AGENTS_REL).read_text(encoding="utf-8").startswith("## MnemoSeed memory")

    install_texts = artifact_texts(adapter_templates_dir())
    for rel in HOOK_SCRIPT_RELS:
        assert (home / rel).read_text(encoding="utf-8") == install_texts[rel]


def test_install_codex_is_idempotent(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / ".codex").mkdir(parents=True)

    first = install(home, data, approve=_approve_all)
    assert first.written == 3

    plans = plan_registrations(home, data)
    assert len(plans) == 3
    assert all(not plan.changed for plan in plans)
    second = install(home, data, approve=_approve_all)
    assert second.written == 0


def test_install_codex_merges_existing_hooks_json(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    existing = {
        "description": "my own hooks",
        "hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "echo goodbye", "timeout": 10}]}]},
    }
    _write(home / HOOKS_JSON_REL, json.dumps(existing, indent=2))
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    install(home, data, approve=_approve_all)

    merged = _load(home / HOOKS_JSON_REL)
    assert merged["description"] == "my own hooks"
    assert merged["hooks"]["PreCompact"] == existing["hooks"]["PreCompact"]
    assert set(merged["hooks"]) == {"PreCompact", "SessionStart", "UserPromptSubmit", "SessionEnd"}


def test_install_codex_appends_agents_fragment_preserving_content(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = "# My own guide\n\nSome notes.\n"
    _write(home / AGENTS_REL, original)
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    install(home, data, approve=_approve_all)

    merged = (home / AGENTS_REL).read_text(encoding="utf-8")
    assert merged.startswith(original.rstrip())
    assert "## MnemoSeed memory" in merged


def test_uninstall_removes_exactly_codex_artifacts(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    unrelated = _write(home / "notes.txt", "keep me\n")
    (home / ".codex").mkdir(parents=True)

    install(home, data, approve=_approve_all)
    assert (home / HOOKS_JSON_REL).exists()
    assert (home / AGENTS_REL).exists()

    report = uninstall(home, data)
    removed = {roll.host: roll.outcome for roll in report.rolls}
    assert removed["codex-hooks"] == "removed"
    assert removed["codex-agents"] == "removed"
    for rel in (HOOKS_JSON_REL, *HOOK_SCRIPT_RELS, AGENTS_REL):
        assert not (home / rel).exists(), rel
    assert unrelated.read_text(encoding="utf-8") == "keep me\n"
    assert not (data / "installer" / "state.json").exists() or (
        _load(data / "installer" / "state.json").get("registrations") == {}
    )


def test_uninstall_restores_pre_existing_agents_md(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    original = b"# My own guide\n\nSome notes.\n"
    _write(home / AGENTS_REL, original.decode("utf-8"))
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    install(home, data, approve=_approve_all)
    assert (home / AGENTS_REL).read_bytes() != original

    report = uninstall(home, data)
    assert {roll.host: roll.outcome for roll in report.rolls}["codex-agents"] == "restored"
    assert (home / AGENTS_REL).read_bytes() == original


def test_uninstall_strips_agents_fragment_when_backup_lost(tmp_path) -> None:
    """Without an install-time backup, uninstall must strip only the appended
    fragment and keep the user's own AGENTS.md intact (FR-6.7 exact removal on
    the Codex guidance fragment, never the whole user file)."""
    from mnemoseed.installer.state import load_state

    home = tmp_path / "home"
    data = tmp_path / "data"
    original = "# My own guide\n\nSome notes.\n"
    _write(home / AGENTS_REL, original)
    (home / ".codex").mkdir(parents=True, exist_ok=True)

    install(home, data, approve=_approve_all)
    assert "## MnemoSeed memory" in (home / AGENTS_REL).read_text(encoding="utf-8")

    # Simulate a lost backup: the state still records it, the file is gone.
    backup = Path(load_state(data).registrations["codex-agents"].backup)
    assert backup.exists()
    backup.unlink()

    report = uninstall(home, data)
    assert {roll.host: roll.outcome for roll in report.rolls}["codex-agents"] == "removed"
    # Only the fragment is stripped; the user's own content survives verbatim.
    assert (home / AGENTS_REL).read_text(encoding="utf-8") == original


def test_cli_install_prints_codex_trust_guidance(tmp_path) -> None:
    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / ".codex").mkdir(parents=True)

    env = dict(os.environ)
    env["MNEMOSEED_USER_HOME"] = str(home)
    env["MNEMOSEED_HOME"] = str(data)
    env.pop("STORAGE_MODE", None)
    proc = subprocess.run(
        [sys.executable, "-m", "mnemoseed.cli", "install", "--yes"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "installed: 3 host registration(s)" in proc.stdout
    assert "codex-hooks: written" in proc.stdout
    assert "/hooks" in proc.stdout
    assert "trust" in proc.stdout.casefold()
    assert _load(home / HOOKS_JSON_REL)["hooks"]["SessionStart"]
    assert (home / AGENTS_REL).exists()


def test_state_round_trip_preserves_codex_companion_files(tmp_path) -> None:
    from mnemoseed.installer.state import State, load_state, save_state

    home = tmp_path / "home"
    data = tmp_path / "data"
    (home / ".codex").mkdir(parents=True)
    install(home, data, approve=_approve_all)

    state = load_state(data)
    record = state.registrations["codex-hooks"]
    assert len(record.files) == len(HOOK_SCRIPT_RELS)
    assert all(backup is None for _path, backup in record.files)  # freshly created

    save_state(data, State(registrations=state.registrations))
    reloaded = load_state(data)
    assert reloaded.registrations["codex-hooks"].files == record.files
