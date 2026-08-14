"""ConfigWriteService (PRD-07 FR-7.11 / design/07 section 9, W1.1): the
daemon-owned single config writer.

Service-level contract, unit-testing the registry -> validate -> surgical toml
patch -> versioned meta-store record -> audit -> live-apply flow without the
HTTP layer:

- the key-path registry is seeded with the keys that exist today
  (dream.auto_trigger, dream.token_budget_usd, and the per-role
  driver/model/base_url/api_key_env/max_tokens fields) and an unknown key is a
  typed error naming the key;
- every write is a surgical line-oriented TOML patch: comments, layout and
  unrelated keys survive, and an existing value line is rewritten in place
  (never duplicated);
- with a meta store the write lands a versioned record (set_config) and an
  audit entry with actor attribution; without one the service still patches the
  file (offline mode) but records nothing;
- api_key_env accepts env-var NAME lists only -- anything key-like is a
  validation failure that names the key;
- rollback is append-only (a new version record, never a delete) and restores
  both the file and the live config;
- boot reconciliation re-baselines the versioned store when the file's
  mtime/hash differs from the last-known state and records a
  config_rebaseline audit entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mnemoseed.config import LLM_ROLES, load_config
from mnemoseed.configwrite.service import (
    CONFIG_KEY_REGISTRY,
    ConfigWriteError,
    ConfigWriteService,
)
from mnemoseed.storage.drivers.sqlite_meta import SqliteMetaDriver
from mnemoseed.storage.ports import AuditFilter, Page

_AUDIT_ACTIONS = ("config.set", "config.rollback", "config_rebaseline")


def _config_toml(tmp_path: Path) -> Path:
    """A config with comments, a [dream] table and the three role tables."""
    path = tmp_path / "config.toml"
    path.write_text(
        "# MnemoSeed configuration\n"
        'preset = "embedded"\n'
        'baseurl = "http://localhost:7788"\n'
        "\n"
        "# Dream-engine section\n"
        "[dream]\n"
        "token_budget_usd = 5.0\n"
        "\n"
        "[dream.llm.deep_reflection]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        'base_url = "http://example.test/v1"\n'
        "\n"
        "[dream.llm.short_increment]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "\n"
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "llama3.1:8b"\n',
        encoding="utf-8",
    )
    return path


def _meta(tmp_path: Path) -> SqliteMetaDriver:
    return SqliteMetaDriver(path=str(tmp_path / "meta.db"))


def _service(tmp_path: Path, *, meta: SqliteMetaDriver | None = None) -> tuple[ConfigWriteService, Path]:
    path = _config_toml(tmp_path)
    return ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0), path


def _audit_entries(meta: SqliteMetaDriver, action: str) -> list[object]:
    return meta.audit_query(AuditFilter(action=action), Page(limit=100)).items


# ---------------------------------------------------------------- registry


def test_registry_seeded_with_writable_keys() -> None:
    """FR-7.11: the registry carries every key the system writes today."""
    expected = {"dream.auto_trigger", "dream.token_budget_usd"}
    for role in LLM_ROLES:
        for field in ("driver", "model", "base_url", "api_key_env", "max_tokens"):
            expected.add(f"dream.llm.{role}.{field}")
    assert expected <= set(CONFIG_KEY_REGISTRY)


def test_unknown_key_is_typed_error_naming_the_key(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[scoring\.w1\]"):
        service.set("scoring.w1", 0.5, actor="cli")


# ---------------------------------------------------------------- surgical patch


def test_set_patches_dream_table_preserving_comments(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.auto_trigger", True, actor="console")
    text = path.read_text(encoding="utf-8")
    # the flag landed inside [dream], and the file's comments/layout survived
    assert "# Dream-engine section" in text
    assert "token_budget_usd = 5.0" in text
    assert "auto_trigger = true" in text
    # unrelated role tables are untouched
    assert 'driver = "ollama"' in text
    # the whole file still parses, and the change round-trips through the loader
    assert load_config(path).dream.auto_trigger is True


def test_set_rewrites_existing_line_in_place(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.auto_trigger", True, actor="console")
    service.set("dream.auto_trigger", False, actor="console")
    keys = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("auto_trigger =")
    ]
    assert keys == ["auto_trigger = false"]


def test_set_inserts_dream_table_when_missing(tmp_path) -> None:
    path = _config_toml(tmp_path)
    text = path.read_text(encoding="utf-8")
    # drop the [dream] table (keep the role tables): the flag must create a new
    # [dream] table instead of leaking into a role table
    text = text.replace("[dream]\ntoken_budget_usd = 5.0\n\n", "")
    path.write_text(text, encoding="utf-8")
    service = ConfigWriteService(load_config(path), None, clock=lambda: 1_700_000_000.0)
    service.set("dream.auto_trigger", True, actor="console")
    assert load_config(path).dream.auto_trigger is True
    assert load_config(path).dream.token_budget_usd == 5.0


def test_set_patches_role_table_and_in_memory_llm(tmp_path) -> None:
    service, path = _service(tmp_path)
    result = service.set("dream.llm.deep_reflection.driver", "anthropic", actor="cli")
    assert result["ok"] is True
    text = path.read_text(encoding="utf-8")
    assert 'driver = "anthropic"' in text
    assert 'model = "stub"' in text  # sibling field untouched
    assert load_config(path).llm["deep_reflection"].driver == "anthropic"
    # live-apply: the running Config reflects the change immediately
    assert service._config.llm["deep_reflection"].driver == "anthropic"


def test_set_role_param_and_clear(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.llm.short_increment.base_url", "http://custom.test", actor="console")
    assert load_config(path).llm["short_increment"].params["base_url"] == "http://custom.test"
    service.set("dream.llm.short_increment.base_url", "", actor="console")
    text = path.read_text(encoding="utf-8")
    # the cleared field is gone from the short_increment table (the
    # deep_reflection route legitimately keeps its own base_url line); the
    # live config and its raw mirror drop the explicit value too (a fresh
    # load_config re-merges only the DEFAULT base_url fallback, which is a
    # loader default, not an explicit write)
    table = text.split("[dream.llm.short_increment]", 1)[1].split("[", 1)[0]
    assert "base_url" not in table
    assert "base_url" not in service._config.llm["short_increment"].params
    assert "base_url" not in service._config.raw["dream"]["llm"]["short_increment"]
    assert load_config(path).dream.auto_trigger is False  # the file still parses


def test_set_api_key_env_persists_names_only(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set(
        "dream.llm.short_increment.api_key_env",
        "MNEMOSEED_SHORT_INCREMENT_API_KEY,FIREWORKS_API_KEY",
        actor="console",
    )
    text = path.read_text(encoding="utf-8")
    assert 'api_key_env = "MNEMOSEED_SHORT_INCREMENT_API_KEY,FIREWORKS_API_KEY"' in text
    assert load_config(path).llm["short_increment"].params["api_key_env"].startswith("MNEMOSEED_")


# ---------------------------------------------------------------- typed validation


def test_set_auto_trigger_requires_boolean(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.auto_trigger\].*boolean"):
        service.set("dream.auto_trigger", "yes", actor="console")


def test_set_token_budget_requires_positive_number(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.token_budget_usd\]"):
        service.set("dream.token_budget_usd", -1, actor="console")


def test_set_max_tokens_requires_positive_int(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.deep_reflection\.max_tokens\]"):
        service.set("dream.llm.deep_reflection.max_tokens", 1.5, actor="console")


def test_set_driver_requires_non_empty_string(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.deep_reflection\.driver\]"):
        service.set("dream.llm.deep_reflection.driver", "  ", actor="console")


def test_set_api_key_env_rejects_key_like_values(tmp_path) -> None:
    service, _ = _service(tmp_path)
    for bad in ("sk-abc123", "sk-proj-deadbeef", "openai_api_key"):
        with pytest.raises(ConfigWriteError, match=r"config\[dream\.llm\.short_increment\.api_key_env\]"):
            service.set("dream.llm.short_increment.api_key_env", bad, actor="console")


def test_set_api_key_env_empty_clears(tmp_path) -> None:
    service, path = _service(tmp_path)
    service.set("dream.llm.short_increment.api_key_env", "MNEMOSEED_SHORT_INCREMENT_API_KEY", actor="console")
    service.set("dream.llm.short_increment.api_key_env", "", actor="console")
    assert "api_key_env" not in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- versioned record + audit


def test_set_records_version_and_audits_actor(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    result = service.set("dream.auto_trigger", True, actor="cli")
    assert result["ok"] is True
    assert isinstance(result["version_id"], int)
    assert result["restart_required"] is False  # seeded keys are all live-apply

    entry = meta.get_config("dream.auto_trigger")
    assert entry is not None
    assert entry.value["value"] is True
    assert entry.version == 1

    audit = _audit_entries(meta, "config.set")
    assert len(audit) == 1
    assert audit[0].actor == "cli"
    assert audit[0].detail["key_path"] == "dream.auto_trigger"
    assert audit[0].detail["value"] is True


def test_set_without_meta_patches_file_but_records_nothing(tmp_path) -> None:
    service, path = _service(tmp_path)  # meta=None: offline mode
    result = service.set("dream.auto_trigger", True, actor="console")
    assert result["version_id"] is None
    assert "auto_trigger = true" in path.read_text(encoding="utf-8")


def test_versions_lists_history_without_internal_keys(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    service.set("dream.auto_trigger", True, actor="console")
    service.set("dream.llm.short_increment.driver", "anthropic", actor="console")
    versions = service.versions()
    by_key = [v for v in versions if v["key"] == "dream.auto_trigger"]
    assert len(by_key) == 1
    assert isinstance(by_key[0]["version_id"], int)
    assert by_key[0]["value"] is True
    assert all("__" not in v["key"] for v in versions)


# ---------------------------------------------------------------- rollback (append-only)


def test_rollback_restores_file_and_live_config_append_only(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    first = service.set("dream.auto_trigger", True, actor="console")["version_id"]
    service.set("dream.auto_trigger", False, actor="console")

    rolled = service.rollback(first, actor="console")
    assert rolled["ok"] is True
    assert rolled["restored"] == rolled["version_id"]
    assert "auto_trigger = true" in path.read_text(encoding="utf-8")
    assert service._config.dream.auto_trigger is True
    assert load_config(path).dream.auto_trigger is True

    # append-only: the rollback is a NEW version; every record survives
    entries = meta.get_config("dream.auto_trigger")
    assert entries is not None
    assert entries.version == 3
    assert entries.value["value"] is True
    assert meta.get_config("dream.auto_trigger", 2).value["value"] is False  # the reverted state stays

    audit = _audit_entries(meta, "config.rollback")
    assert len(audit) == 1
    assert audit[0].actor == "console"
    assert audit[0].detail["key_path"] == "dream.auto_trigger"


def test_rollback_unknown_version_is_typed_error(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    with pytest.raises(ConfigWriteError, match="version"):
        service.rollback(9_999_999_999, actor="console")


def test_rollback_without_meta_is_typed_error(tmp_path) -> None:
    service, _ = _service(tmp_path)
    with pytest.raises(ConfigWriteError, match="versioned"):
        service.rollback(1, actor="console")


# ---------------------------------------------------------------- boot reconciliation


def test_reconcile_first_boot_baselines_and_audits(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    result = service.reconcile_boot()
    assert result["ok"] is True
    assert result["reason"] == "initial"
    assert "dream.auto_trigger" in result["keys_updated"]
    entry = meta.get_config("dream.auto_trigger")
    assert entry is not None and entry.value["value"] is False
    audit = _audit_entries(meta, "config_rebaseline")
    assert len(audit) == 1
    assert audit[0].actor == "daemon"
    assert audit[0].detail["reason"] == "initial"


def test_reconcile_unchanged_is_noop(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    assert service.reconcile_boot()["changed"] is False
    assert len(_audit_entries(meta, "config_rebaseline")) == 1


def test_reconcile_hand_edit_rebaselines_next_boot(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, path = _service(tmp_path, meta=meta)
    service.reconcile_boot()
    # a user hand-edits the file while the daemon is down
    text = path.read_text(encoding="utf-8").replace(
        "token_budget_usd = 5.0\n", "token_budget_usd = 5.0\nauto_trigger = true\n"
    )
    path.write_text(text, encoding="utf-8")
    # next boot: a fresh load + reconcile detects the divergence
    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    result = service.reconcile_boot()
    assert result["changed"] is True
    assert result["reason"] == "hand_edit"
    assert "dream.auto_trigger" in result["keys_updated"]
    assert meta.get_config("dream.auto_trigger").value["value"] is True
    audit = _audit_entries(meta, "config_rebaseline")
    assert len(audit) == 2
    assert audit[1].detail["reason"] == "hand_edit"


def test_reconcile_without_meta_is_noop(tmp_path) -> None:
    service, _ = _service(tmp_path)
    assert service.reconcile_boot()["ok"] is False


# ---------------------------------------------------------------- resolved read (redacted)


def test_get_resolves_config_with_env_names_only(tmp_path) -> None:
    meta = _meta(tmp_path)
    service, _ = _service(tmp_path, meta=meta)
    body = service.get()
    config = body["config"]
    assert config["preset"] == "embedded"
    assert config["dream"]["token_budget_usd"] == 5.0
    deep = config["dream"]["llm"]["deep_reflection"]
    assert deep["driver"] == "stub"
    assert deep["model"] == "stub"
    assert deep["base_url"] == "http://example.test/v1"
    assert body["restart_required"] == {}


def test_get_redacts_literal_key_slipped_in_by_hand_edit(tmp_path) -> None:
    meta = _meta(tmp_path)
    path = _config_toml(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[dream.llm.deep_reflection]\n",
            '[dream.llm.deep_reflection]\napi_key_env = "sk-proj-literal-value"\n',
        ),
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    blob = repr(service.get())
    assert "sk-proj-literal-value" not in blob
    assert "api_key_env" in blob  # the NAMES field still surfaces


def test_get_redacts_versions_too(tmp_path) -> None:
    meta = _meta(tmp_path)
    path = _config_toml(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "[dream.llm.deep_reflection]\n",
            '[dream.llm.deep_reflection]\napi_key_env = "sk-proj-literal-value"\n',
        ),
        encoding="utf-8",
    )
    service = ConfigWriteService(load_config(path), meta, clock=lambda: 1_700_000_000.0)
    service.reconcile_boot()
    assert "sk-proj-literal-value" not in repr(service.versions())
