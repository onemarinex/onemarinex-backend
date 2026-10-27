"""Create and resolve immutable operational vessel context.

Current profile fields answer where a person or ship is now. These helpers
stamp the vessel call, agency, crew assignment, and port onto records when an
operation happens so later reassignment cannot rewrite the event's history.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.port import Port
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.services.assignment_ref import issue_assignment_ref
from app.services.crew_identity import (
    normalize_passport_number,
    normalized_passport_expression,
)
from app.services.crew_reference import normalize_nationality


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def agent_profile_for_vessel(db: Session, vessel: Vessel) -> Optional[AgentProfile]:
    if not vessel.agent_id:
        return None
    return (
        db.query(AgentProfile)
        .filter(AgentProfile.user_id == vessel.agent_id)
        .first()
    )


def port_for_reference(db: Session, reference: Optional[str]) -> Optional[Port]:
    value = (reference or "").strip()
    if not value:
        return None
    return db.query(Port).filter(or_(Port.code == value, Port.name == value)).first()


def active_vessel_call(
    db: Session,
    vessel: Optional[Vessel],
    *,
    create: bool = True,
) -> Optional[VesselCall]:
    if vessel is None:
        return None
    call = (
        db.query(VesselCall)
        .filter(VesselCall.vessel_id == vessel.id, VesselCall.ended_at.is_(None))
        .order_by(VesselCall.id.desc())
        .first()
    )
    if call is not None or not create:
        return call

    # A vessel call is an operational boundary, not a convenience row. Never
    # manufacture a new one while a vessel is outside current operations. The
    # lifecycle transition which reassigns/reopens a vessel must first make it
    # active and assign an agent explicitly.
    stored_status = str(vessel.status or "").strip().lower()
    if vessel.agent_id is None or stored_status in {"archived", "departed"}:
        return None

    agent = agent_profile_for_vessel(db, vessel)
    port = port_for_reference(db, agent.assigned_port if agent else None)
    call = VesselCall(
        vessel_id=vessel.id,
        agency_id=agent.id if agent else None,
        port_id=port.id if port else None,
        vessel_name=vessel.name,
        imo_number=vessel.imo_number,
        flag=vessel.flag,
        agency_name=(agent.agency_name if agent else None) or vessel.agency_name,
        port_name=(port.code if port else None) or (agent.assigned_port if agent else None),
        eta=vessel.eta,
        etd=vessel.etd,
        started_at=vessel.eta or vessel.created_at or _utcnow(),
        status="ACTIVE",
    )
    # The select above and this insert are not atomic, so two writers can both
    # find no open call. uq_vessel_calls_active_vessel keeps the data correct;
    # the savepoint keeps the loser's wider operation alive instead of failing
    # it with an IntegrityError. Opening a call happens on read paths too --
    # _resolve_report_call reaches here -- so two report loads can collide.
    try:
        with db.begin_nested():
            db.add(call)
            db.flush()
    except IntegrityError:
        existing = (
            db.query(VesselCall)
            .filter(VesselCall.vessel_id == vessel.id, VesselCall.ended_at.is_(None))
            .order_by(VesselCall.id.desc())
            .first()
        )
        if existing is None:
            raise
        return existing
    # Reassigning an existing vessel starts a new call while keeping the
    # manifest rows. Materialise fresh call-specific assignments so subsequent
    # events do not fall back to person identity alone.
    manifests = db.query(VesselCrew).filter(VesselCrew.vessel_id == vessel.id).all()
    for manifest in manifests:
        # Each in its own savepoint. A manifest can hold two rows that resolve
        # to one person -- an older entry the agent never removed, and the
        # current one -- and both want the same (call, profile), which the
        # partial unique index refuses. Without the savepoint that refusal
        # aborted the whole transaction and the vessel could not open a call
        # at all: one stale row made the ship unusable.
        #
        # Skipping the loser is right rather than merely convenient. The row
        # that won is an assignment for that person on that call, which is
        # exactly what the second row would have created.
        try:
            with db.begin_nested():
                assignment_for_manifest(db, vessel, manifest)
        except IntegrityError:
            continue
    return call


def finish_vessel_call(
    db: Session,
    vessel: Vessel,
    *,
    status: str = "ARCHIVED",
    ended_at: Optional[datetime] = None,
) -> None:
    call = active_vessel_call(db, vessel, create=False)
    if call is None:
        return
    ended_at = ended_at or _utcnow()
    call.ended_at = ended_at
    call.status = status
    db.query(CrewAssignment).filter(
        CrewAssignment.vessel_call_id == call.id,
        CrewAssignment.ended_at.is_(None),
    ).update({CrewAssignment.ended_at: ended_at}, synchronize_session="fetch")


def refresh_active_vessel_call(db: Session, vessel: Vessel) -> Optional[VesselCall]:
    """Refresh mutable voyage details while a call is still operational."""
    # Editing voyage metadata must never create a fresh call. Call creation is
    # reserved for vessel onboarding and an explicit reassignment transition.
    call = active_vessel_call(db, vessel, create=False)
    if call is None:
        return None
    agent = agent_profile_for_vessel(db, vessel)
    port = port_for_reference(db, agent.assigned_port if agent else call.port_name)
    call.vessel_name = vessel.name
    call.imo_number = vessel.imo_number
    call.flag = vessel.flag
    call.agency_id = agent.id if agent else call.agency_id
    call.agency_name = (agent.agency_name if agent else None) or vessel.agency_name
    call.port_id = port.id if port else call.port_id
    call.port_name = (port.code if port else None) or (
        agent.assigned_port if agent else call.port_name
    )
    call.eta = vessel.eta
    call.etd = vessel.etd
    return call


def crew_profile_for_manifest(
    db: Session, manifest: VesselCrew
) -> Optional[CrewProfile]:
    """The one registered person this manifest row is, or nobody.

    HPID specification §4.1: the matching evidence is the normalised passport
    number **and** the normalised nationality, together. Two things this used
    to do and no longer does.

    It matched on either field alone, joined by OR. A passport number is not
    unique across issuing states, so two different people holding the same
    number could match — and because the comparison was a bare equality, the
    second one was indistinguishable from a repeat visit by the first.

    It also matched on the legacy HPID. That identifier is derived from the
    passport, so using it as a key made the public reference an identity
    credential, which §4.1 forbids by name: "HPID is never the identity-
    matching key". Nothing is lost by dropping it, because every HPID that
    could have matched encodes the passport that still does.

    Nationality rather than passport-issuing country is deliberate. The two are
    not legally interchangeable and the specification is explicit that issuing
    country must not be added in this release; the existing field is used,
    normalised through the alias table so India, Indian and IN all reduce to
    IN. A value the table cannot resolve -- IND, which is both India and
    Indonesia -- returns nothing rather than a guess, and the row stays
    unlinked for review. Refusing to link costs a person a PENDING card;
    guessing wrong attaches them to a stranger's voyage.
    """
    passport = normalize_passport_number(manifest.passport_number)
    nationality = normalize_nationality(manifest.nationality)
    if not passport or not nationality:
        return None

    # Passport narrows in SQL; nationality is compared in Python because the
    # alias table has no SQL equivalent and the candidate set here is tiny.
    candidates = (
        db.query(CrewProfile)
        .filter(
            normalized_passport_expression(CrewProfile.passport_number) == passport
        )
        .limit(10)
        .all()
    )
    matches = [
        candidate
        for candidate in candidates
        if normalize_nationality(candidate.nationality) == nationality
    ]
    # Two matches is an identity conflict, not a tie to break.
    return matches[0] if len(matches) == 1 else None


def _open_assignment_for_passport(
    db: Session, *, vessel_call_id: int, passport_number
) -> Optional[CrewAssignment]:
    """An open assignment on this call for this passport, with no manifest row.

    Restricted to rows that are not already tied to a manifest entry, so this
    can only ever adopt a sign-on or self-selected row -- never steal another
    manifest row's assignment.
    """
    passport = normalize_passport_number(passport_number)
    if not passport:
        return None
    return (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.vessel_call_id == vessel_call_id,
            CrewAssignment.ended_at.is_(None),
            CrewAssignment.vessel_crew_id.is_(None),
            normalized_passport_expression(CrewAssignment.passport_number) == passport,
        )
        .order_by(CrewAssignment.id)
        .first()
    )


def assignment_for_manifest(
    db: Session,
    vessel: Vessel,
    manifest: VesselCrew,
    *,
    profile: Optional[CrewProfile] = None,
    create: bool = True,
) -> Optional[CrewAssignment]:
    call = active_vessel_call(db, vessel, create=create)
    if call is None:
        return None
    assignment = (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.vessel_call_id == call.id,
            CrewAssignment.vessel_crew_id == manifest.id,
            CrewAssignment.ended_at.is_(None),
        )
        .first()
    )
    if assignment is not None or not create:
        return assignment

    # H20: the same person may already be on this call from a sign-on that
    # preceded the manifest. That row has no vessel_crew_id, so the query
    # above cannot see it, and creating a second would give one person two
    # references for one call. Adopt it instead -- "resolve existing
    # assignment; no duplicate HPID" (§8).
    adopted = _open_assignment_for_passport(
        db, vessel_call_id=call.id, passport_number=manifest.passport_number
    )
    if adopted is not None:
        adopted.vessel_crew_id = manifest.id
        if adopted.crew_profile_id is None:
            resolved = profile or crew_profile_for_manifest(db, manifest)
            if resolved is not None:
                adopted.crew_profile_id = resolved.id
        if manifest.shore_pass_eligible:
            # The manifest is the agent's word on eligibility; a sign-on row
            # carries none of its own.
            adopted.shore_pass_eligible = True
        db.flush()
        return adopted

    profile = profile or crew_profile_for_manifest(db, manifest)

    # One person, one call, one assignment -- §10. A manifest can name the
    # same person twice: an older entry the agent never removed and the
    # current one. The second must resolve to the first rather than mint a
    # second reference, which the partial unique index would refuse anyway.
    #
    # The existing row keeps its own vessel_crew_id. Repointing it would
    # rewrite which manifest entry the history was built from, to no benefit.
    if profile is not None:
        held = (
            db.query(CrewAssignment)
            .filter(
                CrewAssignment.vessel_call_id == call.id,
                CrewAssignment.crew_profile_id == profile.id,
                CrewAssignment.ended_at.is_(None),
            )
            .order_by(CrewAssignment.id)
            .first()
        )
        if held is not None:
            return held

    assignment = CrewAssignment(
        vessel_call_id=call.id,
        crew_profile_id=profile.id if profile else None,
        vessel_crew_id=manifest.id,
        crew_name=manifest.name,
        rank=manifest.rank,
        nationality=manifest.nationality,
        hpid=manifest.hp_id,
        # Issued per assignment, so the same person joining a later call is a
        # different public reference. The legacy hpid above is copied from the
        # manifest and still carries the passport; this replaces it.
        public_ref=issue_assignment_ref(db),
        passport_number=manifest.passport_number,
        shore_pass_eligible=bool(manifest.shore_pass_eligible),
        started_at=manifest.created_at or call.started_at or _utcnow(),
    )
    db.add(assignment)
    db.flush()
    return assignment


def self_selected_assignment(
    db: Session,
    profile: Optional[CrewProfile],
    call: Optional[VesselCall],
) -> Optional[CrewAssignment]:
    """The assignment a crew member gets on a vessel no agency runs — §9.

    An unmanaged ("Other") vessel has no agent, no manifest and no
    agent-issued eligibility, so nothing here ever created an assignment and
    these bookings carried no call-specific reference at all. The
    specification's target is one `SELF_SELECTED_UNMANAGED` assignment per
    profile per call, reused by every later login and booking on that call,
    and a fresh one -- with a fresh reference -- for the next call even when
    the vessel and port are the same.

    **It records self-selection and nothing more.** The source says so by
    name, because this is not an agent confirming crew membership and not an
    immigration clearance. Anything that treats an assignment as evidence of
    either must check `assignment_source.is_agent_attested` first.

    No active call means nothing is created: §9 is explicit that a missing
    call must not be papered over with an invented booking context.
    """
    from app.services.assignment_source import SELF_SELECTED_UNMANAGED

    if profile is None or call is None:
        return None

    existing = (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.vessel_call_id == call.id,
            CrewAssignment.crew_profile_id == profile.id,
            CrewAssignment.ended_at.is_(None),
        )
        .first()
    )
    if existing is not None:
        return existing

    assignment = CrewAssignment(
        vessel_call_id=call.id,
        crew_profile_id=profile.id,
        vessel_crew_id=None,
        crew_name=profile.full_name or "",
        rank=profile.rank,
        nationality=profile.nationality,
        # No manifest, so no legacy identifier to copy. The opaque reference
        # is the only public one these ever have, which is the point.
        hpid=None,
        public_ref=issue_assignment_ref(db),
        passport_number=profile.passport_number,
        # An agency grants shore leave. There is no agency here, so this
        # claims nothing about one.
        shore_pass_eligible=False,
        source=SELF_SELECTED_UNMANAGED,
        started_at=call.started_at or _utcnow(),
    )
    db.add(assignment)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        # Two devices selecting the same call at once. The partial unique
        # index on (call, profile) refuses the second, and the row the first
        # one wrote is the answer -- §11.2 requires exactly one.
        return (
            db.query(CrewAssignment)
            .filter(
                CrewAssignment.vessel_call_id == call.id,
                CrewAssignment.crew_profile_id == profile.id,
                CrewAssignment.ended_at.is_(None),
            )
            .first()
        )
    return assignment


def active_assignment_for_profile(
    db: Session,
    profile: Optional[CrewProfile],
    *,
    vessel_call_id: Optional[int] = None,
) -> Optional[CrewAssignment]:
    if profile is None:
        return None
    query = db.query(CrewAssignment).filter(
        CrewAssignment.crew_profile_id == profile.id,
        CrewAssignment.ended_at.is_(None),
    )
    if vessel_call_id is not None:
        query = query.filter(CrewAssignment.vessel_call_id == vessel_call_id)
    matches = query.order_by(CrewAssignment.started_at.desc(), CrewAssignment.id.desc()).limit(2).all()
    # Without a selected trip/call, two concurrent assignments are ambiguous.
    return matches[0] if len(matches) == 1 else None


def eligible_assignments_for_profile(
    db: Session,
    profile: Optional[CrewProfile],
) -> list[CrewAssignment]:
    """All exact assignments this crew member can use for a new operation."""
    if profile is None:
        return []
    return (
        db.query(CrewAssignment)
        .join(VesselCall, VesselCall.id == CrewAssignment.vessel_call_id)
        .join(Vessel, Vessel.id == VesselCall.vessel_id)
        .filter(
            CrewAssignment.crew_profile_id == profile.id,
            CrewAssignment.ended_at.is_(None),
            VesselCall.ended_at.is_(None),
            Vessel.agent_id.isnot(None),
            func.lower(func.coalesce(Vessel.status, "")).notin_(["archived", "departed"]),
        )
        .order_by(CrewAssignment.started_at.desc(), CrewAssignment.id.desc())
        .all()
    )


def selected_assignment_for_profile(
    db: Session,
    profile: Optional[CrewProfile],
    crew_assignment_id: Optional[int],
    *,
    required_when_ambiguous: bool = True,
) -> Optional[CrewAssignment]:
    """Validate a client selection or resolve the only eligible assignment.

    This deliberately refuses passport/HPID/current-profile inference. Those
    fields identify a person, not the vessel call for a new operation.
    """
    matches = eligible_assignments_for_profile(db, profile)
    if crew_assignment_id is not None:
        selected = next(
            (row for row in matches if row.id == crew_assignment_id), None
        )
        if selected is None:
            raise ValueError(
                "The selected vessel assignment is not active for this crew member"
            )
        return selected
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and required_when_ambiguous:
        raise ValueError(
            "Multiple active vessel assignments found; select a vessel before continuing"
        )
    return None


def unique_current_manifest_for_profile(
    db: Session, profile: Optional[CrewProfile]
) -> Optional[VesselCrew]:
    """Resolve the one current manifest row matching this person, or nothing.

    This is used only while creating a new unlinked event. It never repairs a
    historical row, and refuses to choose when the person is on two manifests.
    """
    if profile is None:
        return None
    clauses = []
    if (profile.hpid or "").strip():
        clauses.append(
            func.upper(func.trim(VesselCrew.hp_id)) == profile.hpid.strip().upper()
        )
    if (profile.passport_number or "").strip():
        clauses.append(
            normalized_passport_expression(VesselCrew.passport_number)
            == normalize_passport_number(profile.passport_number)
        )
    if not clauses:
        return None
    matches = db.query(VesselCrew).filter(or_(*clauses)).limit(2).all()
    return matches[0] if len(matches) == 1 else None


def end_manifest_assignment(db: Session, manifest: VesselCrew) -> None:
    assignments = db.query(CrewAssignment).filter(
        CrewAssignment.vessel_crew_id == manifest.id,
        CrewAssignment.ended_at.is_(None),
    ).all()
    now = _utcnow()
    for assignment in assignments:
        assignment.ended_at = now


def event_context(
    db: Session,
    *,
    booking=None,
    profile: Optional[CrewProfile] = None,
    vessel: Optional[Vessel] = None,
) -> dict:
    call = None
    resolution = "unresolved"
    if booking is not None and getattr(booking, "vessel_call_id", None):
        call = db.query(VesselCall).filter(VesselCall.id == booking.vessel_call_id).first()
        resolution = "booking"
    # A booking written without immutable call context is legacy/unresolved.
    # Never create a present-day call while trying to describe its history.
    if call is None and vessel is not None:
        call = active_vessel_call(db, vessel, create=False)
        resolution = "vessel_id" if call is not None else "unresolved"
    assignment = None
    if booking is not None and getattr(booking, "crew_assignment_id", None):
        assignment = db.query(CrewAssignment).filter(
            CrewAssignment.id == booking.crew_assignment_id
        ).first()
    if assignment is None and call is not None and booking is None:
        assignment = active_assignment_for_profile(
            db, profile, vessel_call_id=call.id if call else None
        )
    if call is None and assignment is not None:
        call = assignment.vessel_call
        resolution = "assignment"
    return {
        "vessel_call": call,
        "vessel_id": call.vessel_id if call else None,
        "agency_id": call.agency_id if call else None,
        "port_id": call.port_id if call else None,
        "crew_assignment_id": assignment.id if assignment else None,
        "context_resolution": resolution,
    }


def vessel_call_context(call: Optional[VesselCall], *, fallback_port: Optional[str] = None):
    if call is None:
        return None
    return {
        "id": call.vessel_id,
        "vessel_call_id": call.id,
        "name": call.vessel_name,
        "imo_number": call.imo_number,
        "port_name": call.port_name or fallback_port,
        "flag": call.flag,
        "eta": call.eta,
        "etd": call.etd,
        "berth": call.vessel.berth_assignment if call.vessel else None,
    }
