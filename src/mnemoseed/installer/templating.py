"""Adapter/plugin template resolution shared by the installer (wheel-safe).

The T2 installer writes host adapter artifacts (Cursor hooks/rules, Codex
hooks/AGENTS.md) from template trees that live at the repo root
(``adapters/<host>/templates`` and ``plugins/claude-code/``). ``uv tool
install`` wheels those trees into the package as ``mnemoseed/_templates/`` via
hatch ``force-include`` (pyproject.toml), so a lookup tries, in order:

1. ``MNEMOSEED_ADAPTER_TEMPLATES`` -- an explicit test/one-off override that
   points directly at a templates directory;
2. the wheel-shipped ``mnemoseed/_templates/`` -- the installed-package
   location that makes ``mnemoseed install`` work after ``uv tool install .``;
3. the repo-root tree -- the dev-checkout fallback.

A dev checkout resolves through (3), an installed wheel through (2), and both
get a consistent templates root without the installer ever touching the
marketplace-distribution layout (design/06 section 2.1).
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_ADAPTER_TEMPLATES = "MNEMOSEED_ADAPTER_TEMPLATES"

# mnemoseed/installer/templating.py -> <site-packages>/mnemoseed/_templates
_PACKAGE_DATA = Path(__file__).resolve().parents[1] / "_templates"
# mnemoseed/installer/templating.py -> mnemoseed -> installer -> src -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]

PLUGIN_SUBDIR = Path("plugins") / "claude-code"


def adapter_templates_dir(host: str) -> Path:
    """The directory holding one adapter host's install templates.

    ``host`` is the adapter name under ``adapters/`` -- ``cursor`` and ``codex``
    are the T2 installer hosts; the same resolver serves any future adapter.
    The env override points directly at a templates directory; otherwise the
    wheel-shipped package data wins when present and the repo-root tree is the
    dev-checkout fallback.
    """
    raw = os.environ.get(ENV_ADAPTER_TEMPLATES)
    if raw:
        return Path(raw).expanduser()
    candidates = (
        _PACKAGE_DATA / "adapters" / host / "templates",
        _REPO_ROOT / "adapters" / host / "templates",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def plugin_tree_dir() -> Path:
    """The ``plugins/claude-code`` plugin tree shipped with the wheel.

    Returns the wheel data copy when installed, the repo-root marketplace tree
    in a dev checkout, and never a non-existent path.
    """
    for candidate in (_PACKAGE_DATA / PLUGIN_SUBDIR, _REPO_ROOT / PLUGIN_SUBDIR):
        if candidate.is_dir():
            return candidate
    return _PACKAGE_DATA / PLUGIN_SUBDIR
