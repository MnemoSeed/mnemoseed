"""mnemoseed CLI entry point.

M0 scope: init / doctor / up / serve / embed-sidecar / version. ``up`` starts
the daemon single-process (embedded preset by default — every driver runs in
one process with zero external services); ``serve`` is kept as an alias.
Account, profile, link and console commands land in M1 with the identity layer.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable

from mnemoseed import __version__
from mnemoseed.config import CONFIG_DIR, CONFIG_PATH, ConfigError, default_config_toml, load_config


def cmd_init(args: argparse.Namespace) -> int:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not args.force:
        print(f"config already exists: {CONFIG_PATH} (use --force to overwrite)")
        return 1
    CONFIG_PATH.write_text(default_config_toml(), encoding="utf-8")
    print(f"initialized {CONFIG_DIR}")
    print(f"config: {CONFIG_PATH}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from mnemoseed.storage.factory import build_stores

    config = load_config()
    print(f"preset: {config.preset}")
    try:
        stores = build_stores(config)
    except Exception as exc:  # doctor reports, never crashes
        print(f"ERROR: storage stack failed to build: {exc}")
        return 1

    for kind, store in (
        ("vector", stores.vector),
        ("graph", stores.graph),
        ("meta", stores.meta),
        ("embed", stores.embed),
    ):
        caps = ", ".join(sorted(c.value for c in store.info.capabilities)) or "(none)"
        print(f"  {kind:7s} {store.info.name:16s} [{caps}]")

    if stores.report.ok:
        print("capability gate: OK")
        return 0
    print("capability gate: DEGRADED")
    for deg in stores.report.missing:
        print(f"  - {deg.feature}: {deg.behavior}")
    return 2


def cmd_up(args: argparse.Namespace) -> int:
    from mnemoseed.daemon.runner import run_server
    from mnemoseed.storage.factory import build_stores
    from mnemoseed.storage.ports import StorageError

    host, port = args.host, args.port
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"preset: {config.preset}")
    if config.preset == "embedded":
        print("embedded single-process daemon - all drivers in-process, zero external services")

    # Resolve the storage stack up front so a bad driver key or invalid params
    # fail with a clean one-line error (same treatment as cmd_doctor) instead of
    # a uvicorn startup traceback. Only config/storage assembly errors are
    # handled here; anything else propagates. The daemon rebuilds the stack
    # inside its lifespan, so the probe build is closed right away.
    try:
        stores = build_stores(config)
        asyncio.run(stores.close())
    except StorageError as exc:
        print(f"error: storage stack failed to build: {exc}", file=sys.stderr)
        return 1

    # Only reach here once the stack is confirmed assemblable, so a failed boot
    # never announces a daemon that did not start.
    print(f"daemon on http://{host}:{port}")
    return run_server(host, port)


def cmd_embed_sidecar(args: argparse.Namespace) -> int:
    from mnemoseed.daemon.embed_sidecar import run_sidecar

    host, port = args.host, args.port
    print(f"embedding sidecar (dev stub) on http://{host}:{port}")
    return run_sidecar(host, port)


def cmd_mcp(args: argparse.Namespace) -> int:
    from mnemoseed.mcp.server import run_server

    return run_server()


def _add_serve_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help_text: str
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7788)
    parser.set_defaults(func=cmd_up)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mnemoseed")
    parser.add_argument("--version", action="version", version=f"mnemoseed {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create ~/.mnemoseed with default config")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="build storage stack and report capabilities")
    p_doctor.set_defaults(func=cmd_doctor)

    _add_serve_parser(sub, "up", "start the daemon (embedded single-process default)")
    _add_serve_parser(sub, "serve", "alias of up (kept for compatibility)")

    p_embed = sub.add_parser(
        "embed-sidecar",
        help="serve the dev OpenAI-compatible embeddings stub (compose embed service)",
    )
    p_embed.add_argument("--host", default="0.0.0.0")
    p_embed.add_argument("--port", type=int, default=7789)
    p_embed.set_defaults(func=cmd_embed_sidecar)

    p_mcp = sub.add_parser(
        "mcp",
        help="run the stdio MCP memory gateway (FR-3.1)",
        description=(
            "Run the stdio MCP memory gateway (FR-3.1): exposes memory.recall / "
            "memory.remember / memory.audit / memory.timeline / memory.export / "
            "memory.forget_this over the MCP protocol. Point MNEMOSEED_BASE_URL "
            "at the running daemon and either set MNEMOSEED_PROFILE_ID or pass "
            "profile_id on every call."
        ),
    )
    p_mcp.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
