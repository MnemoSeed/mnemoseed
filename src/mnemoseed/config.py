"""Config loading: ~/.mnemoseed/config.toml is the single source of truth.

A preset (embedded/docker/custom) maps each storage layer to a driver; layers
can be overridden individually. STORAGE_MODE remains as a preset shortcut.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.environ.get("MNEMOSEED_HOME", Path.home() / ".mnemoseed"))
CONFIG_PATH = CONFIG_DIR / "config.toml"

PRESETS: dict[str, dict[str, str]] = {
    "embedded": {
        "vector": "chroma_embedded",
        "graph": "sqlite_graph",
        "meta": "sqlite_meta",
        "embed": "gemma_local",
    },
    "docker": {
        "vector": "chroma_embedded",  # chroma container in compose; same driver, other params
        "graph": "sqlite_graph",
        "meta": "sqlite_meta",
        "embed": "gemma_local",
    },
    "custom": {},  # everything explicit; a missing layer is an error
}

VALID_PRESETS = tuple(PRESETS)


@dataclass
class StorageConfig:
    driver: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    preset: str = "embedded"
    storage: dict[str, StorageConfig] = field(default_factory=dict)
    baseurl: str = "http://localhost:7788"
    raw: dict[str, Any] = field(default_factory=dict)

    def layer(self, kind: str) -> StorageConfig:
        """kind in {vector, graph, meta, embed}."""
        if kind in self.storage:
            return self.storage[kind]
        preset_map = PRESETS[self.preset]
        if kind not in preset_map:
            raise KeyError(f"custom preset requires an explicit storage.{kind}.driver")
        return StorageConfig(driver=preset_map[kind])


def load_config(path: Path | None = None) -> Config:
    path = path or CONFIG_PATH
    raw: dict[str, Any] = {}
    if path.exists():
        raw = tomllib.loads(path.read_text(encoding="utf-8"))

    preset = os.environ.get("STORAGE_MODE") or raw.get("preset", "embedded")
    if preset not in PRESETS:
        raise ValueError(f"unknown preset: {preset} (choose from: {', '.join(VALID_PRESETS)})")

    storage: dict[str, StorageConfig] = {}
    for kind, table in (raw.get("storage") or {}).items():
        storage[kind] = StorageConfig(
            driver=table["driver"],
            params={k: v for k, v in table.items() if k != "driver"},
        )

    return Config(
        preset=preset,
        storage=storage,
        baseurl=raw.get("baseurl", "http://localhost:7788"),
        raw=raw,
    )


def default_config_toml() -> str:
    """Default config written by init."""
    return """\
# MnemoSeed configuration — single source of truth
preset = "embedded"          # embedded | docker | custom
baseurl = "http://localhost:7788"

# Per-layer overrides (required under the custom preset):
# [storage.vector]
# driver = "pgvector"
# dsn = "postgresql://user:pass@host:5432/mnemoseed"
#
# [storage.graph]
# driver = "sqlite_graph"
# path = "~/.mnemoseed/graph.db"
"""
