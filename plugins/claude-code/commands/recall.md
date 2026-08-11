---
description: Recall what MnemoSeed remembers about a topic and cite it
argument-hint: [query]
allowed-tools: Bash(*)
---

Query the MnemoSeed memory daemon for the profile's relevant memories and show them to the agent. Use this when the user asks about a past decision, preference, constraint, or project context.

Run the recall for the user's query:

!`bash "${CLAUDE_PLUGIN_ROOT}/hooks/py.sh" "${CLAUDE_PLUGIN_ROOT}/scripts/recall.py" $ARGUMENTS`

Then summarize what came back, or note that nothing matched when the store reports no memory for the query. Always attribute recalled facts to memory and flag any contradictions with what the user is currently saying.
