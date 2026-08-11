"""PRD-06 T3 -- plugin <> daemon integration test.

Boots ONE real daemon per scenario (embedded stores + deterministic synthetic
embedder, via the e2e dual-client harness) and drives the ACTUAL plugin hook
scripts as real subprocesses against it over its real HTTP surface:

  * SessionStart warm-up: a seeded memory is recalled by the SessionStart query
    (exact-text match under the synthetic embedder), and the hook script's
    stdout carries a ``hookSpecificOutput.hookEventName == "SessionStart"``
    block containing that actual recalled text.
  * UserPromptSubmit capture: the script posts the prompt to /ingest; after a
    /session/end the drained store's export contains exactly the captured text.

Every daemon interaction goes through the plugin's stdlib-only client, so the
integration proves the plugin works against the live daemon end to end.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import uvicorn

from mnemoseed.daemon.app import create_app

_PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins" / "claude-code"
sys.path.insert(0, str(_PLUGIN_ROOT))

from mnemoseed_hook_client import estimate_tokens  # noqa: E402

_SESSION_A = "sess-plugin-int-warmup"
_SESSION_B = "sess-plugin-int-capture"
_PROFILE = "prof-plugin"

# Proven full-score durable texts (same sentences the capture-funnel tests use),
# so a drained turn is guaranteed to write exactly one chunk on the real funnel.
_DURABLE_PROMPT = "我坚持每次提交前都跑一遍完整的测试"
_WARMUP_TEXT = "我决定以后都用 pnpm 管理依赖来构建前端项目"


@pytest.fixture(autouse=True)
def _ensure_real_drivers():
    """test_daemon clears the shared registries; re-register the real drivers."""
    from mnemoseed.storage.drivers import lancedb_embedded, sqlite_graph, sqlite_meta
    from mnemoseed.storage.drivers.synthetic_embedder import SyntheticEmbedder
    from mnemoseed.storage.registry import (
        EMBED_DRIVERS,
        GRAPH_DRIVERS,
        META_DRIVERS,
        VECTOR_DRIVERS,
        register,
    )

    for registry, cls in (
        (VECTOR_DRIVERS, lancedb_embedded.LanceDbEmbeddedStore),
        (GRAPH_DRIVERS, sqlite_graph.SqliteGraphDriver),
        (META_DRIVERS, sqlite_meta.SqliteMetaDriver),
        (EMBED_DRIVERS, SyntheticEmbedder),
    ):
        if not registry.contains(cls.info.name):
            register(registry)(cls)
    yield


def _serving_config_toml(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        'preset = "embedded"\n'
        f'[storage.vector]\nuri = "{(tmp_path / "chunks.lance").as_posix()}"\ndimensions = 64\n'
        f'[storage.graph]\npath = "{(tmp_path / "cortex.db").as_posix()}"\n'
        f'[storage.meta]\npath = "{(tmp_path / "meta.db").as_posix()}"\n'
        f'[storage.embed]\ndriver = "synthetic"\ndimension = 64\n',
        encoding="utf-8",
    )
    return cfg


class _DaemonHarness:
    """One real daemon booted on the caller's event loop (e2e dual-client boot)."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._tmp = tmp_path
        self._monkeypatch = monkeypatch
        self._task: asyncio.Task | None = None
        self.base_url = ""

    async def __aenter__(self) -> _DaemonHarness:
        self._monkeypatch.delenv("STORAGE_MODE", raising=False)
        self._monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", _serving_config_toml(self._tmp))
        self._monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", self._tmp)
        from mnemoseed.daemon.runner import MnemoseedServer

        config = uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
            access_log=False,
        )
        server = MnemoseedServer(config)
        self._server = server
        self._task = asyncio.create_task(server.serve())
        for _ in range(400):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started, "daemon never started its run loop"
        port = server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._task is not None
        self._server.request_shutdown()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except TimeoutError:
            self._task.cancel()

    async def post(self, path: str, payload: dict) -> dict:
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(f"{self.base_url}{path}", json=payload)
            return {"status": response.status_code, "json": response.json()}


def _base_env(base_url: str, **overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MNEMOSEED_")}
    env["MNEMOSEED_BASE_URL"] = base_url
    env["PYTHONUTF8"] = "1"  # same UTF-8 guarantee that hooks/py.sh gives the shell launch
    env.update(overrides)
    return env


def _run_hook(name: str, stdin_text: str, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    """The hook script as a real subprocess (never blocks the event loop)."""
    return subprocess.run(
        [sys.executable, str(_PLUGIN_ROOT / "hooks" / f"{name}.py")],
        input=stdin_text.encode("utf-8"),
        capture_output=True,
        timeout=60,
        env=env,
    )


async def _post(daemon: _DaemonHarness, path: str, payload: dict) -> dict:
    return await daemon.post(path, payload)


def _parse(stdout: str) -> dict:
    parsed = json.loads(stdout.strip())
    assert isinstance(parsed, dict)
    return parsed


# ------------------------------------------------------------ warm-up path


async def test_session_start_warmup_injects_the_actual_recalled_memory(tmp_path, monkeypatch) -> None:
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        seeded = await _post(daemon, "/memory/remember", {"profile_id": _PROFILE, "text": _WARMUP_TEXT})
        assert seeded["status"] == 200

        env = _base_env(daemon.base_url, MNEMOSEED_PROFILE_ID=_PROFILE)
        env["MNEMOSEED_SESSION_START_QUERY"] = _WARMUP_TEXT
        proc = await asyncio.to_thread(
            _run_hook,
            "session_start",
            json.dumps(
                {
                    "session_id": _SESSION_A,
                    "hook_event_name": "SessionStart",
                    "cwd": "",
                    "permission_mode": "default",
                }
            ),
            env,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        output = _parse(proc.stdout.decode("utf-8"))
        specific = output["hookSpecificOutput"]
        assert specific["hookEventName"] == "SessionStart"
        context = specific["additionalContext"]
        assert _WARMUP_TEXT in context  # the REAL memory flowed back into the session
        assert estimate_tokens(context) <= 800  # the budget gate held end to end


# ------------------------------------------------------------ capture path


async def test_user_prompt_submit_capture_lands_in_the_real_store(tmp_path, monkeypatch) -> None:
    async with _DaemonHarness(tmp_path, monkeypatch) as daemon:
        env = _base_env(daemon.base_url, MNEMOSEED_PROFILE_ID=_PROFILE)
        proc = await asyncio.to_thread(
            _run_hook,
            "user_prompt_submit",
            json.dumps(
                {
                    "session_id": _SESSION_B,
                    "hook_event_name": "UserPromptSubmit",
                    "user_prompt": _DURABLE_PROMPT,
                    "cwd": "",
                }
            ),
            env,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        output = _parse(proc.stdout.decode("utf-8"))
        # per-turn injection found nothing yet (the capture was still buffered),
        # so the hook emitted an empty response -- exit 0, no context
        assert output == {}

        settle = await _post(daemon, "/session/end", {"session_id": _SESSION_B, "profile_id": _PROFILE})
        assert settle["status"] == 200
        assert settle["json"]["turns"] == 1

        exported = await _post(daemon, "/memory/export", {"profile_id": _PROFILE, "limit": 100})
        assert exported["status"] == 200
        chunks = exported["json"]["chunks"]
        texts = [chunk.get("text") or "" for chunk in chunks]
        assert texts, "the drained funnel wrote no chunk"
        # the writing pipeline tags captures with the assertor, so the raw
        # prompt is embedded in the stored chunk text
        assert any(_DURABLE_PROMPT in text for text in texts), texts
