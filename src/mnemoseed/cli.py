"""mnemoseed CLI entry point.

M0 scope: init / doctor / serve / version. Account, profile, link and console
commands land in M1 with the identity layer.
"""

from __future__ import annotations

import argparse
import sys

from mnemoseed import __version__
from mnemoseed.config import CONFIG_DIR, CONFIG_PATH, default_config_toml, load_config


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


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("mnemoseed.daemon.app:app", host=args.host, port=args.port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mnemoseed")
    parser.add_argument("--version", action="version", version=f"mnemoseed {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create ~/.mnemoseed with default config")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="build storage stack and report capabilities")
    p_doctor.set_defaults(func=cmd_doctor)

    p_serve = sub.add_parser("serve", help="run the daemon")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7788)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
