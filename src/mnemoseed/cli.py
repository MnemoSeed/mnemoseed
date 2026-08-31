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
import json
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


# ------------------------------------------------------------ CLI parity (design/07 5)


def _emit_json(payload: Any) -> int:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    return 0


def _client_error(exc: Exception) -> int:
    print(f"error: {exc}", file=sys.stderr)
    return 1


def cmd_console(args: argparse.Namespace) -> int:
    """FR-7.1: open the management console in the default browser."""
    import webbrowser

    from mnemoseed.rest_client import resolve_client

    client = resolve_client(args)
    url = f"{client.base_url}/console"
    print(f"opening console: {url}")
    if not webbrowser.open(url):
        print(f"no browser available; open {url} manually", file=sys.stderr)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """FR-7.2 dashboard: daemon health + one row per profile (table or JSON)."""
    from mnemoseed.rest_client import resolve_client

    client = resolve_client(args)
    try:
        body = client.get("/api/v1/status")
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    daemon = body.get("daemon", {})
    gate = daemon.get("gate", {})
    gate_state = "ok" if gate.get("ok") else "FAIL"
    print(
        f"daemon: mnemoseed {daemon.get('version', '?')} "
        f"(preset {daemon.get('preset', '?')}, gate {gate_state})"
    )
    for profile in body.get("profiles", []):
        print(f"profile: {profile.get('profile_id')}")
        dream = profile.get("dream", {})
        pool = profile.get("pool", {})
        counts = profile.get("counts", {})
        print(f"  dream state:       {dream.get('state')}")
        print(f"  dream pending:     {dream.get('pending_manual', 0)}")
        print(f"  pool balance:      {pool.get('balance')}")
        print(f"  chunks:            {counts.get('chunks')}")
        print(f"  nodes:             {counts.get('nodes')}")
        print(f"  needs_reconcile:   {counts.get('needs_reconcile')}")
        print(f"  pending_consolidation: {counts.get('pending_consolidation')}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """Command-line retrieval over /memory/recall (usable from scripts)."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
        body = client.post(
            "/memory/recall",
            {
                "profile_id": client.profile_id,
                "query": args.query,
                **({"top_k": args.top_k} if args.top_k is not None else {}),
            },
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    memory = body.get("memory", {})
    for entry in memory.get("entries", []):
        kind = entry.get("kind")
        score = entry.get("score")
        flags = ",".join(entry.get("flags", []))
        suffix = f" [{flags}]" if flags else ""
        print(f"[{kind}] ({score:.2f}) {entry.get('text')}{suffix}")
    coverage = memory.get("coverage", {})
    if coverage:
        print(
            f"coverage: vector_hits={coverage.get('vector_hits')} "
            f"graph_hits={coverage.get('graph_hits')} "
            f"profile_chunks={coverage.get('profile_chunks')}"
        )
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    """Command-line explicit memorization over /memory/remember."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
        body = client.post(
            "/memory/remember",
            {"profile_id": client.profile_id, "text": args.text},
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    print(f"remembered: {body.get('outcome')} (chunk {body.get('chunk_id')})")
    return 0


def cmd_dream(args: argparse.Namespace) -> int:
    """FR-2.8 manual-first dream: --once runs one cycle; status reads the trigger."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
        if getattr(args, "dream_command", None) == "status":
            body = client.get("/api/v1/dream/status", {"profile_id": client.profile_id or ""})
        else:
            body = client.post("/api/v1/dream/once", {"profile_id": client.profile_id})
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    if getattr(args, "dream_command", None) == "status":
        print(f"state: {body.get('state')}")
        print(f"pending_queue: {body.get('pending_queue')}")
        print(f"pending_manual: {body.get('pending_manual')}")
        last = body.get("last_event")
        if last:
            print(f"last event: {last.get('kind')} at {last.get('fired_at')}")
    else:
        print(f"launched: {body.get('launched')}")
        print(f"state: {body.get('state')}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Single-file self-contained export over /memory/export (design/06 5)."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
        body = client.post(
            "/memory/export",
            {
                "profile_id": client.profile_id,
                "offset": args.offset,
                "limit": args.limit,
            },
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    chunks = body.get("chunks", [])
    nodes = body.get("nodes", [])
    paging = body.get("paging", {})
    print(
        f"exported profile {body.get('profile_id')}: "
        f"chunks: {len(chunks)} (total {paging.get('chunk_total')}), "
        f"nodes: {len(nodes)} (total {paging.get('node_total')})"
    )
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    """Memory version diff for one node (design/02 rollback semantics view)."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
        body = client.post(
            "/memory/audit",
            {"profile_id": client.profile_id, "node_id": args.node_id},
        )
    except Exception as exc:
        return _client_error(exc)
    versions = body.get("versions", [])
    if len(versions) < 2:
        print(f"node {args.node_id}: {len(versions)} version(s); nothing to diff")
        return 0
    prev = versions[-2]
    curr = versions[-1]
    from difflib import unified_diff

    def line(version: dict[str, Any]) -> str:
        props = version.get("props") or {}
        subject = props.get("subject") or props.get("statement") or ""
        predicate = props.get("predicate") or ""
        obj = props.get("object") or ""
        return f"{subject} {predicate} {obj}".strip()

    diff_lines = list(
        unified_diff(
            [line(prev)],
            [line(curr)],
            fromfile=f"v{prev.get('version', '?')}",
            tofile=f"v{curr.get('version', '?')}",
            lineterm="",
        )
    )
    for diff_line in diff_lines:
        if diff_line.startswith(("---", "+++", "@@", "\\")):
            print(diff_line)
        elif diff_line.startswith("-"):
            print(f"- {diff_line[1:]}")
        elif diff_line.startswith("+"):
            print(f"+ {diff_line[1:]}")
        else:
            print(diff_line)
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Explicit deletion over /memory/forget_this (design/03 storage-layer erasure)."""
    from mnemoseed.rest_client import resolve_client

    body: dict[str, Any] = {"profile_id": None}
    if args.kind == "chunk":
        body["chunk_id"] = args.target
    elif args.kind == "entity":
        body["entity"] = args.target
    else:
        body["node_id"] = args.target
    try:
        client = resolve_client(args, require_session=True)
        body["profile_id"] = client.profile_id
        payload = client.post("/memory/forget_this", body)
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(payload)
    removed = payload.get("removed", {})
    print(f"forgotten: {len(removed.get('chunks', []))} chunk(s), {len(removed.get('nodes', []))} node(s)")
    return 0


def cmd_pin(args: argparse.Namespace) -> int:
    """FR-7.9 manual pin over /api/v1/pin (audited, actor=cli)."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
        payload = client.post(
            "/api/v1/pin",
            {
                "profile_id": client.profile_id,
                "node_id": args.node_id,
                "pinned": not args.off,
            },
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(payload)
    print(f"node {args.node_id}: {'pinned' if not args.off else 'unpinned'}")
    return 0


def cmd_weight(args: argparse.Namespace) -> int:
    """FR-7.9 manual decay-weight adjust over /api/v1/weights (audited, actor=cli)."""
    from mnemoseed.rest_client import resolve_client

    kind = "node" if args.kind == "node" else "chunk"
    try:
        client = resolve_client(args, require_session=True)
        payload = client.post(
            "/api/v1/weights",
            {
                "profile_id": client.profile_id,
                "kind": kind,
                "target_id": args.target,
                "decay_weight": args.value,
            },
        )
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(payload)
    print(
        f"{kind} {args.target}: decay_weight {payload.get('old_decay_weight')} -> "
        f"{payload.get('decay_weight')}"
    )
    return 0


def cmd_conflicts(args: argparse.Namespace) -> int:
    """FR-7.7 conflicts inbox + resolution over the console surface (actor=cli)."""
    from mnemoseed.rest_client import resolve_client

    try:
        client = resolve_client(args, require_session=True)
    except Exception as exc:
        return _client_error(exc)

    if args.conflicts_command == "list":
        try:
            body = client.get("/api/v1/conflicts", {"profile_id": client.profile_id})
        except Exception as exc:
            return _client_error(exc)
        if getattr(args, "json", False):
            return _emit_json(body)
        groups = body.get("groups", [])
        if not groups:
            print("no conflicts")
            return 0
        for group in groups:
            group_id = group.get("group_id")
            print(f"group {group_id}:")
            for side in group.get("sides", []):
                print(
                    f"  {side.get('node_id')} [{side.get('node_type')}] "
                    f"{side.get('statement')} (w={side.get('decay_weight')})"
                )
        return 0

    if args.conflicts_command == "resolve":
        payload: dict[str, Any] = {"profile_id": client.profile_id, "branch": args.branch}
        if args.node_id:
            payload["node_id"] = args.node_id
        if args.cues:
            payload["scope"] = args.cues
        try:
            result = client.post(f"/api/v1/conflicts/{args.conflict_id}/resolve", payload)
        except Exception as exc:
            return _client_error(exc)
        if getattr(args, "json", False):
            return _emit_json(result)
        print(
            f"resolved {args.conflict_id}: branch={args.branch} "
            f"written={result.get('written')} invalidated={result.get('invalidated')}"
        )
        return 0

    print(f"error: unknown conflicts subcommand {args.conflicts_command!r}", file=sys.stderr)
    return 1


def cmd_audit(args: argparse.Namespace) -> int:
    """Audit-log query: who/what/when across every write surface (design/07 5)."""
    from mnemoseed.rest_client import resolve_client

    params: dict[str, Any] = {"offset": args.offset, "limit": args.limit}
    if args.actor:
        params["actor"] = args.actor
    if args.action:
        params["action"] = args.action
    if args.since is not None:
        params["since"] = args.since
    if args.until is not None:
        params["until"] = args.until
    try:
        client = resolve_client(args)
        body = client.get("/api/v1/audit", params)
    except Exception as exc:
        return _client_error(exc)
    if args.json:
        return _emit_json(body)
    for item in body.get("items", []):
        print(
            f"{item.get('id')} {item.get('actor')} {item.get('action')} "
            f"{json.dumps(item.get('detail', {}), ensure_ascii=False, default=str)}"
        )
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    """FR-6.1c: bind a profile per agent (identity env in the host config)."""
    from mnemoseed.identity.session import load_session
    from mnemoseed.installer import HostConfigError, apply_registrations, plan_registrations

    session = load_session()
    if session is None:
        print("error: not logged in (run `mnemoseed login`)", file=sys.stderr)
        return 1
    try:
        plans = plan_registrations(profile_id=session.profile_id, token=session.token)
    except HostConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not plans:
        print("no hosts detected; nothing to link")
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
    print(f"linked: {report.written} host registration(s)")
    return 0


def cmd_unlink(args: argparse.Namespace) -> int:
    """FR-6.1c: unbind the mnemoseed profile from every detected host."""
    from mnemoseed.installer import HostConfigError
    from mnemoseed.installer.uninstall import deregister

    try:
        rolls = deregister()
    except HostConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rolls:
        print("no mnemoseed registrations; nothing to unlink")
        return 0
    for roll in rolls:
        suffix = f" ({roll.detail})" if roll.detail else ""
        print(f"  {roll.host}: {roll.outcome}{suffix}")
    return 0


# ------------------------------------------------------------ config ops (design/06 6)


def _parse_config_value(raw: str) -> Any:
    """Parse a CLI value: JSON scalars win, otherwise keep the raw string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def cmd_config(args: argparse.Namespace) -> int:
    """Versioned config read/write/rollback over daemon REST (loopback-only).

    ``config set`` is the sole verb with an offline escape: ``--force`` patches
    config.toml directly and prints "not audited (daemon down)" — the daemon
    REST is always preferred (PRD-07 FR-7.12).
    """
    from mnemoseed.rest_client import (
        DaemonUnavailableError,
        is_loopback,
        resolve_client,
        set_config_toml_offline,
    )

    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)

    def _require_loopback() -> int | None:
        if not is_loopback(client.base_url):
            print(
                f"error: config operations are loopback-only; refusing {client.base_url}",
                file=sys.stderr,
            )
            return 1
        return None

    def _read_current() -> dict[str, Any]:
        return client.get("/api/v1/config")

    if args.config_command == "get":
        refused = _require_loopback()
        if refused is not None:
            return refused
        try:
            body = _read_current()
        except (DaemonUnavailableError, Exception) as exc:
            return _client_error(exc)
        if getattr(args, "json", False):
            return _emit_json(body)
        config = body.get("config", {})
        if args.key:
            found: Any = config
            for segment in args.key.split("."):
                if not isinstance(found, dict) or segment not in found:
                    print(f"key {args.key!r} not present in the resolved config")
                    return 1
                found = found[segment]
            print(json.dumps(found, ensure_ascii=False, default=str))
            return 0
        print(json.dumps(config, indent=2, ensure_ascii=False, default=str))
        return 0

    if args.config_command == "versions":
        refused = _require_loopback()
        if refused is not None:
            return refused
        try:
            body = client.get("/api/v1/config/versions")
        except (DaemonUnavailableError, Exception) as exc:
            return _client_error(exc)
        if getattr(args, "json", False):
            return _emit_json(body)
        for version in body.get("versions", []):
            print(f"{version.get('version_id')} {version.get('key', '')} {version.get('updated_at', '')}")
        return 0

    value = _parse_config_value(args.value)
    if args.force:
        if not is_loopback(client.base_url):
            print(
                f"error: config operations are loopback-only; refusing {client.base_url}",
                file=sys.stderr,
            )
            return 1
        try:
            from mnemoseed.config import CONFIG_PATH

            set_config_toml_offline(CONFIG_PATH, args.key_path, value)
        except Exception as exc:
            return _client_error(exc)
        print(f"config set {args.key_path} = {value!r} (offline --force, not audited (daemon down))")
        return 0
    refused = _require_loopback()
    if refused is not None:
        return refused
    try:
        body = client.post("/api/v1/config/set", {"key_path": args.key_path, "value": value})
    except (DaemonUnavailableError, Exception) as exc:
        return _client_error(exc)
    if getattr(args, "json", False):
        return _emit_json(body)
    print(f"config set {args.key_path} = {value!r} (version {body.get('version_id')})")
    restart = body.get("restart_required")
    if restart:
        print("restart required to apply (storage driver / port / auth changes)")
    return 0


def cmd_config_rollback(args: argparse.Namespace) -> int:
    from mnemoseed.rest_client import DaemonUnavailableError, is_loopback, resolve_client

    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)
    if not is_loopback(client.base_url):
        print(
            f"error: config operations are loopback-only; refusing {client.base_url}",
            file=sys.stderr,
        )
        return 1
    try:
        body = client.post("/api/v1/config/rollback", {"version_id": args.version_id})
    except (DaemonUnavailableError, Exception) as exc:
        return _client_error(exc)
    if getattr(args, "json", False):
        return _emit_json(body)
    print(f"config rolled back to version {body.get('version_id')}")
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
    """Persist one dream role route (FR-6.9) over the daemon REST surface.

    W2 parity: the CLI writes dream routing through ``POST /api/v1/llm/routes/
    {role}`` — the exact endpoint the console wizard drives — so every change is
    validated, versioned, and audited (actor=cli) instead of touching
    config.toml directly (design/07 §5, design/06 §6). The endpoint runs the
    same validation (unknown role / driver, empty model, oauth provider rule)
    and answers 422 with the typed message the CLI prints.
    """
    from mnemoseed.rest_client import DaemonRestError, is_loopback, resolve_client

    try:
        client = resolve_client(args)
    except Exception as exc:
        return _client_error(exc)
    if not is_loopback(client.base_url):
        print(
            f"error: llm route changes are config operations; loopback-only, refusing {client.base_url}",
            file=sys.stderr,
        )
        return 1
    if args.role == "local_track":
        print(
            "error: llm role 'local_track' was removed — MnemoSeed now has two dream "
            "roles: deep_reflection and short_increment",
            file=sys.stderr,
        )
        return 1
    if args.api_key is not None:
        # T2-4: a key VALUE is written through the REST key endpoint — stored
        # under ~/.mnemoseed/secrets and referenced from config, never echoed.
        # No --force bypass exists for secrets: a down daemon is a hard error.
        if not args.api_key.strip():
            print("error: --api-key must not be empty", file=sys.stderr)
            return 1
        try:
            result = client.post("/api/v1/llm/key", {"role": args.role, "key": args.api_key})
        except DaemonRestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            return _client_error(exc)
        masked = result.get("masked_tail")
        tail = f" (masked tail {masked})" if masked else ""
        print(f"{result.get('role')}: api key stored locally{tail}")
        print("no restart needed: the new key is effective on the next dream run")
        return 0
    body: dict[str, Any] = {}
    for field, value in (
        ("driver", args.driver),
        ("model", args.model),
        ("base_url", args.base_url),
        ("api_key_env", args.api_key_env),
        ("provider", args.provider),
    ):
        if value is not None:
            body[field] = value

    # MUST-FIX 2: connectivity-test-before-persist. The route endpoint rejects
    # a persist that was not probed first, so the CLI probes the exact same
    # signature (only the fields this run changes; the server merges the rest
    # from the current route) and refuses to write a failed route.
    probe_body: dict[str, Any] = {"role": args.role}
    for field in ("driver", "model", "base_url", "api_key_env", "provider"):
        if body.get(field) is not None:
            probe_body[field] = body[field]
    try:
        probe = client.post("/api/v1/llm/test", probe_body)
    except DaemonRestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        return _client_error(exc)
    if not probe.get("ok"):
        detail = probe.get("detail") or {}
        message = detail.get("error") if isinstance(detail, dict) else detail
        print(
            f"error: connectivity test failed for role {args.role!r}" + (f": {message}" if message else ""),
            file=sys.stderr,
        )
        return 1

    try:
        result = client.post(f"/api/v1/llm/routes/{args.role}", body)
    except DaemonRestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        return _client_error(exc)
    print(f"{result['role']}: driver={result['driver']} model={result['model']}")
    print(f"persisted to: {result.get('persisted_to')}")
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


def cmd_onboard(args: argparse.Namespace) -> int:
    """FR-6.10 / FR-7.13: guided onboarding over the shared onboard backend.

    Step sequence: owner setup -> storage preset -> dream LLM wizard -> host
    link -> autostart -> doctor all-green. Every step is skippable + resumable
    (state persists under the config dir). The LLM wizard keeps
    connectivity-test-before-persist and skipping it yields a bootable
    capture-only daemon (stated in the wizard). Config operations are
    loopback-only.
    """
    from mnemoseed.onboard import OnboardService

    answers: dict[str, Any] = {"preset": "embedded"}
    if args.password:
        answers["password"] = args.password
    service = OnboardService(
        base_url=args.baseurl,
        username=args.username,
        skip=args.skip,
        yes=args.yes,
        llm_driver=args.llm_driver,
        llm_model=args.llm_model,
        answers=answers,
    )
    return service.run()


def _add_serve_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser], name: str, help_text: str
) -> argparse.ArgumentParser:
    parser = sub.add_parser(name, help=help_text)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7788)
    parser.set_defaults(func=cmd_up)
    return parser


