---
description: Explicitly delete a memory by id or query
argument-hint: [target]
---

Delete a specific MnemoSeed memory, or memories matching a query. Use this only when the user explicitly asks to forget something.

Run the deletion:

!`bash hooks/py.sh scripts/forget.py $ARGUMENTS`

Confirm what was removed, or report that nothing matched.
