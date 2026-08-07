"""Config loading and preset resolution."""

import pytest

from mnemoseed.config import Config, default_config_toml, load_config


def test_default_config_is_embedded(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.preset == "embedded"
    assert cfg.layer("vector").driver == "chroma_embedded"
    assert cfg.layer("graph").driver == "sqlite_graph"
    assert cfg.layer("meta").driver == "sqlite_meta"
    assert cfg.layer("embed").driver == "gemma_local"


def test_env_overrides_preset(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text('preset = "embedded"\n', encoding="utf-8")
    monkeypatch.setenv("STORAGE_MODE", "docker")
    assert load_config(p).preset == "docker"


def test_unknown_preset_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    p.write_text('preset = "nope"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="preset"):
        load_config(p)


def test_custom_requires_explicit_layers():
    cfg = Config(preset="custom")
    with pytest.raises(KeyError):
        cfg.layer("vector")


def test_layer_override():
    cfg = Config(preset="embedded")
    from mnemoseed.config import StorageConfig

    cfg.storage["vector"] = StorageConfig(driver="pgvector", params={"dsn": "x"})
    assert cfg.layer("vector").driver == "pgvector"
    assert cfg.layer("graph").driver == "sqlite_graph"  # others fall back to preset


def test_default_toml_parses(tmp_path, monkeypatch):
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    p = tmp_path / "config.toml"
    p.write_text(default_config_toml(), encoding="utf-8")
    assert load_config(p).preset == "embedded"
