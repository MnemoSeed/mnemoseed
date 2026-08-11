---
description: Remove one memory (chunk, node, or entity) from the profile
argument-hint: --chunk <id> | --node <id> | --entity <name>
allowed-tools: Bash(*)
---

Remove one provenance unit: a chunk, a graph node, or everything tagged with an entity name. Always confirm the target with the user before running, because removal is permanent.

Run the removal once the target is confirmed:

!`bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/forget.py" $ARGUMENTS`

Report what was removed or that nothing matched.
