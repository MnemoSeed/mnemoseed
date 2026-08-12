"""User-level Codex CLI adapter artifacts (FR-6.3c).

The T2 installer registers Codex by writing the single ``mnemoseed`` MCP entry
(``~/.codex/config.toml``). On top of that, whenever Codex is among the detected
hosts, ``mnemoseed install`` plans the user-level Codex adapter files as separate
approvable items:

* one item for the hooks artifact -- ``~/.codex/hooks.json`` (merged into any
  existing file, preserving unrelated events) plus the hook scripts under
  ``~/.codex/mnemoseed/``;
* one item for the AGENTS.md guidance fragment -- appended to any existing
  ``~/.codex/AGENTS.md``, never overwriting user content.

Both items flow through the same plan -> backup -> diff -> per-item confirm ->
apply path as the host registrations, so uninstall can roll them back exactly:
hooks.json restores byte-identical from its backup, and the AGENTS.md fragment
is stripped (never the user's own file). Template texts live in
``adapters/codex/templates`` and are located through :func:`adapter_templates_dir`
(resolved by :mod:`mnemoseed.installer.templating`: wheel-shipped package data
first, the dev-checkout repo tree as fallback).
"""

from __future__ import annotations

import json
from difflib import unified_diff
from pathlib import Path
from typing import Any

from mnemoseed.installer.hosts import HostConfigError, json_file_text, load_host_json
from mnemoseed.installer.registration import RegistrationPlan
from mnemoseed.installer.templating import adapter_templates_dir as _shared_adapter_templates_dir

# Codex runs user-managed hooks only after a `/hooks` trust review by hash
# (FR-6.3c / AC-8). The installer prints this whenever it plans the hooks.


def trust_guidance_lines() -> tuple[str, ...]:
    return (
        "Codex hooks require a one-time trust review before Codex will execute them:",
        "  run `codex`, issue `/hooks`, and approve the MnemoSeed hooks by their hash.",
        "Without that review the hooks silently never execute.",
    )


HOOKS_HOST_KEY = "codex-hooks"
AGENTS_HOST_KEY = "codex-agents"

HOOKS_JSON_REL = Path(".codex") / "hooks.json"
AGENTS_REL = Path(".codex") / "AGENTS.md"
HOOK_SCRIPT_RELS = (
    Path(".codex") / "mnemoseed" / "py.sh",
    Path(".codex") / "mnemoseed" / "mnemoseed_hook_client.py",
    Path(".codex") / "mnemoseed" / "session_start.py",
    Path(".codex") / "mnemoseed" / "user_prompt_submit.py",
    Path(".codex") / "mnemoseed" / "session_end.py",
)


def adapter_templates_dir() -> Path:
    """The directory holding the Codex adapter templates.

    Wheel-shipped package data wins when present (``uv tool install .``);
    otherwise the dev-checkout repo tree (``adapters/codex/templates``) is the
    fallback, and ``MNEMOSEED_ADAPTER_TEMPLATES`` overrides both for tests.
    """
    return _shared_adapter_templates_dir("codex")


def _template_source(root: Path, rel: Path) -> Path:
    if rel == HOOKS_JSON_REL:
        return root / "hooks.json"
    if rel == AGENTS_REL:
        return root / "agents.md"
    return root / "hooks" / rel.name


def artifact_texts(templates: Path) -> dict[Path, str]:
    """Every artifact file's exact install text, keyed by home-relative path."""
    texts: dict[Path, str] = {}
    for rel in (HOOKS_JSON_REL, *HOOK_SCRIPT_RELS, AGENTS_REL):
        texts[rel] = _template_source(templates, rel).read_text(encoding="utf-8")
    return texts


def _text_diff(before: str, after: str, label: str) -> str:
    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"{label} (current)",
            tofile=f"{label} (planned)",
        )
    )


def _parse_object(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}


