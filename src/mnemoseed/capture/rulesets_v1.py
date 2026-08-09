"""Ruleset v1 — the default data set for the F1 Local Stripper (FR-1.2).

Rules are data, not control flow: each entry is a Rule with a target, an
action and a pattern, so the set can be hot-swapped by the daemon. Every
pattern deliberately anchors to a mechanical shape (a package-manager summary,
a stage line, a progress bar), never to prose wording; see the safety tests.

Rule order matters only within an action family: redaction normalizes text
first (ANSI codes), then line strips, then the dead-loop block collapse.
"""

from __future__ import annotations

from mnemoseed.capture.stripper import ContentTarget, Rule, RuleSet, StripAction

RULESET_V1 = RuleSet(
    rules=(
        # ---- ANSI / terminal control (span redaction, applied first) ----
        Rule(
            id="ansi-codes",
            target=ContentTarget.BOTH,
            action=StripAction.REDACT_SPAN,
            pattern=(
                r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]"  # CSI
                r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
                r"|[\x40-\x5f])"  # other ESC sequences
            ),
        ),
        # ---- carriage-return progress fragments (overwritten on any screen) ----
        Rule(
            id="cr-progress-fragment",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^[^\r\n]*\r\Z",
        ),
        # ---- package managers: npm ----
        Rule(
            id="npm-summary",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=(
                r"(?i)^\s*(?:added|removed|changed) \d+ packages?"
                r"(?:\s*,\s*and changed \d+ packages?)? in [\d.]+[smhd]\w{0,2}\s*$"
            ),
        ),
        Rule(
            id="npm-vulns",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=(
                r"(?i)^\s*found \d+ (?:(?:low|moderate|high|critical) severity )?"
                r"vulnerabilit(?:y|ies)(?: in \d+ scanned packages?)?\s*$"
            ),
        ),
        Rule(
            id="npm-warn",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*npm\s+warn(?:ing)?\b\s",
        ),
        Rule(
            id="npm-added-list",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*\+\s+\S+@\S+\s*$",
        ),
        # ---- package managers: pip / uv / cargo ----
        Rule(
            id="pip-acquire",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*collecting [\w.\-]+(?:(?:==|>=|<=|~=|!=)[\w.\-+]+)?\s*$",
        ),
        Rule(
            id="pip-download",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*downloading \S+\.(?:whl|tar\.gz|zip|tgz)\b[^\r\n]*$",
        ),
        Rule(
            id="pip-build-wheel",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*(?:building wheel|building editable wheel|creating build env)[^\r\n]*$",
        ),
        Rule(
            id="pip-using-cached",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*using cached \S+\.(?:whl|tar\.gz|zip|tgz)[^\r\n]*$",
        ),
        Rule(
            id="pip-install-summary",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*(?:installing collected packages:|successfully (?:built|installed))[^\r\n]*$",
        ),
        Rule(
            id="rich-progress-bar",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*[─-▟]{4,}[^\r\n]*\d[^\r\n]*$",
        ),
        Rule(
            id="uv-counts",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*(?:Resolved|Downloaded|Installed|Prepared|Audited) \d+ packages?[^\r\n]*$",
        ),
        Rule(
            id="uv-built",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*Built \S+ v\d[\d.]*[^\r\n]*$",
        ),
        Rule(
            id="cargo-compile",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*Compiling \S+ v\d[\d.]*[^\r\n]*$",
        ),
        Rule(
            id="cargo-finished",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*Finished \x60[^\x60]*\x60 profile [^\r\n]*target\(s\) in [\d.]+s\s*$",
        ),
        # ---- compiler / build logs ----
        Rule(
            id="cmake-stage",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=(
                r"^\s*\[[\d/ %]*\]\s*"
                r"(?:Building|Compiling|Linking|Generating|Copying|Creating|Writing|Processing)\s"
            ),
        ),
        Rule(
            id="rustc-warning",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=(
                r"(?i)^\s*warning: (?:"
                r"unused (?:imports|variable|mut|field|struct|enum variant):?\s[^\r\n]*"
                r"|\d+ warnings? emitted"
                r"|\x60[^\x60]*\x60(?:\([^)]*\))?[^\r\n]*)"
                r"\s*$"
            ),
        ),
        Rule(
            id="rustc-location",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=(r"^\s*(?:-->|─{2,}>)\s+\S+:\d+(?::\d+)?[^\r\n]*$"),
        ),
        Rule(
            id="rustc-note",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*=\s+note:\s+[^\r\n]*$",
        ),
        Rule(
            id="gyp-noise",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"(?i)^\s*gyp\s+(?:err|warn|info|verbose)\S*\s",
        ),
        # ---- progress bars / spinners ----
        Rule(
            id="percent-bar",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*\[[#=<>._&+% -]*\]\s*\d{1,3}%\s*$",
        ),
        Rule(
            id="tqdm-bar",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*\d{1,3}(?:\.\d+)?%\|[^\r\n]*\|[^\r\n]*(?:\[[^\r\n]*\])?\s*$",
        ),
        Rule(
            id="spinner-braille",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.STRIP_LINE,
            pattern=r"^\s*[⠀-⣿]+(?:\s+[^\r\n]+)?\s*$",
        ),
        # ---- dead-loop repeated error blocks ----
        Rule(
            id="collapse-repeated-blocks",
            target=ContentTarget.TOOL_OUTPUT,
            action=StripAction.COLLAPSE_RUNS,
            min_run=3,  # dead loops emit many repeats; two equal lines may be legit
        ),
    )
)
