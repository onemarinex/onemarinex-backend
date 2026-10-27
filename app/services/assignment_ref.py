"""The opaque public identifier for one crew member on one vessel call.

The legacy HPID is ``HP-{passport}-{NAT}-{PORT}``, so every party that received
it received a passport number, and the same person carried the same value from
voyage to voyage. The replacement is the opposite on both counts: random,
meaningless, and issued afresh for each ``CrewAssignment``.

Additive by design. This issues a new identifier alongside the existing
``hpid`` column; nothing reads it yet and no legacy value is rewritten. The
cutover of public payloads is a separate deployable step, so that a rollback at
any point leaves a schema both the old and new application can run on.

**It is an identifier, never an authorisation.** Holding one proves nothing;
every caller must still be authorised for the assignment it names.
"""
import secrets
import string

from sqlalchemy.orm import Session

# Crockford base32 without I, L, O and U: no character pairs that a person
# reading one off a screen or a printed manifest can confuse, and no accidental
# words. 26 characters from this alphabet is a little over 128 bits.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_LENGTH = 26

# Owner decision, 26 Oct 2026: the prefix the specification names, and the only
# one. An earlier draft issued HPA- to be distinguishable from the legacy
# format at a glance; those references existed solely in a development
# database, were never deployed and were reissued rather than carried.
#
# It stays unambiguous without the distinct prefix, because the shape does the
# work. A legacy identifier is HP-{passport}-{NAT}-{PORT}, so its body always
# contains further hyphens, and a hyphen is not in the alphabet below -- a
# legacy value can therefore never satisfy the check, whatever its length.
PREFIX = "HP"

# Enough attempts that exhausting them means something is wrong -- a broken
# random source, or the wrong table -- rather than genuinely bad luck.
_MAX_ATTEMPTS = 5


def generate_assignment_ref() -> str:
    """One opaque reference. No input, so nothing about the person can leak."""
    body = "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))
    return f"{PREFIX}-{body}"


def issue_assignment_ref(db: Session) -> str:
    """An opaque reference not already taken.

    The unique index is the real guarantee; this only avoids making the caller
    handle a collision that is vanishingly unlikely to begin with.
    """
    from app.db.models.crew_assignment import CrewAssignment

    for _ in range(_MAX_ATTEMPTS):
        candidate = generate_assignment_ref()
        taken = (
            db.query(CrewAssignment.id)
            .filter(CrewAssignment.public_ref == candidate)
            .first()
        )
        if taken is None:
            return candidate
    raise RuntimeError(
        "Could not issue an unused assignment reference; check the random source"
    )


def looks_like_assignment_ref(value: str) -> bool:
    """Whether a string is one of ours, for routing a lookup to the right column.

    Deliberately a shape check and nothing more: it says which column to search,
    never whether the caller may have what it finds.
    """
    if not value:
        return False
    value = value.strip().upper()
    prefix, _, body = value.partition("-")
    return (
        prefix == PREFIX
        and len(body) == _LENGTH
        and all(c in _ALPHABET for c in body)
    )


def resolve_crew_identifier(db: Session, value: str) -> str:
    """Translate a public reference into the manifest identifier it names.

    Callers key crew lookups on ``VesselCrew.hp_id``. This lets them accept
    either form without changing that: a legacy identifier passes through
    untouched, and an opaque reference is resolved to the identifier of the
    assignment it belongs to.

    **It translates and nothing else.** It performs no access check and grants
    no access. Every caller keeps whatever scoping it already applied, so an
    agent presenting another agency's reference still reaches that agency's
    filter and is refused there. A reference that resolves to nothing is
    returned unchanged, so the caller's own lookup produces its usual
    not-found rather than a different error that would confirm the reference
    exists.
    """
    from app.db.models.crew_assignment import CrewAssignment

    if not looks_like_assignment_ref(value):
        return value
    assignment = (
        db.query(CrewAssignment)
        .filter(CrewAssignment.public_ref == value.strip().upper())
        .first()
    )
    if assignment is None or not assignment.hpid:
        return value
    return assignment.hpid