def _merged_hooks_write(config: Path, template_text: str) -> tuple[str, str, bool]:
    """The text ``~/.codex/hooks.json`` should hold after merge.

    Returns ``(write_text, current_text, changed)``. An existing valid file is
    preserved: our three hook events are added or updated inside its ``hooks``
    object and every other key (other events, description) survives byte-faithful
    apart from the merged event. A corrupt existing file raises
    :class:`HostConfigError` so nothing is ever overwritten behind the user's
    back.
    """
    current = config.read_bytes().decode("utf-8") if config.exists() else ""
    if not current.strip():
        return template_text, current, True
    before = load_host_json(config, missing_ok=False)
    merged = dict(before)
    hooks = merged.get("hooks")
    if hooks is None:
        hooks = {}
        merged["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise HostConfigError(config, "'hooks' must be an object in ~/.codex/hooks.json")
    template_hooks = json.loads(template_text)["hooks"]
    for event, matchers in template_hooks.items():
        if hooks.get(event) != matchers:
            hooks[event] = matchers
    write_text = json_file_text(merged, raw=current)
    return write_text, current, write_text != current


def _merged_agents_write(config: Path, fragment: str) -> tuple[str, str, bool]:
    """The text ``~/.codex/AGENTS.md`` should hold after appending the fragment.

    Returns ``(write_text, current_text, changed)``. Never overwrites user
    content: a fresh or blank file takes the fragment as-is; an existing file
    gets the fragment appended with a blank-line separator; a file that already
    carries the fragment (as suffix or embedded anywhere) is left untouched, so
    a repeated install is a no-op.
    """
    current = config.read_bytes().decode("utf-8") if config.exists() else ""
    if not current.strip():
        return fragment, current, True
    if current.endswith(fragment) or fragment in current:
        return current, current, False
    write_text = current.rstrip() + "\n\n" + fragment
    return write_text, current, True


def strip_agents_fragment(text: str, fragment: str) -> str | None:
    """Undo the fragment append without touching user content.

    Returns the text with the fragment removed, ``""`` when the whole file is
    the fragment (uninstall deletes it), and ``None`` when the fragment is not
    present (the user edited the file; leave it alone).
    """
    if text == fragment:
        return ""
    if text.endswith(fragment):
        head = text[: -len(fragment)].rstrip("\n")
        return head + "\n" if head else ""
    return None


def plan_codex_files(home: Path, *, templates: Path | None = None) -> list[RegistrationPlan]:
    """Read-only plan of the user-level Codex artifacts (hooks + AGENTS.md).

    Two approvable items: ``codex-hooks`` (hooks.json merge plus the companion
    hook scripts, written together) and ``codex-agents`` (the AGENTS.md guidance
    fragment, appended to any existing file). ``changed`` covers every file of
    the item, so a second identical install is a no-op even when only a
    companion script differs.
    """
    home = Path(home)
    root = templates if templates is not None else adapter_templates_dir()
    texts = artifact_texts(root)

    hooks_config = home / HOOKS_JSON_REL
    hooks_write, current_text, hooks_changed = _merged_hooks_write(hooks_config, texts[HOOKS_JSON_REL])
    companion_changed = False
    for rel in HOOK_SCRIPT_RELS:
        target = home / rel
        if not target.exists() or target.read_text(encoding="utf-8") != texts[rel]:
            companion_changed = True
    changed = hooks_changed or companion_changed
    hooks_plan = RegistrationPlan(
        host=HOOKS_HOST_KEY,
        display="Codex hooks",
        config=hooks_config,
        format="json",
        before=_parse_object(current_text),
        after=_parse_object(hooks_write),
        before_text=current_text,
        write_text=hooks_write,
        diff=_text_diff(current_text, hooks_write, "Codex hooks") if changed else "",
        changed=changed,
        files=tuple((home / rel, texts[rel]) for rel in HOOK_SCRIPT_RELS),
        what="install the user-level MnemoSeed Codex hooks",
    )

    agents_config = home / AGENTS_REL
    agents_write, agents_current, agents_changed = _merged_agents_write(agents_config, texts[AGENTS_REL])
    agents_plan = RegistrationPlan(
        host=AGENTS_HOST_KEY,
        display="Codex AGENTS.md",
        config=agents_config,
        format="text",
        before={},
        after={},
        before_text=agents_current,
        write_text=agents_write,
        diff=_text_diff(agents_current, agents_write, "Codex AGENTS.md") if agents_changed else "",
        changed=agents_changed,
        what="append the MnemoSeed guidance fragment to ~/.codex/AGENTS.md",
    )
    return [hooks_plan, agents_plan]
