"""docker-compose split by purpose (issue #5): the default stack is ONE service
running the embedded daemon; the Postgres family moves behind the ``pg``
profile and the cloud/VPS deployment behind ``docker-compose.cloud.yml``.
Structural validity goes through real ``docker compose config`` (skipped when
docker is unavailable); the service/wiring facts are asserted on the official
compiler output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
CLOUD_FILE = REPO_ROOT / "docker-compose.cloud.yml"

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker is not available on this machine"
)


def _compose_config(*extra: str, file: Path = COMPOSE_FILE, timeout: int = 60) -> dict:
    """`docker compose -f <file> <extra> config --format json` (compiled output)."""
    result = subprocess.run(
        ["docker", "compose", "-f", str(file), *extra, "config", "--format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(result.stdout)


def _compose_services(*extra: str, file: Path = COMPOSE_FILE) -> set[str]:
    output = subprocess.run(
        ["docker", "compose", "-f", str(file), *extra, "config", "--services"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return set(output.stdout.split())


# ---------------------------------------------------------------- validity


@pytest.mark.parametrize(
    "args",
    [[], ["--profile", "pg"], ["--profile", "ollama"]],
    ids=["default", "pg", "ollama"],
)
def test_compose_config_is_structuraly_valid(args: list[str]) -> None:
    subprocess.run(
        ["docker", "compose", *args, "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


def test_cloud_compose_config_is_structurally_valid() -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(CLOUD_FILE), "config", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )


# ------------------------------------------------------------ default stack


def test_compose_default_is_single_embedded_daemon() -> None:
    """The default `docker compose up` is ONE service: the embedded daemon."""
    assert _compose_services() == {"core"}


def test_compose_default_excludes_the_pg_family_and_ollama() -> None:
    data = _compose_config()
    assert "vector" not in data["services"]
    assert "pg" not in data["services"]
    assert "embed" not in data["services"]
    assert "ollama" not in data["services"]


def test_compose_core_runs_embedded_preset_with_a_data_volume() -> None:
    data = _compose_config()
    core = data["services"]["core"]
    # embedded daemon, identical UX to local `mnemoseed up`
    assert core["command"] == ["mnemoseed", "up", "--host", "0.0.0.0", "--port", "7788"]
    assert core["environment"]["MNEMOSEED_HOME"] == "/data"
    targets = [mount["target"] for mount in core["volumes"]]
    assert "/data" in targets
    mount = next(m for m in core["volumes"] if m["target"] == "/data")
    assert mount["type"] == "volume"


def test_compose_core_has_a_real_http_healthcheck() -> None:
    core = _compose_config()["services"]["core"]
    assert "healthcheck" in core
    probe = " ".join(core["healthcheck"]["test"][1:])
    assert "/healthz" in probe


# -------------------------------------------------------------- pg profile


def test_compose_pg_profile_restores_the_full_stack() -> None:
    services = _compose_services("--profile", "pg")
    assert services == {"core", "vector", "pg", "embed"}


def test_compose_pg_profile_services_each_have_a_healthcheck() -> None:
    data = _compose_config("--profile", "pg")
    for name in ("core", "vector", "pg", "embed"):
        assert "healthcheck" in data["services"][name], f"service {name!r} must declare a healthcheck"
    assert "pg_isready" in data["services"]["vector"]["healthcheck"]["test"][-1]
    assert "pg_isready" in data["services"]["pg"]["healthcheck"]["test"][-1]
    core_probe = " ".join(data["services"]["core"]["healthcheck"]["test"][1:])
    assert "/healthz" in core_probe


def test_compose_pg_profile_keeps_the_developer_ports() -> None:
    """The dev-reserved published ports are unchanged for PG work on the host."""
    data = _compose_config("--profile", "pg")
    published = {
        name: {p["published"] for p in data["services"][name].get("ports", [])}
        for name in ("vector", "pg", "embed")
    }
    assert published["vector"] == {"55433"}
    assert published["pg"] == {"55434"}
    assert published["embed"] == {"7789"}


# ------------------------------------------------------------ ollama profile


def test_compose_ollama_profile_adds_the_service() -> None:
    assert _compose_services("--profile", "ollama") == {"core", "ollama"}


# ------------------------------------------------------- cloud (design/10)


def test_cloud_compose_is_single_daemon_with_persistent_volume() -> None:
    data = _compose_config(file=CLOUD_FILE)
    assert set(data["services"]) == {"core"}
    core = data["services"]["core"]
    assert core["command"] == ["mnemoseed", "up", "--host", "0.0.0.0", "--port", "7788"]
    assert core["environment"]["MNEMOSEED_HOME"] == "/data"
    assert "healthcheck" in core
    targets = [mount["target"] for mount in core["volumes"]]
    assert "/data" in targets


def test_cloud_compose_binds_loopback_and_restarts() -> None:
    """design/10 section 3: never expose the raw HTTP port; the reverse proxy
    terminates TLS. The container stays up via restart: unless-stopped."""
    core = _compose_config(file=CLOUD_FILE)["services"]["core"]
    assert core["restart"] == "unless-stopped"
    ports = core["ports"]
    assert len(ports) == 1
    assert ports[0]["target"] == 7788
    assert ports[0]["published"] == "7788"
    assert ports[0].get("host_ip") == "127.0.0.1"


def test_cloud_compose_passes_through_the_admin_token_var() -> None:
    core = _compose_config(file=CLOUD_FILE)["services"]["core"]
    assert "MNEMOSEED_CONSOLE_ADMIN_TOKEN" in core["environment"]
