"""Wheel packaging regression for installer templates (issue #9).

``mnemoseed install`` reads host adapter templates from the repo-root
``adapters/`` tree and the ``plugins/claude-code/`` plugin tree. An installed
wheel (``uv tool install .``) shipped neither, so template lookup resolved to a
non-existent repo-relative path and ``mnemoseed install`` crashed with
FileNotFoundError.

The wheel now ships both trees as package data under ``mnemoseed/_templates/``
(hatch ``force-include``, see pyproject.toml) and the installer resolvers in
:mod:`mnemoseed.installer.templating` try the wheel-shipped location before the
dev-checkout repo tree:

* the developer-checkout half is exercised below (and by the ordinary
  installer tests) at no cost;
* the installed-wheel half rebuilds the wheel and installs it into a scratch
  venv, which is too slow for the default suite, so it is opt-in:
  ``MNEMOSEED_TEST_WHEEL_INSTALL=1`` (same spirit as the bge-m3 real-inference
  smoke test, which also only runs when its local resources are present).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from mnemoseed.installer.codexfiles import adapter_templates_dir as codex_templates_dir
from mnemoseed.installer.cursorfiles import adapter_templates_dir as cursor_templates_dir
from mnemoseed.installer.templating import plugin_tree_dir

_REPO_ROOT = Path(__file__).resolve().parents[1]

_WHEEL_INSTALL_ENV = "MNEMOSEED_TEST_WHEEL_INSTALL"

# artifact_texts counts: 5 companion hook scripts + hooks.json + one text file.
_CURSOR_ARTIFACTS = 7
_CODEX_ARTIFACTS = 7


def _venv_python(venv: Path) -> Path:
    bin_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    executable = "python.exe" if os.name == "nt" else "python"
    return bin_dir / executable


def _run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stdout}{proc.stderr}"
        )
    return proc


# ------------------------------------------------------------ dev checkout (in default suite)


def test_dev_checkout_cursor_templates_resolve_repo_tree() -> None:
    assert cursor_templates_dir() == _REPO_ROOT / "adapters" / "cursor" / "templates"


def test_dev_checkout_codex_templates_resolve_repo_tree() -> None:
    assert codex_templates_dir() == _REPO_ROOT / "adapters" / "codex" / "templates"


def test_dev_checkout_plugin_tree_resolves_repo_tree() -> None:
    assert plugin_tree_dir() == _REPO_ROOT / "plugins" / "claude-code"


def test_adapter_templates_env_override_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    override = tmp_path / "custom-templates"
    override.mkdir()
    monkeypatch.setenv("MNEMOSEED_ADAPTER_TEMPLATES", str(override))
    assert cursor_templates_dir() == override
    assert codex_templates_dir() == override


# ------------------------------------------------------------ installed wheel (opt-in)
#
# Rebroadcasts the first dogfood failure of issue #9: build the wheel, install
# it into an isolated scratch venv, and run the template-resolution path plus
# ``mnemoseed install --yes`` from the installed package. The resolved template
# dirs must be the wheel's own package data, not a repo-relative path, and the
# install must not crash.


@pytest.mark.skipif(
    os.environ.get(_WHEEL_INSTALL_ENV) != "1",
    reason="wheel-install regression test is opt-in: set MNEMOSEED_TEST_WHEEL_INSTALL=1",
)
def test_installed_wheel_resolves_adapter_and_plugin_templates(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available; cannot build/install the wheel")

    dist = tmp_path / "dist"
    _run([uv, "build", "--wheel", "--out-dir", str(dist)], cwd=_REPO_ROOT, timeout=300)
    wheel = next(dist.glob("mnemoseed-*.whl"))

    venv = tmp_path / "scratch-venv"
    _run([uv, "venv", str(venv), "--python", sys.executable], timeout=120)
    venv_python = _venv_python(venv)
    # Full deps on purpose: the realistic ``uv tool install`` resolution is
    # exactly what an installed ``mnemoseed install`` runs against.
    _run([uv, "pip", "install", "--python", str(venv_python), str(wheel)], timeout=600)

    check = f"""\
import json, pathlib, sys
from mnemoseed.installer.codexfiles import adapter_templates_dir as xdir, artifact_texts as xat
from mnemoseed.installer.cursorfiles import adapter_templates_dir as cdir, artifact_texts as cat
from mnemoseed.installer.templating import plugin_tree_dir

c = cdir()
assert (c / 'hooks.json').is_file(), c
assert len(cat(c)) == {_CURSOR_ARTIFACTS}, (c, len(cat(c)))
x = xdir()
assert (x / 'hooks.json').is_file(), x
assert len(xat(x)) == {_CODEX_ARTIFACTS}, (x, len(xat(x)))
p = plugin_tree_dir()
assert (p / '.claude-plugin' / 'plugin.json').is_file(), p
assert (p / 'hooks' / 'hooks.json').is_file(), p
manifest = json.loads((p / '.claude-plugin' / 'plugin.json').read_text(encoding='utf-8'))
assert manifest['name'] == 'mnemoseed'

# The installed package must resolve its own wheel data, not the repo tree.
for resolved in (c, x, p):
    assert pathlib.Path(resolved).is_relative_to(pathlib.Path(sys.prefix)), resolved
print('resolved:', c)
"""
    proc = _run([str(venv_python), "-c", check], timeout=120)
    assert "resolved:" in proc.stdout

    # Full CLI reproduction of the original crash: fake HOME with the three
    # detected hosts plus a --cursor-project target must complete without a
    # template FileNotFoundError.
    home = tmp_path / "fake-home"
    data = tmp_path / "fake-data"
    project = tmp_path / "fake-project"
    (home / ".cursor").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    (home / ".claude.json").write_text("{}", encoding="utf-8")
    env = dict(os.environ)
    env["MNEMOSEED_USER_HOME"] = str(home)
    env["MNEMOSEED_HOME"] = str(data)
    env.pop("STORAGE_MODE", None)
    env.pop("MNEMOSEED_ADAPTER_TEMPLATES", None)
    proc = _run(
        [str(venv_python), "-m", "mnemoseed.cli", "install", "--yes", "--cursor-project", str(project)],
        env=env,
        cwd=tmp_path,
        timeout=300,
    )
    combined = proc.stdout + proc.stderr
    for host in (
        "claude-code: written",
        "cursor-hooks: written",
        "cursor-rules: written",
        "codex-hooks: written",
        "codex-agents: written",
    ):
        assert host in combined, (host, combined)
    assert "FileNotFoundError" not in combined
    assert (home / ".cursor" / "mcp.json").exists()
    assert (project / ".cursor" / "hooks.json").exists()
    assert (home / ".codex" / "hooks.json").exists()
