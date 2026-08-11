---
description: Run or inspect MnemoSeed dream consolidation
argument-hint: [once|status]
allowed-tools: Bash(*)
---

Dream is the offline consolidation pass that lifts captured turns into durable graph memory. `once` starts exactly one manual cycle (snapshot -> reflect -> merge -> safe-clear); `status` (default) reports the consolidation trigger state.

Run the manual cycle:

!`bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" once`

Run the status check:

!`bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/dream.py" status`

Relay the trigger state or the "nothing to consolidate" message to the user.