def build_parser() -> argparse.ArgumentParser:
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
    # connectivity (read-only, offline); 'set' persists one role through the
    # same validation + surgical TOML patch the console API runs, over daemon
    # REST (design/07 5: the CLI never writes config.toml directly except the
    # `config set --force` escape). The api_key_env field names an env var; the
    # secret value itself is never stored or echoed (FR-2.14).
    p_llm = sub.add_parser(
        "llm",
        help="manage dream LLM routes (status offline; set via daemon REST)",
        description=(
            "Dream model routing (FR-6.9): 'llm status' shows each role's "
            "driver/model/endpoint/env-var NAME with a live connectivity probe; "
            "'llm set' persists one role route through the daemon REST surface "
            "(same endpoint the console wizard drives). Env-var NAMES only — "
            "a token value is never stored or printed."
        ),
    )
    llm_sub = p_llm.add_subparsers(dest="llm_command", required=True)
    p_llm_status = llm_sub.add_parser("status", help="show each role's route + connectivity")
    p_llm_status.set_defaults(func=cmd_llm_status)
    p_llm_set = llm_sub.add_parser(
        "set",
        help="update one role route (--driver/--model/--base-url/--api-key-env/--provider) "
        "or store a key (--api-key)",
    )
    p_llm_set.add_argument(
        "role",
        metavar="ROLE",
        help="dream role (deep_reflection, short_increment)",
    )
    p_llm_set.add_argument("--baseurl", default=None, help="daemon base URL (default: config baseurl)")
    p_llm_set.add_argument(
        "--driver",
        default=None,
        help="provider (or --provider codex|grok for a host login)",
    )
    p_llm_set.add_argument("--model", default=None, help="model name to switch to")
    p_llm_set.add_argument(
        "--base-url", default=None, help="endpoint override (omit to keep; empty to clear)"
    )
    p_llm_set.add_argument(
        "--api-key-env",
        default=None,
        help="env-var NAME whose value the router resolves (comma-separated fallback chain)",
    )
    p_llm_set.add_argument(
        "--api-key",
        default=None,
        help="paste an API key VALUE: stored under ~/.mnemoseed/secrets via the daemon "
        "(no restart needed; effective on the next dream run)",
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

    # CLI capability parity (design/07 5): every console action scriptable.
    # All verbs carry --baseurl (daemon endpoint) and --json (machine output);
    # state-changing verbs talk to the daemon REST surface, never the config
    # file, and every call is attributed with actor=cli.
    def _add_rest_flags(
        parser: argparse.ArgumentParser, *, with_json: bool = True, suppress_default: bool = False
    ) -> None:
        """--baseurl / --json for one verb.

        ``suppress_default`` is for subcommand parsers that share the namespace
        with their parent: an absent flag must not clobber a value the parent
        already parsed (``argparse.SUPPRESS`` leaves the attribute untouched).
        """
        default = argparse.SUPPRESS if suppress_default else None
        parser.add_argument("--baseurl", default=default, help="daemon base URL (default: config baseurl)")
        if with_json:
            parser.add_argument(
                "--json", action="store_true", default=default, help="emit JSON instead of a table"
            )

    p_console = sub.add_parser("console", help="open the management console in the browser (FR-7.1)")
    _add_rest_flags(p_console, with_json=False)
    p_console.set_defaults(func=cmd_console)

    p_status = sub.add_parser("status", help="daemon health + per-profile dashboard row (FR-7.2)")
    _add_rest_flags(p_status)
    p_status.set_defaults(func=cmd_status)

    p_recall = sub.add_parser(
        "recall", help='command-line retrieval: mnemoseed recall "<query>" (design/06 5)'
    )
    _add_rest_flags(p_recall)
    p_recall.add_argument("query", help="retrieval query")
    p_recall.add_argument("--top-k", type=int, default=None, help="override the retrieval top-k")
    p_recall.set_defaults(func=cmd_recall)

    p_remember = sub.add_parser(
        "remember", help='command-line explicit memorization: mnemoseed remember "<fact>"'
    )
    _add_rest_flags(p_remember)
    p_remember.add_argument("text", help="the fact/preference to remember")
    p_remember.set_defaults(func=cmd_remember)

    p_dream = sub.add_parser("dream", help="manual consolidation (FR-2.8): --once or status")
    _add_rest_flags(p_dream)
    p_dream.add_argument("--once", action="store_true", help="run exactly one manual dream cycle")
    p_dream.set_defaults(func=cmd_dream)
    dream_sub = p_dream.add_subparsers(dest="dream_command")
    p_dream_status = dream_sub.add_parser("status", help="read the trigger state / pending queue")
    # SUPPRESS default: the subparser shares the parent's namespace, so an
    # absent --baseurl/--json here must not clobber a value the parent already
    # parsed (`dream --baseurl X status`).
    _add_rest_flags(p_dream_status, suppress_default=True)
    p_dream_status.set_defaults(func=cmd_dream)

    p_export = sub.add_parser(
        "export",
        help="single-file self-contained memory export (copyable off-machine)",
        description=(
            "Export a profile's memory as a stable JSON dump including provenance "
            "and the index snapshot, copyable off-machine (design/06 5)."
        ),
    )
    _add_rest_flags(p_export)
    p_export.add_argument("--offset", type=int, default=0, help="paging offset (default: 0)")
    p_export.add_argument("--limit", type=int, default=50, help="paging limit (default: 50)")
    p_export.set_defaults(func=cmd_export)

    p_diff = sub.add_parser("diff", help="memory version diff for one node (design/02)")
    _add_rest_flags(p_diff)
    p_diff.add_argument("node_id", help="node whose latest two versions are diffed")
    p_diff.set_defaults(func=cmd_diff)

    p_forget = sub.add_parser(
        "forget",
        help="explicit deletion: mnemoseed forget <target> [--kind node|chunk|entity]",
    )
    _add_rest_flags(p_forget)
    p_forget.add_argument("target", help="the node id, chunk id, or entity to forget")
    p_forget.add_argument(
        "--kind",
        choices=("node", "chunk", "entity"),
        default="node",
        help="what the target names (default: node)",
    )
    p_forget.set_defaults(func=cmd_forget)

    p_pin = sub.add_parser(
        "pin",
        help="FR-7.9 manual pin: mnemoseed pin <node_id> [--off] (audited, actor=cli)",
    )
    _add_rest_flags(p_pin)
    p_pin.add_argument("node_id", help="the node to pin (or unpin with --off)")
    p_pin.add_argument("--off", action="store_true", help="unpin (clear never_decay)")
    p_pin.set_defaults(func=cmd_pin)

    p_weight = sub.add_parser(
        "weight",
        help="FR-7.9 manual decay-weight adjust: mnemoseed weight <target> <0..1>",
    )
    _add_rest_flags(p_weight)
    p_weight.add_argument("target", help="node or chunk id whose decay_weight is adjusted")
    p_weight.add_argument("value", type=float, help="the new decay_weight within [0.0, 1.0]")
    p_weight.add_argument(
        "--kind", choices=("node", "chunk"), default="node", help="what the target names (default: node)"
    )
    p_weight.set_defaults(func=cmd_weight)

    p_conflicts = sub.add_parser(
        "conflicts",
        help="FR-7.7 conflicts inbox + resolution (actor=cli): list | resolve",
    )
    _add_rest_flags(p_conflicts)
    conflicts_sub = p_conflicts.add_subparsers(dest="conflicts_command")

    p_conflicts_list = conflicts_sub.add_parser("list", help="show the conflicts inbox")
    _add_rest_flags(p_conflicts_list, suppress_default=True)
    p_conflicts_list.set_defaults(func=cmd_conflicts)

    p_conflicts_resolve = conflicts_sub.add_parser(
        "resolve",
        help="resolve one conflict group: mnemoseed conflicts resolve <id> --branch <b>",
    )
    _add_rest_flags(p_conflicts_resolve, suppress_default=True)
    p_conflicts_resolve.add_argument("conflict_id", help="the conflict group_id to resolve")
    p_conflicts_resolve.add_argument(
        "--branch",
        choices=("reinforce", "coexist", "invalidate", "pending"),
        required=True,
        help="resolution branch: reinforce | coexist | invalidate | pending",
    )
    p_conflicts_resolve.add_argument(
        "--node", dest="node_id", default=None, help="kept node (reinforce/invalidate)"
    )
    p_conflicts_resolve.add_argument("--cues", default=None, help="shared scope annotation (coexist branch)")
    p_conflicts_resolve.set_defaults(func=cmd_conflicts)

    p_audit = sub.add_parser(
        "audit",
        help="audit-log query — who/what/when across every write surface (design/07 5)",
        description=(
            "Query the daemon audit log with optional actor/action/time filters. "
            "Actor attribution: cli / console / mcp."
        ),
    )
    _add_rest_flags(p_audit)
    p_audit.add_argument("--actor", default=None, help="filter by actor (cli|console|mcp)")
    p_audit.add_argument("--action", default=None, help="filter by action (e.g. remember)")
    p_audit.add_argument("--since", type=float, default=None, help="only events at/after this epoch")
    p_audit.add_argument("--until", type=float, default=None, help="only events before this epoch")
    p_audit.add_argument("--offset", type=int, default=0, help="paging offset (default: 0)")
    p_audit.add_argument("--limit", type=int, default=50, help="paging limit (default: 50)")
    p_audit.set_defaults(func=cmd_audit)

    p_link = sub.add_parser(
        "link",
        help="bind a profile per agent: write profile_id + token into each host config (FR-6.1c)",
    )
    p_link.add_argument("--yes", action="store_true", help="approve every write without prompting")
    p_link.set_defaults(func=cmd_link)

    p_unlink = sub.add_parser(
        "unlink",
        help="unbind the mnemoseed profile from every detected host (FR-6.1c)",
    )
    p_unlink.set_defaults(func=cmd_unlink)

    # Versioned config over daemon REST (design/06 6): loopback-only, audited
    # with actor=cli; `config set --force` is the offline escape that patches
    # config.toml directly and prints "not audited (daemon down)".
    p_config = sub.add_parser(
        "config",
        help="versioned config read/write/rollback over daemon REST (loopback-only)",
        description=(
            "Config operations go through the daemon REST API — the same "
            "backend the console Settings page uses. Loopback-only: against a "
            "non-loopback baseurl they fail with a clear error. `set --force` "
            "is the offline escape (patches config.toml directly, prints "
            "'not audited (daemon down)')."
        ),
    )
    _add_rest_flags(p_config)
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_config_get = config_sub.add_parser("get", help="show the resolved config (or one dotted key)")
    p_config_get.add_argument(
        "--baseurl", default=argparse.SUPPRESS, help="daemon base URL (default: config baseurl)"
    )
    p_config_get.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON instead of a table"
    )
    p_config_get.add_argument("key", nargs="?", default=None, help="dotted key to show (optional)")
    p_config_get.set_defaults(func=cmd_config)
    p_config_set = config_sub.add_parser(
        "set", help="update one dotted key (JSON scalars parsed, else kept as a string)"
    )
    p_config_set.add_argument(
        "--baseurl", default=argparse.SUPPRESS, help="daemon base URL (default: config baseurl)"
    )
    p_config_set.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON instead of a table"
    )
    p_config_set.add_argument(
        "--force",
        action="store_true",
        help="offline escape: patch config.toml directly even when the daemon is down",
    )
    p_config_set.add_argument("key_path", metavar="KEY_PATH", help="dotted key, e.g. scoring.w1")
    p_config_set.add_argument("value", help="new value (JSON scalar or literal string)")
    p_config_set.set_defaults(func=cmd_config)
    p_config_versions = config_sub.add_parser("versions", help="list the versioned config history")
    p_config_versions.add_argument(
        "--baseurl", default=argparse.SUPPRESS, help="daemon base URL (default: config baseurl)"
    )
    p_config_versions.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON instead of a table"
    )
    p_config_versions.set_defaults(func=cmd_config)
    p_config_rollback = config_sub.add_parser("rollback", help="revert the config to a prior version")
    p_config_rollback.add_argument(
        "--baseurl", default=argparse.SUPPRESS, help="daemon base URL (default: config baseurl)"
    )
    p_config_rollback.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON instead of a table"
    )
    p_config_rollback.add_argument("version_id", type=int, metavar="VERSION_ID")
    p_config_rollback.set_defaults(func=cmd_config_rollback)

    # Guided onboarding (FR-6.10 / FR-7.13): the CLI frontend over the shared
    # onboard backend. Every step is skippable + resumable; the LLM wizard keeps
    # connectivity-test-before-persist; config operations are loopback-only.
    p_onboard = sub.add_parser(
        "onboard",
        help="guided onboarding: owner -> storage -> LLM wizard -> host link -> autostart -> doctor",
        description=(
            "A guided, step-by-step aggregate over the existing primitives — "
            "owner account setup, storage preset, dream LLM wizard "
            "(connectivity-test-before-persist), host link (backup + diff + "
            "confirmation), autostart, and a closing doctor self-check. Every "
            "step is skippable + resumable (state persists under the config "
            "dir); skipping the LLM step yields a bootable capture-only daemon."
        ),
    )
    p_onboard.add_argument("--baseurl", default=None, help="daemon base URL (default: config baseurl)")
    p_onboard.add_argument("--username", default=None, help="owner username (prompts when omitted)")
    p_onboard.add_argument(
        "--password",
        default=None,
        help="owner password (prompts when omitted; never echoed)",
    )
    p_onboard.add_argument(
        "--skip",
        nargs="*",
        default=[],
        choices=["setup", "storage", "llm", "link", "autostart", "doctor"],
        help="steps to skip this run (e.g. --skip llm -> capture-only daemon)",
    )
    p_onboard.add_argument("--yes", action="store_true", help="approve defaults without prompting")
    p_onboard.add_argument(
        "--llm-driver", default=None, help="dream LLM driver (wizard prompts when omitted)"
    )
    p_onboard.add_argument("--llm-model", default=None, help="dream LLM model (wizard prompts when omitted)")
    p_onboard.set_defaults(func=cmd_onboard)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
