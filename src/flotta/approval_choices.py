"""Which answers a person may give an approval, shared by the door and the control plane.

These lived in `flotta.door`, and the control plane imported them from there to check
an answer given on Home. The control plane's image installs no `websockets`, which the
door package needs on import, so every answer from Home was a 500 while the suite,
which has it, stayed green. This module imports nothing, so either side can hold it.
"""

from __future__ import annotations

#: What a client's own socket may answer with. Never "always": an answer that
#: outlives the question is the agency's to give (`flotta.door.conversation`).
CLIENT_APPROVAL_CHOICES = frozenset({"once", "session", "deny"})

#: What an answer from outside a conversation may tell the box: never `session`,
#: `always` or `all`, which outlive the question the person saw (FLOTTA-166).
OUTSIDE_CHOICES = ("once", "deny")
