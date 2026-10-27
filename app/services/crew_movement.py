"""Signing a crew member on and off through the transfer screen — HPID spec §8.

    "A joining crew member may not be in the original sailing manifest and may
    have no account. When agent selects SIGN ON... create or resolve a
    CrewAssignment for the correct active vessel call, generate a new opaque
    HPID, and leave crew_profile_id null until safely linked. If the person is
    already in the manifest for that call, REUSE that assignment; do not
    duplicate it."

Three rules carry the weight here.

**Resolve before creating.** A person can reach a call by three routes -- a
manifest row, an earlier sign-on, or a later one -- and each must land on the
same assignment and the same reference. Resolution is by normalised passport
on that call, which is also what the partial unique index enforces, so two
concurrent requests cannot produce two references for one person.

**Sign-off never mints anything.** It ends the assignment it was given and
keeps its reference: §5.1 is explicit that a closed assignment keeps its
identifier as a historical reference while its active privileges expire.

**A trip is not a movement.** §8: "Trip booked/completed is NOT automatically
confirmed sign-on/off. Require explicit movement confirmation for verified
company movement metrics." Nothing in this module is called from a booking;
an agent confirms the movement, and `signed_on_at` / `signed_off_at` record
that they did rather than inferring it from a cab ride.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel import Vessel
from app.services import assignment_source
from app.services.assignment_ref import issue_assignment_ref
from app.services.crew_identity import (
    normalize_passport_number,
    normalized_passport_expression,
)
from app.services.crew_reference import normalize_nationality
from app.services.historical_context import active_vessel_call


class MovementError(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def assignment_on_call_for_passport(
    db: Session, *, vessel_call_id: int, passport_number: Optional[str]
) -> Optional[CrewAssignment]:
    """The open assignment on this call for this passport, if there is one.

    The resolution step §8 and H20 require. It deliberately looks at the whole
    call rather than at manifest rows, because a sign-on that preceded the
    manifest has no manifest row to be found by.
    """
    passport = normalize_passport_number(passport_number)
    if not passport:
        return None
    return (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.vessel_call_id == vessel_call_id,
            CrewAssignment.ended_at.is_(None),
            normalized_passport_expression(CrewAssignment.passport_number) == passport,
        )
        .order_by(CrewAssignment.id)
        .first()
    )


def sign_on(
    db: Session,
    *,
    vessel: Vessel,
    crew_name: str,
    passport_number: str,
    nationality: Optional[str] = None,
    rank: Optional[str] = None,
    shore_pass_eligible: bool = False,
    actor=None,
) -> CrewAssignment:
    """Put a joining crew member on the vessel's active call.

    Returns the existing assignment when one already covers this person on
    this call -- from the manifest or from an earlier sign-on -- so H20's
    "resolve existing assignment; no duplicate HPID" holds whichever order the
    two arrive in.
    """
    if not (crew_name or "").strip():
        raise MovementError("A name is required to sign someone on")
    if not normalize_passport_number(passport_number):
        raise MovementError(
            "A passport number is required: it is what resolves this person "
            "to an existing assignment rather than creating a second one"
        )

    call = active_vessel_call(db, vessel, create=True)
    if call is None:
        raise MovementError(
            "This vessel has no active port call to sign anyone on to"
        )

    existing = assignment_on_call_for_passport(
        db, vessel_call_id=call.id, passport_number=passport_number
    )
    if existing is not None:
        # Already here. Record the movement without touching the identifier.
        if existing.signed_on_at is None:
            existing.signed_on_at = _utcnow()
        db.flush()
        return existing

    assignment = CrewAssignment(
        vessel_call_id=call.id,
        # §8: left null until the person registers and is safely linked.
        crew_profile_id=None,
        vessel_crew_id=None,
        crew_name=crew_name.strip(),
        rank=rank,
        nationality=normalize_nationality(nationality) or nationality,
        hpid=None,
        public_ref=issue_assignment_ref(db),
        passport_number=normalize_passport_number(passport_number),
        shore_pass_eligible=bool(shore_pass_eligible),
        source=assignment_source.SIGN_ON,
        started_at=_utcnow(),
        signed_on_at=_utcnow(),
    )
    db.add(assignment)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # Two agents signing the same person on at once. The partial unique
        # index on (call, normalised passport) refuses the second; §11.2 wants
        # one assignment and one reference, so reload rather than retry.
        found = assignment_on_call_for_passport(
            db, vessel_call_id=call.id, passport_number=passport_number
        )
        if found is None:
            raise
        return found
    return assignment


def sign_off(db: Session, *, assignment: CrewAssignment, actor=None) -> CrewAssignment:
    """End this assignment, keeping its reference.

    §5.1: "expires" means the permission ends, not that the historical
    identifier vanishes. Nothing here writes `public_ref`.
    """
    if assignment is None:
        raise MovementError("There is no assignment to sign off")
    if assignment.ended_at is not None:
        raise MovementError("This crew member is already signed off")
    now = _utcnow()
    assignment.signed_off_at = now
    assignment.ended_at = now
    db.flush()
    return assignment
