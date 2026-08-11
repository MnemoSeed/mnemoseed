"""Project-level Cursor adapter artifacts (FR-6.3b).

The T2 installer registers hosts by writing the single ``mnemoseed`` MCP entry
(``~/.cursor/mcp.json`` for Cursor). On top of that, ``mnemoseed install
--cursor-project <dir>`` plans the project-scoped Cursor adapter files into the
chosen project as separate approvable items:

* one item for the hooks artifact — ``.cursor/hooks.json`` (merged into any
  existing file, preserving unrelated events) plus the hook scripts under
  ``.cursor/hooks/mnemoseed/``;
* one item for the standing rules file ``.cursor/rules/mnemoseed.mdc``.

Both items flow through the same plan -> backup -> diff -> per-item confirm ->
apply path as the MCP registrations, so uninstall can roll them back exactly.
Template texts live in ``adapters/cursor/templates`` and are located through
:func:`adapter_templates_dir` (``MNEMOSEED_ADAPTER_TEMPLATES`` overrides it for
wheels and tests).
"""

from __future__ import annotations

import json
import os
from difflib import unified_diff
from pathlib import Path
from typing import Any

from mnemoseed.installer.hosts import HostConfigError, json_file_text, load_host_json
from mnemoseed.installer.registration import RegistrationPlan

ENV_ADAPTER_TEMPLATES = "MNEMOSEED_ADAPTER_TEMPLATES"

HOOKS_HOST_KEY = "cursor-hooks"
RULES_HOST_KEY = "cursor-rules"

HOOKS_JSON_REL = Path(".cursor") / "hooks.json"
RULES_REL = Path(".cursor") / "rules" / "mnemoseed.mdc"
HOOK_SCRIPT_RELS = (
    Path(".cursor") / "hooks" / "mnemoseed" / "py.sh",
    Path(".cursor") / "hooks" / "mnemoseed" / "mnemoseed_hook_client.py",
    Path(".cursor") / "hooks" / "mnemoseed" / "session_start.py",
    Path(".cursor") / "hooks" / "mnemoseed" / "post_tool_use.py",
    Path(".cursor") / "hooks" / "mnemoseed" / "after_agent_response.py",
)


def adapter_templates_dir() -> Path:
    """The directory holding the Cursor adapter templates.

    Env-overridable (``MNEMOSEED_ADAPTER_TEMPLATES``) so a wheel install or a
    test can point at an explicit source; otherwise resolved relative to this
    checkout (``adapters/cursor/templates``).
    """
    raw = os.environ.get(ENV_ADAPTER_TEMPLATES)
    if raw:
        return Path(raw).expanduser()
    return Path(__file__).resolve().parents[3] / "adapters" / "cursor" / "templates"


def _template_source(root: Path, rel: Path) -> Path:
    if rel == HOOKS_JSON_REL:
        return root / "hooks.json"
    if rel == RULES_REL:
        return root / "rules" / "mnemoseed.mdc"
    return root / "hooks" / rel.name


def artifact_texts(templates: Path) -> dict[Path, str]:
    """Every artifact file's exact install text, keyed by project-relative path."""
    texts: dict[Path, str] = {}
    for rel in (HOOKS_JSON_REL, *HOOK_SCRIPT_RELS, RULES_REL):
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
    """The text ``.cursor/hooks.json`` should hold after merge.

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
        raise HostConfigError(config, "'hooks' must be an object in .cursor/hooks.json")
    template_hooks = json.loads(template_text)["hooks"]
    for event, matchers in template_hooks.items():
        if hooks.get(event) != matchers:
            hooks[event] = matchers
    write_text = json_file_text(merged, raw=current)
    return write_text, current, write_text != current


def plan_cursor_project(project: Path, *, templates: Path | None = None) -> list[RegistrationPlan]:
    """Read-only plan of the Cursor project artifacts (hooks + rules).

    Two approvable items: ``cursor-hooks`` (hooks.json merge plus the companion
    hook scripts, written together) and ``cursor-rules`` (the alwaysApply
    standing guidance). ``changed`` covers every file of the item, so a second
    identical install is a no-op even when only a companion script differs.
    """
    project = Path(project)
    root = templates if templates is not None else adapter_templates_dir()
    texts = artifact_texts(root)

    hooks_config = project / HOOKS_JSON_REL
    hooks_write, current_text, hooks_changed = _merged_hooks_write(hooks_config, texts[HOOKS_JSON_REL])
    companion_changed = False
    for rel in HOOK_SCRIPT_RELS:
        target = project / rel
        if not target.exists() or target.read_text(encoding="utf-8") != texts[rel]:
            companion_changed = True
    changed = hooks_changed or companion_changed
    hooks_plan = RegistrationPlan(
        host=HOOKS_HOST_KEY,
        display="Cursor hooks",
        config=hooks_config,
        format="json",
        before=_parse_object(current_text),
        after=_parse_object(hooks_write),
        before_text=current_text,
        write_text=hooks_write,
        diff=_text_diff(current_text, hooks_write, "Cursor hooks") if changed else "",
        changed=changed,
        files=tuple((project / rel, texts[rel]) for rel in HOOK_SCRIPT_RELS),
        what="install the project-level MnemoSeed Cursor hooks",
    )

    rules_config = project / RULES_REL
    rules_current = rules_config.read_text(encoding="utf-8") if rules_config.exists() else ""
    rules_changed = rules_current != texts[RULES_REL]
    rules_plan = RegistrationPlan(
        host=RULES_HOST_KEY,
        display="Cursor rules",
        config=rules_config,
        format="text",
        before={},
        after={},
        before_text=rules_current,
        write_text=texts[RULES_REL],
        diff=_text_diff(rules_current, texts[RULES_REL], "Cursor rules") if rules_changed else "",
        changed=rules_changed,
        what="install the standing MnemoSeed rules file",
    )
    return [hooks_plan, rules_plan]
