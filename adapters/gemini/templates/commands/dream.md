---
description: Trigger an immediate memory consolidation pass
argument-hint: [once]
---

Run a MnemoSeed consolidation (dream) pass on the profile's pending memories. Use this when the user explicitly asks to consolidate what has been captured recently.

Run the consolidation:

!`bash hooks/py.sh scripts/dream.py $ARGUMENTS`

Relay the outcome to the user.
