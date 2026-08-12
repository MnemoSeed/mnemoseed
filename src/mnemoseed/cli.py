"""mnemoseed CLI entry point.

Scope: init / install / doctor / up / serve / embed-sidecar / uninstall /
version, plus the identity chain (issue #14): ``login`` / ``logout`` /
``whoami`` / ``auth reset``, the dream LLM route manager (issue #23):
``llm status`` / ``llm set``, and the daemon autostart manager (issue #6):
``startup enable`` / ``startup disable`` / ``startup status``. ``up`` starts
the daemon single-process (embedded preset by default — every driver runs in
one process with zero external services); ``serve`` is kept as an alias.
``install`` / ``uninstall`` / ``doctor`` are the FR-6.1 / FR-6.7 / FR-6.6
installer surface.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
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
        trust_guidance_lines,
    )

    try:
        cursor_project = Path(args.cursor_project) if args.cursor_project else None
        plans = plan_registrations(
            command=args.command,
            profile_id=args.profile_id,
            cursor_project=cursor_project,
        )
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
    if any(plan.host in ("codex", "codex-hooks", "codex-agents") for plan in plans):
        for line in trust_guidance_lines():
            print(line)
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


# ------------------------------------------------------------ identity (issue #14)


def _base_url(args: argparse.Namespace, fallback: str | None = None) -> str:
    """Daemon base URL: --baseurl, then fallback, then the config baseurl."""
    from mnemoseed.config import load_config

    if args.baseurl:
        return str(args.baseurl).rstrip("/")
    if fallback:
        return fallback.rstrip("/")
    return load_config().baseurl.rstrip("/")


def cmd_login(args: argparse.Namespace) -> int:
    """POST /api/v1/auth/login and persist the profile token (0600 session file)."""
    import getpass

    import httpx

    from mnemoseed.identity.session import AuthSession, save_session

    base_url = _base_url(args)
    username = (args.username or "").strip()
    if not username:
        username = input("username: ").strip()
    password = args.password
    if password is None:
        password = getpass.getpass("password: ")
    try:
        response = httpx.post(
            f"{base_url}/api/v1/auth/login",
            json={"username": username, "password": password},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"error: cannot reach {base_url}: {exc}", file=sys.stderr)
        return 1
    if response.status_code == 401:
        print("error: invalid username or password", file=sys.stderr)
        return 1
    if response.status_code == 503:
        print(
            "error: setup required - no owner account exists yet (open the console setup wizard)",
            file=sys.stderr,
        )
        return 1
    if response.status_code >= 400:
        print(f"error: login failed ({response.status_code})", file=sys.stderr)
        return 1
    body = response.json()
    session = AuthSession(
        base_url=base_url,
        username=str(body["username"]),
        profile_id=str(body["profile_id"]),
        token=str(body["token"]),
        expires_at=float(body["expires_at"]) if body.get("expires_at") is not None else None,
    )
    path = save_session(session)
    prefix = f"logged in as {session.username} (profile {session.profile_id}"
    if session.expires_at is not None:
        import time

        days = max(0, int((session.expires_at - time.time()) // 86400))
        prefix += f", token expires in ~{days}d"
    print(f"{prefix})")
    print(f"session: {path}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Revoke the token server-side (best-effort) and delete the local session."""
    import httpx

    from mnemoseed.identity import session as auth_session

    existing = auth_session.load_session()
    if existing is None:
        print("not logged in (no stored session to revoke)")
        return 0
    base_url = _base_url(args, existing.base_url)
    # Server-side revocation is best-effort; the local file is the source of
    # truth for a logged-out state (the token may already be revoked/expired).
    try:
        httpx.post(
            f"{base_url}/api/v1/auth/logout",
            headers=auth_session.bearer_headers(existing.token),
            timeout=30.0,
        )
    except httpx.HTTPError:
        pass
    auth_session.delete_session()
    print("logged out")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    """Report the stored session identity and validate the token against the daemon."""
    import httpx

    from mnemoseed.identity import session as auth_session

    existing = auth_session.load_session()
    if existing is None:
        print("not logged in (run `mnemoseed login`)", file=sys.stderr)
        return 1
    base_url = _base_url(args, existing.base_url)
    try:
        response = httpx.get(
            f"{base_url}/api/v1/auth/me",
            headers=auth_session.bearer_headers(existing.token),
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        print(f"error: cannot reach {base_url}: {exc}", file=sys.stderr)
        return 1
    if response.status_code == 401:
        print("error: token invalid or expired (run `mnemoseed login`)", file=sys.stderr)
        return 1
    if response.status_code == 503:
        print("error: setup required - no owner account exists yet", file=sys.stderr)
        return 1
    if response.status_code >= 400:
        print(f"error: whoami failed ({response.status_code})", file=sys.stderr)
        return 1
    body = response.json()
    print(f"daemon:   {base_url}")
    print(f"username: {body['username']}")
    print(f"profile:  {body['profile_id']}")
    print(f"role:     {body['role']}")
    if existing.expires_at is not None:
        from datetime import datetime

        when = datetime.fromtimestamp(existing.expires_at, tz=UTC).astimezone()
        print(f"expires:  {when.isoformat(timespec='minutes')}")
    return 0


def cmd_auth_reset(args: argparse.Namespace) -> int:
    """Local-only owner password reset (design/06 2.7): direct meta-store access."""
    import asyncio
    import getpass

    from mnemoseed.config import ConfigError, load_config
    from mnemoseed.identity import IdentityService
    from mnemoseed.storage.registry import DRIVER_REGISTRIES

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    password = args.password
    if password is None:
        password = getpass.getpass("new password: ")
        confirm = getpass.getpass("confirm new password: ")
        if password != confirm:
            print("error: passwords do not match", file=sys.stderr)
            return 1
    if not password or not password.strip():
        print("error: password must not be empty", file=sys.stderr)
        return 1

    # Build ONLY the meta layer (never the heavy embed model): a local password
    # reset needs the same store the daemon uses, nothing else.
    try:
        built = {
            name: DRIVER_REGISTRIES["meta"].build(spec.driver, spec.params)
            for name, spec in config.layer_instances("meta").items()
        }
    except (ValueError, KeyError) as exc:
        print(f"error: storage stack failed to build: {exc}", file=sys.stderr)
        return 1
    meta = built.get("main") if "main" in built else next(iter(built.values()), None)
    if meta is None:
        print("error: meta layer resolved to no instance", file=sys.stderr)
        return 1
    try:
        IdentityService(meta).set_owner_password(password)
    except Exception as exc:
        from mnemoseed.identity.service import InvalidCredentialsError

        if isinstance(exc, InvalidCredentialsError):
            print("error: no owner account exists (run the setup wizard first)", file=sys.stderr)
            return 1
        print(f"error: password reset failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for instance in built.values():
            closer = getattr(instance, "close", None)
            if closer is not None:
                try:
                    asyncio.run(closer())
                except Exception:
                    pass
    print("owner password updated (re-login to obtain a new token)")
    return 0


def cmd_llm_status(args: argparse.Namespace) -> int:
    """REST-free dream route table + live connectivity (FR-6.9).

    Reports env-var NAMES only — a token value must never reach the terminal.
    """
    from mnemoseed.config import ConfigError, load_config
    from mnemoseed.llm.admin import LLMAdminError, LLMAdminService

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        body = LLMAdminService(config).routes()
    except LLMAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if config.source is not None:
        print(f"routes from: {config.source}")
    for role in body["roles"]:
        print(role["role"])
        probe = role["connectivity"]
        state = "ok" if probe["ok"] else "FAIL"
        suffix = f" ({probe['detail']})" if not probe["ok"] else ""
        for label, value in (
            ("driver", role["driver"] or "-"),
            ("model", role["model"] or "-"),
            ("base_url", role["base_url"] or "-"),
            ("api_key_env", role["api_key_env"] or "-"),
            ("connectivity", f"{state}{suffix}"),
        ):
            print(f"  {label}: {value}")
    print("drivers: " + ", ".join(info["name"] for info in body["drivers"]))
    return 0


def cmd_llm_set(args: argparse.Namespace) -> int:
    """Persist one dream role route (FR-6.9): the same validation + surgical
    TOML patch the console API runs, offline (no connectivity probe)."""
    from mnemoseed.config import ConfigError, load_config
    from mnemoseed.llm.admin import LLMAdminError, LLMAdminService

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        result = LLMAdminService(config).set_role(
            args.role,
            driver=args.driver,
            model=args.model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            provider=args.provider,
        )
    except LLMAdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{result['role']}: driver={result['driver']} model={result['model']}")
    print(f"persisted to: {result['persisted_to']}")
    return 0


def cmd_startup_enable(args: argparse.Namespace) -> int:
    """Register the daemon to launch at login/boot (issue #6)."""
    from mnemoseed.installer import startup

    for line in startup.enable():
        print(line)
    return 0


def cmd_startup_disable(args: argparse.Namespace) -> int:
    """Remove the login/boot registration (issue #6)."""
    from mnemoseed.installer import startup

    for line in startup.disable():
        print(line)
    return 0


def cmd_startup_status(args: argparse.Namespace) -> int:
    """Report registration + whether the daemon is running (issue #6)."""
    from mnemoseed.installer import startup

    st = startup.status()
    print(f"platform:    {st.platform}")
    print(f"target:      {st.target}")
    print(f"registered:  {st.registered}")
    print(f"pid:         {st.daemon_pid if st.daemon_pid is not None else 'none'}")
    print(f"pid alive:   {st.pid_alive}")
    print(f"/healthz:    {st.healthz_ok}")
    print(f"baseurl:     {st.baseurl}")
    print(f"running:     {st.running}")
    print(f"to change:   {st.change_command}")
    return 0


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
    p_install.add_argument(
        "--cursor-project",
        default=None,
        metavar="DIR",
        help="also write the project-level Cursor hooks + rules into DIR (FR-6.3b)",
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

    # Identity chain (issue #14): login persists a 0600 profile-token session
    # file under the config dir; logout revokes and deletes it; whoami reports
    # the stored identity against the running daemon; auth reset is the
    # local-only owner password reset (direct meta-store access).
    p_login = sub.add_parser("login", help="log in to the daemon and persist the profile token")
    p_login.add_argument("--baseurl", default=None, help="daemon base URL (default: config baseurl)")
    p_login.add_argument("--username", default=None, help="owner username (prompts when omitted)")
    p_login.add_argument(
        "--password",
        default=None,
        help="owner password (prompts when omitted; prefer the prompt over the argv flag)",
    )
    p_login.set_defaults(func=cmd_login)

    p_logout = sub.add_parser("logout", help="revoke the token and clear the stored session")
    p_logout.add_argument("--baseurl", default=None, help="daemon base URL (default: stored base_url)")
    p_logout.set_defaults(func=cmd_logout)

    p_whoami = sub.add_parser("whoami", help="show the logged-in identity and token validity")
    p_whoami.add_argument("--baseurl", default=None, help="daemon base URL (default: stored base_url)")
    p_whoami.set_defaults(func=cmd_whoami)

    p_auth = sub.add_parser("auth", help="identity management (owner account)")
    auth_sub = p_auth.add_subparsers(dest="auth_command", required=True)
    p_auth_reset = auth_sub.add_parser(
        "reset",
        help="reset the owner password (local-only, requires daemon data-dir access)",
    )
    p_auth_reset.add_argument(
        "--password",
        default=None,
        help="new owner password (prompts with confirmation when omitted)",
    )
    p_auth_reset.set_defaults(func=cmd_auth_reset)

    # Dream model routing (FR-6.9): 'status' shows each role's route + live
    # connectivity; 'set' persists one role through the same validation +
    # surgical TOML patch the console API runs. The api_key_env field names an
    # env var; the secret value itself is never stored or echoed (FR-2.14).
    p_llm = sub.add_parser(
        "llm",
        help="manage dream LLM routes (REST-free; writes config.toml)",
        description=(
            "Dream model routing (FR-6.9): 'llm status' shows each role's "
            "driver/model/endpoint/env-var NAME with a live connectivity probe; "
            "'llm set' persists one role route offline. Env-var NAMES only — "
            "a token value is never stored or printed."
        ),
    )
    llm_sub = p_llm.add_subparsers(dest="llm_command", required=True)
    p_llm_status = llm_sub.add_parser("status", help="show each role's route + connectivity")
    p_llm_status.set_defaults(func=cmd_llm_status)
    p_llm_set = llm_sub.add_parser(
        "set",
        help="update one role route (--driver/--model/--base-url/--api-key-env/--provider)",
    )
    p_llm_set.add_argument(
        "role",
        metavar="ROLE",
        help="dream role (deep_reflection, short_increment, local_track)",
    )
    p_llm_set.add_argument("--driver", default=None, help="driver name to switch to")
    p_llm_set.add_argument("--model", default=None, help="model name to switch to")
    p_llm_set.add_argument(
        "--base-url", default=None, help="endpoint override (omit to keep; empty to clear)"
    )
    p_llm_set.add_argument(
        "--api-key-env",
        default=None,
        help="env-var NAME whose value the router resolves (comma-separated fallback chain)",
    )
    p_llm_set.add_argument("--provider", default=None, help="oauth provider (codex|grok) for driver=oauth")
    p_llm_set.set_defaults(func=cmd_llm_set)

    # Daemon autostart (issue #6): enable registers the daemon to launch at
    # login/boot via the platform's native per-user surface (Windows Run key,
    # systemd user unit, launchd agent); disable removes that registration;
    # status reports registration + running state (pidfile + /healthz) and the
    # exact command that would change it.
    p_startup = sub.add_parser(
        "startup",
        help="manage daemon autostart (enable / disable / status)",
        description=(
            "Cross-platform daemon autostart: 'startup enable' registers the "
            "daemon to launch at login/boot (Windows HKCU Run key, systemd "
            "user unit, launchd LaunchAgent); 'startup disable' removes the "
            "registration; 'startup status' reports whether it is registered "
            "and whether the daemon is currently running."
        ),
    )
    startup_sub = p_startup.add_subparsers(dest="startup_command", required=True)
    p_startup_enable = startup_sub.add_parser("enable", help="register the daemon at login/boot")
    p_startup_enable.set_defaults(func=cmd_startup_enable)
    p_startup_disable = startup_sub.add_parser("disable", help="remove the login/boot registration")
    p_startup_disable.set_defaults(func=cmd_startup_disable)
    p_startup_status = startup_sub.add_parser(
        "status",
        help="show whether the daemon is registered and running",
        description=(
            "Reports the registration target, whether the daemon is registered, "
            "and whether it is running (pidfile liveness + /healthz probe), plus "
            "the exact `mnemoseed startup` command that would change it."
        ),
    )
    p_startup_status.set_defaults(func=cmd_startup_status)

    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
