"""The shared onboard backend service (PRD-07 FR-7.13, PRD-06 FR-6.10).

The service walks the onboarding steps in order. Every step is a thin driver
over an existing primitive — no parallel logic — and each is skippable and
resumable: per-step state persists under the config dir (``onboard.json``), so
a repeated ``onboard`` run skips completed steps and resumes where the previous
run stopped. Config operations are loopback-only (design/06 6).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mnemoseed.rest_client import (
    DaemonRestError,
    is_loopback,
    resolve_client,
)

STATE_FILE_NAME = "onboard.json"

#: The provider picker of the dream-LLM wizard (models-routing-ux.md §11.3). Only the
#: Fireworks default model id was verified against the live catalog — every
#: other provider defaults to free text, never an unverified suggestion (D9).
_LLM_PROVIDERS: dict[str, dict[str, str]] = {
    "1": {
        "driver": "openai_compatible",
        "provider": "fireworks",
        "name": "Fireworks",
        "base_url": "https://api.fireworks.ai/inference/v1",
        "key_env": "FIREWORKS_API_KEY",
        "key_url": "https://app.fireworks.ai/settings/users/api-keys",
        "key_prompt": "api key env var [FIREWORKS_API_KEY]:",
        "model_prompt": "model [accounts/fireworks/models/kimi-k3]:",
    },
    "2": {
        "driver": "openai_compatible",
        "provider": "openrouter",
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
        "key_url": "https://openrouter.ai/settings/keys",
        "key_prompt": "api key env var [OPENROUTER_API_KEY]:",
        "model_prompt": "model:",
    },
    "3": {
        "driver": "anthropic",
        "provider": "anthropic",
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com",
        "key_env": "ANTHROPIC_API_KEY",
        "key_url": "https://platform.claude.com/settings/keys",
        "key_prompt": "api key env var [ANTHROPIC_API_KEY]:",
        "model_prompt": "model:",
    },
    "4": {
        "driver": "ollama",
        "provider": "ollama",
        "name": "Ollama",
        "base_url": "http://localhost:11434",
        "key_env": "",
        "key_url": "",
        "key_prompt": "",
        "model_prompt": "model [llama3.1:8b]:",
    },
    "5": {
        "driver": "openai_compatible",
        "provider": "other",
        "name": "the endpoint",
        "base_url": "",
        "key_env": "MNEMOSEED_DEEP_REFLECTION_API_KEY",
        "key_url": "",
        "key_prompt": "api key env var [MNEMOSEED_DEEP_REFLECTION_API_KEY]:",
        "model_prompt": "model:",
    },
}


def _state_path(path: Path | None) -> Path:
    """Resolve the state file against the live config dir (import-time-safe)."""
    if path is not None:
        return path

    from mnemoseed.config import CONFIG_DIR

    return CONFIG_DIR / STATE_FILE_NAME


#: Step ids in run order (FR-6.10: owner -> storage -> llm -> link -> autostart -> doctor).
STEPS = ("setup", "storage", "llm", "link", "autostart", "doctor")

_DONE = "done"
_SKIPPED = "skipped"

#: How long one step may run before the wizard moves on (FR-6.10 rule 5: each
#: step is timeboxed so a slow step cannot blow the TTFM < 3 min budget).
STEP_TIMEOUT_SECONDS = 45.0


@dataclass(frozen=True)
class OnboardState:
    """Per-step outcome: ``done`` or ``skipped`` for each step."""

    steps: dict[str, str] = field(default_factory=dict)

    @property
    def remaining(self) -> tuple[str, ...]:
        return tuple(step for step in STEPS if self.steps.get(step) not in (_DONE, _SKIPPED))


def load_state(path: Path | None = None) -> OnboardState:
    """Read the persisted onboard state; corrupt/missing state starts fresh."""
    target = _state_path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return OnboardState()
    steps = raw.get("steps", {}) if isinstance(raw, dict) else {}
    if not isinstance(steps, dict):
        steps = {}
    return OnboardState(steps={str(key): str(value) for key, value in steps.items()})


def save_state(state: OnboardState, path: Path | None = None) -> Path:
    target = _state_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"steps": state.steps}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return target


class OnboardService:
    """Guided aggregate over the onboarding primitives (FR-6.10).

    ``answers`` drives interactive prompts (username/password, LLM model
    choice); callers that already have the answers pass them so the wizard can
    run scripted (``--yes``).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        skip: tuple[str, ...] = (),
        yes: bool = False,
        llm_driver: str | None = None,
        llm_model: str | None = None,
        answers: dict[str, Any] | None = None,
        out: Any = None,
    ) -> None:
        self.base_url = base_url
        self.username = username
        self.password = password
        self.skip = set(skip)
        self.yes = yes
        self.llm_driver = llm_driver
        self.llm_model = llm_model
        self.answers = answers or {}
        self.out = out
        self.state = load_state()

    # ------------------------------------------------------------ io helpers

    def _print(self, line: str = "") -> None:
        target = self.out if self.out is not None else print
        target(line)

    def _error(self, line: str) -> None:
        if self.out is not None:
            self.out(f"error: {line}")
            return
        import sys

        print(f"error: {line}", file=sys.stderr)

    def _answer(self, key: str, prompt: str, *, default: str | None = None) -> str | None:
        if key in self.answers:
            value = self.answers[key]
            if value == "" and default is not None:
                return default
            return value if value not in ("", None) else default
        if self.yes and default is not None:
            return default
        value = input(f"{prompt}: ").strip()
        if not value and default is not None:
            return default
        return value or None

    def _confirm(self, prompt: str, *, default_no: bool = False) -> bool:
        if self.yes:
            return True
        if default_no:
            return False
        answer = input(f"{prompt} [y/N] ").strip().lower()
        return answer in ("y", "yes")

    # ------------------------------------------------------------ steps

    def _step_setup(self, client: Any) -> tuple[bool, str]:
        """① owner account setup through the exact-once /api/v1/setup endpoint."""
        status = client.get("/api/v1/setup/status")
        if not status.get("setup_required", True):
            return self._login_after_setup(client, "owner already exists")
        username = self._answer("username", "choose an owner username", default="owner")
        password = self._answer("password", "choose an owner password")
        if not username or not password:
            return False, "username and password are required"
        body = client.post("/api/v1/setup", {"username": username, "password": password})
        self.username = username
        self.password = password
        return self._login_after_setup(client, f"owner account created ({body.get('profile_id', 'default')})")

    def _login_after_setup(self, client: Any, message: str) -> tuple[bool, str]:
        """Login and persist the profile token so later steps (link, doctor's
        round-trip) can attach the bearer identity (design/06: login -> token)."""
        from mnemoseed.identity.session import AuthSession, save_session

        username = self.username or self.answers.get("username")
        password = self.password or self.answers.get("password")
        if not username or not password:
            return True, message + " (no session: username/password not provided)"
        try:
            body = client.post("/api/v1/auth/login", {"username": username, "password": password})
        except DaemonRestError as exc:
            return True, f"{message} (session login failed: {exc})"
        session = AuthSession(
            base_url=client.base_url,
            username=username,
            profile_id=str(body.get("profile_id", "default")),
            token=str(body["token"]),
            expires_at=float(body["expires_at"]) if body.get("expires_at") is not None else None,
        )
        save_session(session)
        return True, message

    def _step_storage(self, client: Any) -> tuple[bool, str]:
        """② storage preset choice: embedded by default, zero Docker."""
        if not is_loopback(client.base_url):
            return False, f"config operations are loopback-only; refusing {client.base_url}"
        preset = self.answers.get("preset", "embedded")
        if preset not in ("embedded", "compose"):
            preset = "embedded"
        current = client.get("/api/v1/config")
        resolved = current.get("config", {})
        if resolved.get("preset") == preset:
            return True, f"storage preset already {preset}"
        client.post("/api/v1/config/set", {"key_path": "preset", "value": preset})
        return True, f"storage preset set to {preset}"

    def _step_llm(self, client: Any) -> tuple[bool, str]:
        """③ dream LLM wizard: key-paste first, then connectivity-test-before-
        persist, skippable (T2-4: a pasted key is stored through the REST key
        endpoint and the role references it — no restart needed; the env-var
        path stays as the advanced alternative)."""
        if not is_loopback(client.base_url):
            return False, f"config operations are loopback-only; refusing {client.base_url}"
        driver = self.llm_driver or self.answers.get("llm_driver")
        model = self.llm_model or self.answers.get("llm_model")
        base_url = ""
        key_env = ""
        api_key = ""
        meta: dict[str, str] | None = None
        if not driver or not model:
            if self.yes:
                self._print(
                    "  skipping the LLM wizard: the daemon stays capture-only "
                    "(dreaming disabled until a model is configured)"
                )
                return True, "llm skipped (capture-only daemon)"
            driver, model, base_url, key_env, meta, api_key = self._llm_interactive()
            if not driver or not model:
                self._print(
                    "  skipping the LLM wizard: the daemon stays capture-only "
                    "(dreaming disabled until a model is configured)"
                )
                return True, "llm skipped (capture-only daemon)"

        probe: dict[str, Any] = {"role": "deep_reflection", "driver": driver, "model": model}
        persist: dict[str, Any] = {"driver": driver, "model": model}
        stored_key = False
        if meta is not None:
            if api_key:
                try:
                    client.post("/api/v1/llm/key", {"role": "deep_reflection", "key": api_key})
                except DaemonRestError as exc:
                    self._error(f"storing the api key failed: {exc}")
                    return False, "llm api key rejected"
                stored_key = True
            if base_url:
                probe["base_url"] = base_url
                persist["base_url"] = base_url
            key_source = "secrets:mnemoseed/dream/deep_reflection" if stored_key else key_env
            if key_source:
                probe["api_key_env"] = key_source
                persist["api_key_env"] = key_source
            probe["provider"] = meta["provider"]
            persist["provider"] = meta["provider"]
            self._print(f"  testing connection to {meta['name']}…")
        try:
            report = client.post("/api/v1/llm/test", probe)
        except DaemonRestError as exc:
            self._error(f"llm probe rejected: {exc}")
            return False, "llm probe rejected"
        if not report.get("ok"):
            detail = report.get("detail") or {}
            error_text = str(detail.get("error") or "") if isinstance(detail, dict) else str(detail)
            self._print(self._llm_probe_message(driver, meta, base_url, key_env, error_text))
            return True, "llm skipped (connectivity test failed)"

        share = False
        if meta is not None:
            self._print("  connected — key works. saving…")
        if "llm_share" in self.answers:
            share = bool(self.answers["llm_share"])
        elif meta is not None and not self.yes:
            answer = input("  also apply to short_increment? [y/N]: ").strip().lower()
            share = answer in ("y", "yes")
        client.post("/api/v1/llm/routes/deep_reflection", persist)
        if share:
            if stored_key:
                # the shared role gets its own stored key + reference, and the
                # probe arms its own persist signature (MUST-FIX 2).
                client.post("/api/v1/llm/key", {"role": "short_increment", "key": api_key})
                shared_probe = dict(probe)
                shared_probe["role"] = "short_increment"
                shared_probe["api_key_env"] = "secrets:mnemoseed/dream/short_increment"
                shared_persist = dict(persist)
                shared_persist["api_key_env"] = "secrets:mnemoseed/dream/short_increment"
                client.post("/api/v1/llm/test", shared_probe)
                client.post("/api/v1/llm/routes/short_increment", shared_persist)
            else:
                client.post("/api/v1/llm/routes/short_increment", persist)
        return True, f"dream model configured ({driver}/{model})"

    def _llm_interactive(self) -> tuple[str | None, str | None, str, str, dict[str, str] | None, str]:
        """The provider-first picker (models-routing-ux.md §11.3), run only when the
        caller did not pass ``--llm-driver`` / ``--llm-model``.

        Returns (driver, model, base_url, key_env, meta, api_key): the pasted
        key VALUE is returned separately so the caller stores it through the
        REST key endpoint — it is never printed, persisted, or sent over the
        probe/persist wire.
        """
        self._print(
            "  Pick the model that distills your sessions into long-term memory. "
            "One model gets you started; change any role later with "
            "'mnemoseed llm set'."
        )
        self._print("  1) Fireworks (recommended)")
        self._print("  2) OpenRouter")
        self._print("  3) Anthropic")
        self._print("  4) Ollama on this computer")
        self._print("  5) other OpenAI-compatible")
        choice = str(self.answers.get("llm_provider") or "").strip()
        if not choice:
            choice = input("provider [1]: ").strip() or "1"
        meta = _LLM_PROVIDERS.get(choice)
        if meta is None:
            self._error("That connection type isn't built in — go back and pick a provider.")
            return None, None, "", "", None, ""
        if meta["provider"] == "ollama":
            self._print(
                "  Ollama chosen for this role — lower synthesis quality than cloud "
                "models; you accept this for privacy or cost."
            )
        key_env = meta["key_env"]
        api_key = ""
        if key_env:
            if meta["key_url"]:
                self._print(f"  Create a key at {meta['key_url']}")
            self._print(
                "  Paste your API key now — stored locally under ~/.mnemoseed/secrets, "
                "never shown again (no restart needed; the next dream run picks it up)."
            )
            self._print(f"  Advanced: leave it empty to use the {key_env} env var instead.")
            value = str(self.answers.get("llm_api_key") or "").strip()
            if not value:
                value = input("api key (empty = env var): ").strip()
            api_key = value
        base_url = meta["base_url"]
        if meta["provider"] == "other":
            endpoint = str(self.answers.get("llm_endpoint") or "").strip()
            if not endpoint:
                endpoint = input("endpoint: ").strip()
            if endpoint:
                base_url = endpoint
        model = str(self.answers.get("llm_model") or "").strip()
        if not model:
            model = input(meta["model_prompt"] + " ").strip()
        return meta["driver"], model or None, base_url, key_env, meta, api_key

    def _llm_probe_message(
        self,
        driver: str,
        meta: dict[str, str] | None,
        base_url: str,
        key_env: str,
        error_text: str,
    ) -> str:
        """The §11.3 plain-language probe failure mapping. The fallback always
        carries the raw driver error so a typed failure is never hidden."""
        if meta is None:
            return f"  connectivity test failed: {error_text}"
        name = meta["name"]
        if re.search(r"401|403", error_text):
            return (
                f"  error: {name} rejected the key — re-paste it (or set {key_env} "
                "as an env var) and re-run onboard (it resumes here); no restart "
                "needed, the next dream run picks the key up."
            )
        if meta["provider"] == "ollama":
            return (
                f"  error: can't reach Ollama at {base_url} — is it running? Install "
                "from ollama.com and pull a model (ollama pull llama3.1:8b)."
            )
        return f"  connectivity test failed: {error_text}"

    def _step_link(self, client: Any) -> tuple[bool, str]:
        """④ host link: installer plan + apply with per-item confirmation."""
        from mnemoseed.identity.session import load_session
        from mnemoseed.installer import (
            HostConfigError,
            apply_registrations,
            plan_registrations,
        )

        session = load_session()
        if session is None:
            return False, "no session: run the owner setup step first or `mnemoseed login`"
        try:
            plans = plan_registrations(profile_id=session.profile_id, token=session.token)
        except HostConfigError as exc:
            return False, str(exc)
        if not plans:
            return True, "no hosts detected; nothing to link"
        for plan in plans:
            state_char = "no-op (already registered)" if not plan.changed else "write"
            self._print(f"  {plan.describe()} [{state_char}]")
            if plan.diff:
                self._print(plan.diff.rstrip("\n"))
        try:
            report = apply_registrations(plans, approve=self._approve(plans))
        except HostConfigError as exc:
            return False, str(exc)
        return True, f"linked {report.written} host registration(s)"

    def _approve(self, plans: list[Any]) -> Any:
        def confirm(plan: Any) -> bool:
            if not plan.changed or self.yes:
                return True
            answer = input(f"apply {plan.describe()}? [y/N] ").strip().lower()
            return answer in ("y", "yes")

        return confirm

    def _step_autostart(self, client: Any) -> tuple[bool, str]:
        """⑤ register the daemon to start at login/boot."""
        from mnemoseed.installer import startup

        lines = startup.enable()
        for line in lines:
            self._print(f"  {line}")
        return True, "autostart registered"

    def _step_doctor(self, client: Any) -> tuple[bool, str]:
        """⑥ doctor all-green: the closing self-check."""
        from mnemoseed.config import load_config
        from mnemoseed.installer.doctor import run_doctor

        config = load_config()
        report = run_doctor(config)
        for check in report.checks:
            state_char = "ok" if check.ok else "FAIL"
            self._print(f"  [{state_char:>4}] {check.name}: {check.detail}")
            if not check.ok and check.fix:
                self._print(f"        fix: {check.fix}")
        if report.failed:
            fixes = "; ".join(check.fix for check in report.failed if check.fix)
            return False, f"{len(report.failed)} check(s) failed" + (f" — {fixes}" if fixes else "")
        return True, "doctor all checks passed"

    # ------------------------------------------------------------ run

    def run(self) -> int:
        """Walk the onboarding steps and return the process exit code."""
        try:
            client = resolve_client(self._namespace())
        except Exception as exc:
            return self._fail(exc)
        if not is_loopback(client.base_url) and not self.answers.get("allow_remote"):
            return self._fail(f"config operations are loopback-only; refusing {client.base_url}")

        steps = {name: getattr(self, f"_step_{name}") for name in STEPS}
        started = time.monotonic()
        failed_step: str | None = None
        for name in STEPS:
            if name in self.skip:
                self.state.steps[name] = _SKIPPED
                save_state(self.state)
                self._print(f"[{name}] skipped")
                if name == "llm":
                    self._print(
                        "  skipping the LLM wizard: the daemon stays capture-only "
                        "(dreaming disabled until a model is configured)"
                    )
                continue
            if self.state.steps.get(name) in (_DONE, _SKIPPED):
                status = "skipped" if self.state.steps.get(name) == _SKIPPED else "done"
                self._print(f"[{name}] already {status}")
                continue
            self._print(f"[{name}]")
            timeout = self._timeboxed(steps[name], client)
            ok, message = timeout
            if not ok:
                self._print(f"  FAIL: {message}")
                self.state.steps[name] = _SKIPPED
                save_state(self.state)
                failed_step = name
                break
            self.state.steps[name] = _DONE
            save_state(self.state)
            self._print(f"  ✓ {message}")
        elapsed = time.monotonic() - started
        self._print(f"onboard {'failed' if failed_step else 'complete'} in {elapsed:.1f}s")
        if failed_step:
            return 1
        self._print("Done. The next time you're in a meeting, it'll already be there.")
        return 0

    def _timeboxed(self, step: Any, client: Any) -> tuple[bool, str]:
        start = time.monotonic()
        ok, message = step(client)
        if time.monotonic() - start > STEP_TIMEOUT_SECONDS:
            self._print("  step exceeded its time budget; resuming later continues from here")
        return ok, message

    def _fail(self, message: Any) -> int:
        self._error(str(message))
        return 1

    def _namespace(self) -> Any:
        """A minimal args stand-in for resolve_client (--baseurl)."""

        class _Namespace:
            baseurl: str | None

        namespace = _Namespace()
        namespace.baseurl = self.base_url
        return namespace
