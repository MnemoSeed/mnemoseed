"""Shared plumbing for the MnemoSeed Gemini CLI extension.

The hook scripts under this directory are thin Python entry points that talk to
a running MnemoSeed daemon over localhost HTTP. This module is stdlib-only by
design: hook subprocesses run inside arbitrary Gemini CLI environments and must
not depend on the mnemoseed package or a prepared virtualenv.

Fail-open contract (FR-6.3d): a hook always exits 0, never blocks the agent, and
spends zero LLM tokens. Every daemon call shares one wall-clock budget (default
2s) captured at hook start; a call that times out, errors, or returns an
unusable body is dropped and the hook emits nothing extra.

Gemini read paths (design/06 section 2.5): the SessionStart warm-up and the
BeforeAgent per-turn injection — the strongest per-turn read alongside Claude
Code — carry reads into the session; GEMINI.md provides standing guidance.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import socket
import ssl
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

HOST = "gemini_cli"

DEFAULT_BASE_URL = "http://localhost:7788"
DEFAULT_PROFILE_ID = "default"
DEFAULT_BUDGET_SECONDS = 2.0
DEFAULT_WARMUP_BUDGET_TOKENS = 800
DEFAULT_TURN_BUDGET_TOKENS = 200
DEFAULT_TOOL_OUTPUT_CAP = 100_000

ENV_BASE_URL = "MNEMOSEED_BASE_URL"
ENV_PROFILE_ID = "MNEMOSEED_PROFILE_ID"
ENV_BUDGET_SECONDS = "MNEMOSEED_HOOK_BUDGET_SECONDS"
ENV_TOOL_OUTPUT_CAP = "MNEMOSEED_TOOL_OUTPUT_CAP"
ENV_WARMUP_QUERY = "MNEMOSEED_SESSION_START_QUERY"

# Fallback SessionStart warm-up query. Profiles whose memory language differs
# set MNEMOSEED_SESSION_START_QUERY to their own phrase.
DEFAULT_WARMUP_QUERY = "recent preferences, decisions, constraints, and active projects"

# Memory entries flagged for the user's attention (design/03 markers).
_ATTENTION_FLAGS = ("conflict_pair", "pending_consolidation")

_CJK_START = 0x2E80


def _is_cjk(char: str) -> bool:
    return ord(char) >= _CJK_START


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate, no model involved: 1 token per aligned CJK
    character and 1 token per 4 non-CJK characters. Used for the budget gate so
    context emitted is always within the documented budget."""
    cjk = sum(1 for char in text if _is_cjk(char))
    other = len(text) - cjk
    return cjk + int(math.ceil(other / 4.0))


def truncate_to_tokens(text: str, budget_tokens: int) -> str:
    """Longest prefix whose estimated token count fits the budget.

    Characters beyond the cut are dropped and replaced with a single ellipsis
    marker when the marker itself still fits. Deterministic character-array
    slicing; never a model in the loop.
    """
    if estimate_tokens(text) <= budget_tokens:
        return text
    cjk = 0
    other = 0
    for index, char in enumerate(text):
        if _is_cjk(char):
            cjk += 1
        else:
            other += 1
        if cjk + int(math.ceil(other / 4.0)) > budget_tokens:
            cut = text[:index]
            if estimate_tokens(cut) > budget_tokens:  # single char already over
                return ""
            suffix = "" if estimate_tokens(cut + "…") > budget_tokens else "…"
            return cut + suffix
    return text


# ------------------------------------------------------------ stdio contract


def read_stdin_json() -> dict[str, Any]:
    """The hook input object, read from stdin.

    Malformed, truncated, or empty stdin must never crash a hook: each returns
    an empty object, and the hook then fails open.
    """
    raw = sys_stdin_read()
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sys_stdin_read() -> str:
    import sys

    return sys.stdin.read()


