---
description: Show the profile's memory status and recent activity
allowed-tools: Bash(*)
---

Inspect the current profile's memory: stored chunk and graph-node totals plus the most recent captured activity. Use this to audit what MnemoSeed has recorded and when the record is stale.

Run the status report:

!`bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/memory.py" $ARGUMENTS`

Briefly relay the totals and the recent events to the user.
