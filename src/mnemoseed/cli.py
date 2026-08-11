"""mnemoseed CLI entry point.

M0/M1 scope: init / install / doctor / up / serve / embed-sidecar / uninstall /
version. ``up`` starts the daemon single-process (embedded preset by default —
every driver runs in one process with zero external services); ``serve`` is
kept as an alias. ``install`` / ``uninstall`` / ``doctor`` are the FR-6.1 /
FR-6.7 / FR-6.6 installer surface. Account, profile, link and console commands
land later in M1 with the identity layer.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from typing import Any

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
    from mnemoseed.installer.doctor import run_doctor

    config = load_config()
    report = run_doctor(config)
    print(f"baseurl: {report.baseurl}")
    for check in report.checks:
        state_char = "ok" if check.ok else "FAIL"
        print(f"[{state_char:>4}] {check.name}: {check.detail}")
        if not check.ok and check.fix:
            print(f"      fix: {check.fix}")
    if report.failed:
        print(f"doctor: {len(report.failed)} check(s) failed")
    else:
        print("doctor: all checks passed")
    return report.exit_code


def _install_approver(args: argparse.Namespace) -> Callable[[Any], bool]:
    """Per-item approval: --yes accepts everything, otherwise a y/N prompt.
    No-change plans (already registered) are approved without prompting."""
    from mnemoseed.installer import RegistrationPlan

    def confirm(plan: RegistrationPlan) -> bool:
        if not plan.changed:
            return True
        if args.yes:
            return True
        answer = input(f"apply {plan.describe()}? [y/N] ").strip().lower()
        return answer in ("y", "yes")

    return confirm


def cmd_install(args: argparse.Namespace) -> int:
    from mnemoseed.installer import (
        HostConfigError,
        apply_registrations,
        plan_registrations,
    )

    try:
        plans = plan_registrations(command=args.command, profile_id=args.profile_id)
    except HostConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not plans:
        print("no hosts detected; nothing to install")
        return 0
    print("planned registrations:")
    for plan in plans:
        state_char = "no-op (already registered)" if not plan.changed else "write"
        print(f"  {plan.describe()} [{state_char}]")
        if plan.diff:
            print(plan.diff, end="")
    try:
        report = apply_registrations(plans, approve=_install_approver(args))
    except HostConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for item in report.applied:
        if item.approved and item.changed:
            suffix = f" (backup: {item.backup})" if item.backup else ""
            print(f"  {item.host}: written{suffix}")
        elif item.changed:
            print(f"  {item.host}: skipped (not approved)")
        else:
            print(f"  {item.host}: no changes")
    print(f"installed: {report.written} host registration(s)")
    return 0


def _purge_approver(args: argparse.Namespace) -> Callable[[list[str]], bool]:
    """Approval gate for --purge: show the dry-run list, then --yes or prompt."""

    def confirm(paths: list[str]) -> bool:
        print("purge will delete:")
        for path in paths:
            print(f"  {path}")
        if args.yes:
            return True
        answer = input("delete these MnemoSeed data files? [y/N] ").strip().lower()
        return answer in ("y", "yes")

    return confirm


def cmd_uninstall(args: argparse.Namespace) -> int:
    from mnemoseed.installer import HostConfigError, uninstall

    try:
        report = uninstall(purge=args.purge, approve_purge=_purge_approver(args))
    except HostConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if report.no_op and not report.purge_list:
        print("no MnemoSeed registrations; nothing to uninstall")
    for roll in report.rolls:
        suffix = f" ({roll.detail})" if roll.detail else ""
        print(f"  {roll.host}: {roll.outcome}{suffix}")
    print(f"  daemon: {report.daemon}")
    print(f"  data dir (kept): {report.data_dir}")
    if report.purge_list and not report.purged:
        print("  purge dry-run (would delete):")
        for path in report.purge_list:
            print(f"    {path}")
    if report.purged:
        print(f"  data dir deleted: {report.data_dir}")
    if report.daemon == "failed":
        print("  error: failed to stop the daemon", file=sys.stderr)
        return 1
    return 0


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
    # never announces a daemon that did not start. A daemon launched by this
    # command records its pidfile (pid + process start time) so
    # ``mnemoseed uninstall`` can identify and stop it (FR-6.7); a hard kill
    # leaves a stale pidfile that uninstall discards without touching any
    # unrelated process that recycled the pid.

    from mnemoseed.installer.proc import process_start_epoch
    from mnemoseed.installer.state import PIDFILE_NAME

    pidfile = CONFIG_DIR / PIDFILE_NAME
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    started = process_start_epoch(os.getpid())
    pidfile.write_bytes(f"{os.getpid()}\n{started if started is not None else ''}\n".encode())
    print(f"daemon on http://{host}:{port}")
    try:
        return run_server(host, port)
    finally:
        pidfile.unlink(missing_ok=True)


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

    p_install = sub.add_parser(
        "install",
        help="detect hosts and register the mnemoseed MCP entry (backup + diff + confirm)",
    )
    p_install.add_argument("--yes", action="store_true", help="approve every write without prompting")
    p_install.add_argument(
        "--command", default="mnemoseed", help="MCP server base command (default: mnemoseed)"
    )
    p_install.add_argument(
        "--profile-id",
        default=None,
        help="profile id embedded as MNEMOSEED_PROFILE_ID env (identity seam; token lands with login)",
    )
    p_install.set_defaults(func=cmd_install)

    p_doctor = sub.add_parser("doctor", help="run the FR-6.6 self-check checklist")
    p_doctor.set_defaults(func=cmd_doctor)

    p_uninstall = sub.add_parser(
        "uninstall", help="roll back host registration, stop our daemon, keep data (--purge deletes it)"
    )
    p_uninstall.add_argument("--purge", action="store_true", help="delete the data dir after rollback")
    p_uninstall.add_argument("--yes", action="store_true", help="approve the purge without prompting")
    p_uninstall.set_defaults(func=cmd_uninstall)

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
