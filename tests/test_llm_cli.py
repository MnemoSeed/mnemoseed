"""LLM admin CLI (issue #23; FR-6.9 wizard + design/07 section 8).

``mnemoseed llm`` shares the SAME validation + persistence service the console
API uses (LLMAdminService), so a route changed through the CLI reads back
identically through the file and stays out of the daemon's memory surface:

- ``mnemoseed llm status``  shows the per-role routes with a live connectivity
                            probe and the driver catalog, and never echoes a
                            token value.
- ``mnemoseed llm set <role> --driver/--model/--base-url/--api-key-env``
                            persists a surgical TOML patch (validation errors
                            exit 1 with a typed message).

The CLI is offline: it writes the config file directly and does not probe
connectivity on ``set`` (a network-free response), exactly like the API path.
"""

from __future__ import annotations

from pathlib import Path

from mnemoseed.cli import main
from mnemoseed.config import LLM_ROLES, load_config

_SECRET = "sk-ultra-secret-cli-value"


def _config_toml(tmp_path: Path) -> Path:
    """Network-free routes so the status probe never leaves the host."""
    p = tmp_path / "config.toml"
    p.write_text(
        'preset = "embedded"\n'
        "[dream.llm.deep_reflection]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[dream.llm.short_increment]\n"
        'driver = "stub"\n'
        'model = "stub"\n'
        "[dream.llm.local_track]\n"
        'driver = "ollama"\n'
        'model = "llama3.1:8b"\n'
        'base_url = "http://127.0.0.1:1"\n',
        encoding="utf-8",
    )
    return p


def _env(tmp_path: Path, monkeypatch) -> Path:
    cfg = _config_toml(tmp_path)
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    return cfg


# ---------------------------------------------------------------- llm status


def test_llm_status_lists_all_roles_with_routes_and_connectivity(tmp_path, monkeypatch, capsys) -> None:
    cfg = _env(tmp_path, monkeypatch)
    code = main(["llm", "status"])
    captured = capsys.readouterr()
    assert code == 0
    for role in LLM_ROLES:
        assert role in captured.out
    assert "deep_reflection" in captured.out
    assert "connectivity: ok" in captured.out  # stub probe is healthy
    assert "ollama" in captured.out
    assert str(cfg) in captured.out  # the source file is named


def test_llm_status_reports_failed_connectivity_for_closed_port(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    code = main(["llm", "status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "local_track" in captured.out
    assert "connectivity: FAIL" in captured.out


def test_llm_status_never_emits_secret_values(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("MNEMOSEED_TEST_SECRET", _SECRET)
    # an attested env name is fine; its value must never appear -- add the name
    # only (never the value) into the short_increment row, then patch the env
    # directly (do not re-run _config_toml, which would clobber the edit)
    cfg = _config_toml(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            '[dream.llm.short_increment]\ndriver = "stub"\nmodel = "stub"\n',
            '[dream.llm.short_increment]\ndriver = "stub"\nmodel = "stub"\n'
            'api_key_env = "MNEMOSEED_TEST_SECRET"\n',
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("STORAGE_MODE", raising=False)
    monkeypatch.delenv("MNEMOSEED_USER_HOME", raising=False)
    monkeypatch.setattr("mnemoseed.config.CONFIG_PATH", cfg)
    monkeypatch.setattr("mnemoseed.dream.snapshot.CONFIG_DIR", tmp_path)
    code = main(["llm", "status"])
    captured = capsys.readouterr()
    assert code == 0
    assert "MNEMOSEED_TEST_SECRET" in captured.out  # the NAME is reported
    assert _SECRET not in captured.out  # the value never is


# ---------------------------------------------------------------- llm set


def test_llm_set_persists_route_and_reads_back(tmp_path, monkeypatch, capsys) -> None:
    cfg = _env(tmp_path, monkeypatch)
    code = main(
        [
            "llm",
            "set",
            "short_increment",
            "--driver",
            "anthropic",
            "--model",
            "claude-sonnet-5",
            "--api-key-env",
            "ANTHROPIC_API_KEY",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "short_increment" in captured.out

    text = cfg.read_text(encoding="utf-8")
    assert 'driver = "anthropic"' in text
    assert 'model = "claude-sonnet-5"' in text
    assert 'api_key_env = "ANTHROPIC_API_KEY"' in text
    reread = load_config(cfg).llm["short_increment"]
    assert reread.driver == "anthropic"
    assert reread.model == "claude-sonnet-5"
    assert reread.params["api_key_env"] == "ANTHROPIC_API_KEY"
    # unrelated roles untouched
    assert load_config(cfg).llm["deep_reflection"].driver == "stub"
    assert load_config(cfg).llm["local_track"].driver == "ollama"


def test_llm_set_clears_optional_param(tmp_path, monkeypatch, capsys) -> None:
    cfg = _env(tmp_path, monkeypatch)
    code = main(["llm", "set", "short_increment", "--base-url", "http://example.test"])
    assert code == 0
    assert load_config(cfg).llm["short_increment"].params["base_url"] == "http://example.test"
    code = main(["llm", "set", "short_increment", "--base-url", ""])
    captured = capsys.readouterr()
    assert code == 0
    # the endpoint is no longer pinned in the config file
    assert 'base_url = "http://example.test"' not in cfg.read_text(encoding="utf-8")
    assert "http://example.test" not in captured.out


def test_llm_set_unknown_role_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    code = main(["llm", "set", "no_such_role", "--driver", "stub", "--model", "m"])
    captured = capsys.readouterr()
    assert code == 1
    assert "unknown llm role" in captured.err


def test_llm_set_unknown_driver_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--driver", "no_such_driver", "--model", "m"])
    captured = capsys.readouterr()
    assert code == 1
    assert "unknown llm driver" in captured.err


def test_llm_set_empty_model_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--driver", "stub", "--model", ""])
    captured = capsys.readouterr()
    assert code == 1
    assert "model" in captured.err


def test_llm_set_oauth_without_provider_exits_1(tmp_path, monkeypatch, capsys) -> None:
    _env(tmp_path, monkeypatch)
    code = main(["llm", "set", "deep_reflection", "--driver", "oauth", "--model", "gpt-5.6-codex"])
    captured = capsys.readouterr()
    assert code == 1
    assert "oauth" in captured.err
