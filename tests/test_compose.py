"""docker-compose skeleton (prd-08 FR-8.8): four services, each with a
healthcheck, an optional ollama profile, and structural validity via
`docker compose config` (skipped when docker is unavailable)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this machine"
)


def _compose_config(*extra: str, timeout: int = 60) -> dict:
    result = subprocess.run(
        ["docker", "compose", *extra, "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(result.stdout)


def test_compose_file_is_structuraly_valid() -> None:
    subprocess.run(
        ["docker", "compose", "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def test_compose_has_four_healthchecked_services() -> None:
    data = _compose_config()
    services = data["services"]
    assert set(services) == {"core", "vector", "pg", "embed"}
    for name in ("core", "vector", "pg", "embed"):
        assert "healthcheck" in services[name], f"service {name!r} must declare a healthcheck"
    # core gets a real HTTP /healthz probe; the databases use pg_isready
    assert "pg_isready" in services["vector"]["healthcheck"]["test"][-1]
    assert "pg_isready" in services["pg"]["healthcheck"]["test"][-1]
    core_probe = " ".join(services["core"]["healthcheck"]["test"][1:])
    assert "/healthz" in core_probe


def test_compose_core_waits_for_its_dependencies() -> None:
    data = _compose_config()
    depends = data["services"]["core"]["depends_on"]
    assert set(depends) == {"vector", "pg", "embed"}
    assert all(value.get("condition") == "service_healthy" for value in depends.values())


def test_compose_default_stack_excludes_ollama() -> None:
    data = _compose_config()
    assert "ollama" not in data["services"]


def test_compose_ollama_profile_adds_the_service() -> None:
    data = _compose_config("--profile", "ollama")
    assert "ollama" in data["services"]
