# syntax=docker/dockerfile:1
# MnemoSeed core daemon image (uv-managed, slim).
#
# One image serves the compose `core` service (daemon) and the `embed` sidecar:
# the container command selects which app runs. The docker preset config
# (docker/config.toml) is baked into /etc/mnemoseed so `mnemoseed up` resolves
# the pg drivers against the compose network (hostnames `vector` / `pg`).

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    MNEMOSEED_HOME=/etc/mnemoseed

WORKDIR /app

# Dependency layer: lockfile first so `uv sync` results cache across rebuilds
# (only the source below invalidates it).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Source + package install.
COPY README.md ./
COPY src ./src
COPY docker/config.toml /etc/mnemoseed/config.toml
RUN uv sync --frozen --no-dev

EXPOSE 7788 7789

CMD ["mnemoseed", "up", "--host", "0.0.0.0", "--port", "7788"]
