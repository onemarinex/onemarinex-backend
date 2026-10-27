"""How a crew assignment came to exist — HPID specification §11.

Three ways, and the difference matters at the point of reading rather than
writing. §9 is explicit that a self-selected row on an unmanaged vessel
"records self-selection, NOT independently verified crew membership or
immigration clearance", so anything that treats an assignment as proof of
either has to be able to tell them apart.

Deliberately not an enum column. The set will grow — the specification already
contemplates further sources — and a Postgres enum would need a migration to
add one, on a database where migrations do not run.
"""

#: An agent put this person on a manifest for the call.
MANIFEST = "MANIFEST"

#: An agent signed this person on through the transfer screen, possibly before
#: any manifest row and possibly before they hold an account.
SIGN_ON = "SIGN_ON"

#: An authenticated crew member selected an unmanaged ("Other") vessel
#: themselves. No agent, no manifest, no agent-issued eligibility.
SELF_SELECTED_UNMANAGED = "SELF_SELECTED_UNMANAGED"

ALL = frozenset({MANIFEST, SIGN_ON, SELF_SELECTED_UNMANAGED})

#: Sources an agent stands behind. A self-selected row is not one of them, so
#: it must never be read as an agent having confirmed anything.
AGENT_ATTESTED = frozenset({MANIFEST, SIGN_ON})


def is_agent_attested(source) -> bool:
    return (source or MANIFEST) in AGENT_ATTESTED