def print_json(output: dict[str, Any]) -> None:
    """Emit the hook JSON response on stdout (the contract's response channel).

    Written as raw UTF-8 bytes: the hook contract is JSON-over-UTF-8, while the
    interpreter's default stdout encoding on some hosts is a locale code page.
    """
    import sys

    data = json.dumps(output, ensure_ascii=False).encode("utf-8") + b"\n"
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(data)
        buffer.flush()
    else:
        sys.stdout.write(data.decode("utf-8"))
        sys.stdout.flush()


def run_hook(
    handler: Callable[[dict[str, Any], HookBudget], dict[str, Any]],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Run one hook handler under the fail-open contract.

    Any unexpected failure yields an empty response; the hook still exits 0 and
    the agent's turn is never blocked or delayed beyond the daemon budget.
    """
    try:
        return handler(data, HookBudget())
    except Exception:
        return {}


# ------------------------------------------------------------ environment


def resolve_base_url() -> str:
    return (os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")


def resolve_profile_id() -> str:
    raw = (os.environ.get(ENV_PROFILE_ID) or "").strip()
    return raw if raw else DEFAULT_PROFILE_ID


def budget_seconds() -> float:
    try:
        value = float(os.environ.get(ENV_BUDGET_SECONDS) or DEFAULT_BUDGET_SECONDS)
    except ValueError:
        value = DEFAULT_BUDGET_SECONDS
    return value if 0.1 <= value <= 30.0 else DEFAULT_BUDGET_SECONDS


def _tool_output_cap() -> int:
    try:
        value = int(os.environ.get(ENV_TOOL_OUTPUT_CAP) or DEFAULT_TOOL_OUTPUT_CAP)
    except ValueError:
        return DEFAULT_TOOL_OUTPUT_CAP
    return value if value > 0 else DEFAULT_TOOL_OUTPUT_CAP


class HookBudget:
    """One shared wall-clock deadline for a hook run; every daemon call spends
    it. ``remaining()`` is real monotonic time left before the deadline, never a
    socket idle timeout, so a drip-paced response cannot stretch a hook past its
    budget: connects are capped per address and body reads check ``remaining()``
    between blocks. A slow first call cannot silently let a later call hang."""

    #: Per-address connect cap: probing several resolved addresses (e.g. the
    #: IPv6 + IPv4 pair from ``localhost``) burns at most this much each, so a
    #: multi-address dial of a dead daemon stays well inside the deadline.
    CONNECT_TIMEOUT_CAP = 0.8

    def __init__(self, seconds: float | None = None) -> None:
        self._deadline = time.monotonic() + (budget_seconds() if seconds is None else seconds)

    def remaining(self) -> float:
        left = self._deadline - time.monotonic()
        return left if left > 0.0 else 0.0

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        """One fail-open JSON POST within the remaining budget.

        Returns the parsed JSON object, or None on timeout, transport error,
        non-2xx status, or unparsable body. Never raises.
        """
        if self.remaining() < 0.05 or not path.startswith("/"):
            return None
        parsed = urllib.parse.urlsplit(resolve_base_url())
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        secure = parsed.scheme == "https"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        sock = self._dial(host, port, secure)
        if sock is None:
            return None
        conn = (http.client.HTTPSConnection if secure else http.client.HTTPConnection)(host, port)
        conn.sock = sock
        headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        token = os.environ.get("MNEMOSEED_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            if not (200 <= response.status < 300):
                return None
            raw = self._read_body(response)
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass
        if raw is None:
            return None
        try:
            body_out = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return body_out if isinstance(body_out, dict) else None

    # ------------------------------------------------------------ transport

    def _dial(self, host: str, port: int, secure: bool) -> socket.socket | None:
        """Connect one socket within the deadline, capping each address attempt.

        IPv4 is probed before IPv6 on ``localhost``-style hosts so an unroutable
        ``::1`` cannot consume the whole budget on the first attempt.
        """
        try:
            addresses = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except OSError:
            return None
        addresses.sort(key=lambda entry: 0 if entry[0] == socket.AF_INET else 1)
        for family, socktype, proto, _canon, sockaddr in addresses:
            if self.remaining() < 0.05:
                break
            cap = min(self.remaining(), self.CONNECT_TIMEOUT_CAP)
            sock: socket.socket | None = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(cap)
                sock.connect(sockaddr)
                if secure:
                    sock.settimeout(cap)
                    context = ssl.create_default_context()
                    sock = context.wrap_socket(sock, server_hostname=host)
                sock.settimeout(self.remaining())
                return sock
            except OSError:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
        return None

    def _read_body(self, response: http.client.HTTPResponse) -> bytes | None:
        """Read the response body under the wall-clock deadline.

        Pacing through ``HTTPResponse.read(1)``: it honors Content-Length and
        chunked framing, yet returns the moment a drip tics one byte instead of
        blocking until a full block is assembled. ``remaining()`` is re-checked
        after every byte and each read's socket idle timeout is the remaining
        budget, so a stalled or endless body aborts the call instead of hanging
        the hook. For fast daemons the buffered reader serves each byte from its
        internal recv fill, so the loop costs little.
        """
        chunks = bytearray()
        while True:
            if self.remaining() < 0.05:
                return None
            sock = _response_socket(response)
            if sock is not None:
                try:
                    sock.settimeout(self.remaining())
                except OSError:
                    return None
            try:
                block = response.read(1)
            except Exception:
                return None
            if not block:
                break
            chunks += block
        return bytes(chunks)


def _response_socket(response: http.client.HTTPResponse) -> socket.socket | None:
    """Best-effort raw socket behind an HTTPResponse, for deadline-aware reads.

    ``response.fp`` is a buffered reader wrapping a SocketIO raw object; the
    real socket sits under its ``_sock``. Falls back to piping reads through the
    buffered object's own idle timeout when neither is reachable.
    """
    fp = response.fp
    raw = getattr(fp, "raw", None)
    for candidate in (raw, fp):
        if candidate is None:
            continue
        sock = getattr(candidate, "_sock", None)
        if isinstance(sock, socket.socket):
            return sock
    return None


# ------------------------------------------------------------ daemon calls


def post_ingest(
    budget: HookBudget,
    event: str,
    session_id: str,
    profile_id: str,
    content: dict[str, Any],
    *,
    ts: float | None = None,
) -> dict[str, Any] | None:
    """Capture one normalized hook event (design/06 2.5)."""
    payload = {
        "host": HOST,
        "event": event,
        "session_id": session_id,
        "profile_id": profile_id,
        "ts": time.time() if ts is None else ts,
        "content": content,
    }
    return budget.post("/ingest", payload)


def post_recall(
    budget: HookBudget,
    profile_id: str,
    query: str,
    *,
    budget_tokens: int,
    project: str | None = None,
) -> dict[str, Any] | None:
    """Recall memory through the daemon's budgeted retrieval surface."""
    payload: dict[str, Any] = {
        "profile_id": profile_id,
        "query": query,
        "budget": budget_tokens,
        "host": HOST,
    }
    if project:
        payload["project"] = project
    return budget.post("/memory/recall", payload)


# ------------------------------------------------------------ context shaping


def _project_name(cwd: str | None) -> str | None:
    if not cwd:
        return None
    name = os.path.basename(cwd.rstrip("/\\"))
    return name or None


def _tool_response_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
    else:
        text = str(value)
    cap = _tool_output_cap()
    return text if len(text) <= cap else text[:cap]


def build_context(recall: dict[str, Any] | None, *, budget_tokens: int) -> str:
    """Format a /memory/recall body into one cued context block.

    Emits nothing when recall is honest-empty, and always truncates to the
    stated token budget before returning.
    """
    if not isinstance(recall, dict):
        return ""
    memory = recall.get("memory")
    if not isinstance(memory, dict):
        return ""
    entries = memory.get("entries")
    if not isinstance(entries, list) or not entries:
        return ""
    lines: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        labels: list[str] = []
        kind = entry.get("kind")
        if isinstance(kind, str) and kind:
            labels.append(kind)
        flags = entry.get("flags")
        if isinstance(flags, list):
            for flag in flags:
                if flag in _ATTENTION_FLAGS:
                    labels.append(f"flag:{flag}")
        label = "memory" if not labels else " ".join(labels)
        lines.append(f"- [{label}] {' '.join(text.split())}")
    if not lines:
        return ""
    block = "<memory>\n" + "\n".join(lines) + "\n</memory>"
    return truncate_to_tokens(block, budget_tokens)


# ------------------------------------------------------------ Gemini input shape


def _params(data: dict[str, Any]) -> dict[str, Any]:
    """Gemini nests event arguments under ``params`` in some events; other
    fields stay at the top level. Reading both defensively keeps the scripts
    shape-tolerant."""
    value = data.get("params")
    return value if isinstance(value, dict) else {}


def _first_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value
    return ""


def _session_id(data: dict[str, Any]) -> str:
    return _first_str(data, "session_id", "sessionID") or _first_str(_params(data), "session_id", "sessionID")


def _cwd(data: dict[str, Any]) -> str:
    p = _params(data)
    return _first_str(data, "cwd", "workspace_path", "workspacePath") or _first_str(
        p, "directory", "cwd", "workspace_path", "workspacePath"
    )


def _tool_input(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data:
            return data[key]
    return None


def _inject(event: str, context: str) -> dict[str, Any]:
    """The Gemini hook response shape carrying injected context."""
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": context}}


# ------------------------------------------------------------ event handlers


def handle_session_start(data: dict[str, Any], budget: HookBudget) -> dict[str, Any]:
    """Gemini SessionStart warm-up (FR-6.3d): fetch context and hand it back as
    ``additionalContext`` (the first history turn)."""
    query = os.environ.get(ENV_WARMUP_QUERY) or DEFAULT_WARMUP_QUERY
    recall = post_recall(
        budget,
        resolve_profile_id(),
        query,
        budget_tokens=DEFAULT_WARMUP_BUDGET_TOKENS,
        project=_project_name(_cwd(data)),
    )
    context = build_context(recall, budget_tokens=DEFAULT_WARMUP_BUDGET_TOKENS)
    if not context:
        return {}
    return _inject("SessionStart", context)


def handle_before_agent(data: dict[str, Any], budget: HookBudget) -> dict[str, Any]:
    """BeforeAgent (FR-6.3d): capture the incoming agent request, then inject a
    per-turn recall (≤200 tokens, daemon answer within 2s). Both share the one
    hook deadline and fail open."""
    prompt = _first_str(_params(data), "prompt", "input") or _first_str(data, "prompt", "text")
    if not prompt.strip():
        return {}
    profile_id = resolve_profile_id()
    post_ingest(budget, "user_prompt", _session_id(data), profile_id, {"text": prompt})
    recall = post_recall(
        budget,
        profile_id,
        prompt,
        budget_tokens=DEFAULT_TURN_BUDGET_TOKENS,
        project=_project_name(_cwd(data)),
    )
    context = build_context(recall, budget_tokens=DEFAULT_TURN_BUDGET_TOKENS)
    if not context:
        return {}
    return _inject("BeforeAgent", context)


def handle_after_tool(data: dict[str, Any], budget: HookBudget) -> dict[str, Any]:
    """AfterTool (FR-6.3d): capture relevant tool results into the in-flight
    turn."""
    p = _params(data)
    tool_name = _first_str(p, "tool_name", "toolName") or _first_str(data, "tool_name", "toolName")
    if not tool_name:
        return {}
    input_value = _pick(p, "tool_input", "toolInput") or _pick(data, "tool_input", "toolInput")
    output_value = _pick(p, "tool_output", "toolOutput", "tool_response", "toolResponse") or _pick(
        data, "tool_output", "toolOutput", "tool_response", "toolResponse"
    )
    content = {
        "tool_name": tool_name,
        "input": _tool_input(input_value),
        "output": _tool_response_text(output_value),
    }
    post_ingest(budget, "tool_use", _session_id(data), resolve_profile_id(), content)
    return {}
