from app.services.collection_pages import page_all
from datetime import datetime
from typing import List, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.models.crew_assignment import CrewAssignment
from app.services.assignment_ref import resolve_crew_identifier
from app.db.models.crew_profile import CrewProfile
from app.db.models.cab_booking import CabBooking
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.api.v1.routes_auth import get_current_user
from app.services.crew_service import generate_hpid
from app.services.crew_reference import normalize_nationality, normalize_rank
from app.services.crew_identity import (
    CrewIdentityConflict,
    hpids_match,
    normalize_passport_number,
    validate_passport_number,
    normalized_passport_expression,
    normalized_person_name,
    resolve_verified_crew_profile,
    resolved_identity_decision,
    persist_identity_conflict,
)
import uuid

router = APIRouter()


def is_partnered_agency(agency_name: Optional[str]) -> bool:
    if not agency_name:
        return False
    clean = agency_name.strip().lower()
    return clean not in ["other", "others", "none", "n/a", "", "other agency"]

# --- Pydantic Schemas ---

class CrewMemberIn(BaseModel):
    name: str
    rank: str
    nationality: str
    passport_number: str
    # Accepted for backwards compatibility only. Mapping status is derived
    # from a verified CrewProfile match and this value is never trusted.
    status: Optional[str] = "Pending"
    shore_pass_eligible: Optional[bool] = False
    shore_pass_valid_upto: Optional[datetime] = None

class CrewMemberOut(BaseModel):
    id: int
    name: str
    rank: str
    nationality: Optional[str] = None
    hp_id: Optional[str] = None
    # The opaque per-assignment reference. Absent where the crew member has no
    # active assignment, or where one predates the reference.
    reference: Optional[str] = None
    passport_number: Optional[str] = None
    expiry_date: Optional[datetime] = None
    status: str
    shore_pass_eligible: bool
    shore_pass_valid_upto: Optional[datetime] = None
    
    class Config:
        from_attributes = True

class EligibilityUpdateIn(BaseModel):
    shore_pass_eligible: bool

class CabBookingOut(BaseModel):
    id: int
    pickup_address: str
    drop_address: str
    status: str
    
    class Config:
        from_attributes = True

class CrewProfileOut(BaseModel):
    id: int
    name: str
    rank: str
    hp_id: Optional[str] = None
    status: str
    visits: List[str] = []
    bookings: List[CabBookingOut] = []

    class Config:
        from_attributes = True

class VesselIn(BaseModel):
    name: str
    imo_number: str
    vessel_type: str
    berth_assignment: Optional[str] = None
    flag: Optional[str] = None
    agency_name: Optional[str] = None
    # Accepted for backwards compatibility; roster operations own this value.
    crew_count: Optional[int] = None
    total_crew: Optional[int] = None
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: Literal["Active"] = "Active"

import csv
import io

class ManifestCrewRowOut(BaseModel):
    name: str
    rank: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    shore_pass_eligible: bool = False
    shore_pass_valid_upto: Optional[datetime] = None


class ManifestPreviewOut(BaseModel):
    count: int
    source: str
    warnings: List[str] = []
    crew: List[ManifestCrewRowOut] = []


class ManifestConfirmIn(BaseModel):
    crew: List[ManifestCrewRowOut]


def _agent_vessel_or_404(db: Session, vessel_id: int, current_user: User) -> Vessel:
    if current_user.role == "superadmin":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
    else:
        vessel = db.query(Vessel).filter(
            Vessel.id == vessel_id, Vessel.agent_id == current_user.id
        ).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    return vessel


def _add_crew_conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _resolve_profile_or_queue_conflict(
    db: Session,
    *,
    operation: str,
    vessel: Vessel,
    passport_number: str,
    nationality: str,
    name: str,
    rank: Optional[str],
    generated_hpid: str,
) -> Optional[CrewProfile]:
    try:
        return resolve_verified_crew_profile(
            db,
            passport_number=passport_number,
            nationality=nationality,
            crew_name=name,
            generated_hpid=generated_hpid,
        )
    except CrewIdentityConflict as exc:
        proposed_identity = {
            "name": name,
            "rank": rank,
            "nationality": nationality,
            "passport_number": passport_number,
            "generated_hpid": generated_hpid,
        }
        decided, profile = resolved_identity_decision(
            db,
            operation=operation,
            vessel_id=vessel.id,
            passport_number=passport_number,
            proposed_identity=proposed_identity,
        )
        if decided:
            return profile
        conflict = persist_identity_conflict(
            db,
            operation=operation,
            vessel_id=vessel.id,
            passport_number=passport_number,
            proposed_identity=proposed_identity,
            message=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "identity_conflict_id": conflict.id,
                "status": conflict.status,
                "version": conflict.version,
            },
        ) from exc


def _existing_manifest_or_queue_conflict(
    db: Session,
    *,
    operation: str,
    vessel: Vessel,
    passport_number: str,
    generated_hpid: str,
    proposed_identity: dict,
) -> Optional[VesselCrew]:
    try:
        return _existing_manifest_for_add(
            db,
            vessel,
            passport_number=passport_number,
            generated_hpid=generated_hpid,
        )
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        message = str(exc.detail)
        conflict = persist_identity_conflict(
            db,
            operation=operation,
            vessel_id=vessel.id,
            passport_number=passport_number,
            proposed_identity=proposed_identity,
            message=message,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "message": message,
                "identity_conflict_id": conflict.id,
                "status": conflict.status,
                "version": conflict.version,
            },
        ) from exc


def _same_manifest_identity(
    crew: VesselCrew,
    *,
    name: str,
    nationality: str,
    passport_number: str,
) -> bool:
    """Whether an existing manifest row is this same person.

    The name is not part of the answer. It is how somebody typed a person, not
    who they are, and treating a differently-spelled name as a different
    identity meant re-adding a crew member with their name corrected was
    refused as a conflict. `name` is still taken so the caller reads plainly and
    so the submitted spelling reaches the conflict record.
    """
    existing_nationality = normalize_nationality(crew.nationality, strict=False)
    return (
        existing_nationality == nationality
        and normalize_passport_number(crew.passport_number) == passport_number
    )


def _existing_manifest_for_add(
    db: Session,
    vessel: Vessel,
    *,
    passport_number: str,
    generated_hpid: str,
) -> Optional[VesselCrew]:
    passport_matches = (
        db.query(VesselCrew)
        .filter(
            VesselCrew.vessel_id == vessel.id,
            normalized_passport_expression(VesselCrew.passport_number)
            == passport_number,
        )
        .order_by(VesselCrew.id)
        .limit(2)
        .all()
    )
    if len(passport_matches) > 1:
        raise _add_crew_conflict(
            "This passport already has duplicate entries on the vessel and "
            "requires Superadmin identity reconciliation"
        )

    # Compared on the canonical form rather than the literal string, so a
    # rendering difference does not read as a different person. The port
    # fragment is kept: an HPID is issued per port visit, so HP-X-IN-VIS and
    # HP-X-IN-KRI are two visits by one person and must not collapse. Done in
    # Python over one vessel's manifest, which is tens of rows, rather than as
    # SQL that would have to reimplement the same rules.
    hpid_matches = [
        row
        for row in db.query(VesselCrew)
        .filter(VesselCrew.vessel_id == vessel.id, VesselCrew.hp_id.isnot(None))
        .order_by(VesselCrew.id)
        .all()
        if hpids_match(row.hp_id, generated_hpid)
    ][:2]
    if len(hpid_matches) > 1:
        raise _add_crew_conflict(
            "This HPID already has duplicate entries on the vessel and "
            "requires Superadmin identity reconciliation"
        )

    passport_match = passport_matches[0] if passport_matches else None
    hpid_match = hpid_matches[0] if hpid_matches else None
    if passport_match and hpid_match and passport_match.id != hpid_match.id:
        raise _add_crew_conflict(
            "The passport and HPID refer to different vessel crew entries; "
            "Superadmin identity reconciliation is required"
        )
    if hpid_match and normalize_passport_number(hpid_match.passport_number) not in (
        None,
        passport_number,
    ):
        raise _add_crew_conflict(
            "The generated HPID already belongs to a different passport; "
            "Superadmin identity reconciliation is required"
        )
    return passport_match or hpid_match


def _sync_assignment_eligibility(db: Session, vessel: Vessel, crew: VesselCrew) -> bool:
    """Carry a manifest eligibility change onto the active crew assignment.

    Eligibility is stored twice: on the manifest row the vessel screen edits,
    and on the crew assignment that operational reports and the booking check
    read. Manifest upload and manual crew creation already write both. The
    eligibility toggle and the general crew edit wrote only the manifest, so an
    agent could mark eight crew eligible, see eight on the vessel page, and have
    the report still see whoever the last upload had flagged.

    Returns whether an assignment was found and updated. A vessel between calls
    has none, and that is not an error — the next call's assignment is built
    from the manifest, which now holds the corrected value.
    """
    from app.services.historical_context import active_vessel_call

    call = active_vessel_call(db, vessel)
    if call is None:
        return False

    clauses = [CrewAssignment.vessel_crew_id == crew.id]
    if crew.hp_id and crew.hp_id.strip():
        # Older assignments predate vessel_crew_id being carried across.
        clauses.append(
            func.upper(func.trim(CrewAssignment.hpid)) == crew.hp_id.strip().upper()
        )

    assignment = (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.vessel_call_id == call.id,
            CrewAssignment.ended_at.is_(None),
            or_(*clauses),
        )
        .first()
    )
    if assignment is None:
        return False
    assignment.shore_pass_eligible = bool(crew.shore_pass_eligible)
    return True


def _assignment_for_added_crew(
    db: Session,
    vessel: Vessel,
    crew: VesselCrew,
    *,
    profile: Optional[CrewProfile],
) -> CrewAssignment:
    """Return/create the one active membership for this person and call."""

    from app.services.historical_context import (
        active_vessel_call,
        assignment_for_manifest,
    )

    call = active_vessel_call(db, vessel)
    if call is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This vessel has no active call and crew cannot be added",
        )

    manifest_assignment = (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.vessel_call_id == call.id,
            CrewAssignment.vessel_crew_id == crew.id,
            CrewAssignment.ended_at.is_(None),
        )
        .first()
    )
    if manifest_assignment is not None:
        if profile is not None:
            profile_collision = (
                db.query(CrewAssignment)
                .filter(
                    CrewAssignment.vessel_call_id == call.id,
                    CrewAssignment.crew_profile_id == profile.id,
                    CrewAssignment.ended_at.is_(None),
                    CrewAssignment.id != manifest_assignment.id,
                )
                .first()
            )
            if profile_collision is not None:
                raise _add_crew_conflict(
                    "This crew account is already assigned to the vessel call "
                    "through another manifest entry"
                )
            # A pending manifest can become mapped after the user registers.
            # Link the existing membership; do not create another assignment.
            manifest_assignment.crew_profile_id = profile.id
        return manifest_assignment

    if profile is not None:
        identity_assignment = (
            db.query(CrewAssignment)
            .filter(
                CrewAssignment.vessel_call_id == call.id,
                CrewAssignment.crew_profile_id == profile.id,
                CrewAssignment.ended_at.is_(None),
            )
            .first()
        )
    else:
        passport = normalize_passport_number(crew.passport_number)
        identity_assignment = (
            db.query(CrewAssignment)
            .filter(
                CrewAssignment.vessel_call_id == call.id,
                CrewAssignment.crew_profile_id.is_(None),
                CrewAssignment.ended_at.is_(None),
                normalized_passport_expression(CrewAssignment.passport_number)
                == passport,
            )
            .first()
        )
    if identity_assignment is not None:
        raise _add_crew_conflict(
            "This crew identity is already assigned to the vessel call through "
            "another manifest entry"
        )

    assignment = assignment_for_manifest(db, vessel, crew, profile=profile)
    if assignment is None:  # Defensive; the active call was resolved above.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Unable to create an active crew assignment for this vessel",
        )
    return assignment


def _ensure_crew_shore_pass(
    db: Session,
    *,
    vessel: Vessel,
    assignment: CrewAssignment,
    profile: Optional[CrewProfile],
    port: Optional[str],
    agency_name: Optional[str],
) -> None:
    # A pass is against the assignment -- one person on one call. The profile
    # is the link to an account, and owner answer Q1 is explicit that there
    # may not be one: "an agent can upload the manifest and mark a crew member
    # eligible without that person having an account". This used to return
    # early on a missing profile, so exactly that person got no pass.
    if not is_partnered_agency(agency_name):
        return
    existing = db.query(ShorePass).filter(
        ShorePass.crew_assignment_id == assignment.id,
    ).first()
    if existing is not None:
        return

    call = assignment.vessel_call
    pass_port = call.port_name or port
    pass_vessel = call.vessel_name or vessel.name or "Vessel"
    port_code = (pass_port or "GEN").replace("port_", "")[:3].upper()
    vessel_code = pass_vessel.replace(" ", "")[:3].upper()
    port_display = (pass_port or "General").replace("port_", "").replace("_", " ").title()
    db.add(ShorePass(
        crew_profile_id=profile.id if profile else None,
        crew_assignment_id=assignment.id,
        vessel_call_id=assignment.vessel_call_id,
        agent_name=f"{port_display} Port Authority",
        shore_pass_id=f"SP-{port_code}-{vessel_code}-{uuid.uuid4().hex[:8].upper()}",
        port_name=pass_port,
        vessel_name=pass_vessel,
        is_verified=False,
        status="pending",
    ))


def _refresh_vessel_crew_count(db: Session, vessel: Vessel) -> int:
    """Refresh the legacy count cache from the authoritative current roster."""
    db.flush()
    total = db.query(func.count(VesselCrew.id)).filter(
        VesselCrew.vessel_id == vessel.id
    ).scalar() or 0
    vessel.crew_count = total
    return total


def _save_manifest_rows(db: Session, vessel: Vessel, rows, port: Optional[str]) -> int:
    """Upsert parsed manifest rows onto a vessel's crew list.

    Kept separate from parsing so the agent can review what was read before any
    of it is written. Matching is on passport number or generated HPID, so
    Re-upload is idempotent only for an identical verified identity. Identity
    corrections and reused passports are reconciliation work and are never
    silently overwritten by a bulk import.
    """
    # Serialize every confirm/upload for this vessel before checking for
    # existing manifests or assignments. The database indexes remain the final
    # boundary, while this lock makes concurrent retries deterministic instead
    # of letting one fail after partially building related rows.
    vessel = (
        db.query(Vessel)
        .filter(Vessel.id == vessel.id)
        .with_for_update()
        .one()
    )
    # Validate and resolve the *entire* batch before mutating any manifest,
    # assignment or shore-pass row. Persisting a later identity conflict uses
    # its own commit so the queue item survives the HTTP 409; without this
    # two-phase structure that commit could accidentally make earlier rows in
    # the failed upload permanent.
    prepared = []
    # Rows are resolved against the database before anything is written, so two
    # rows carrying one passport would each look unseen and both be saved. The
    # file itself has to be checked for that.
    seen_passports: dict[str, str] = {}
    for row in rows:
        name = (row.name or "").strip()
        if not name:
            continue
        # The same check registration applies. Until this was shared, the
        # manifest was the wider door: it accepted anything non-empty, so
        # placeholders the sign-up form rejects arrived here instead, and then
        # propagated into generated HPIDs and into the uniqueness rules that
        # assume a passport identifies a person.
        try:
            passport_number = validate_passport_number(row.passport_number)
        except CrewIdentityConflict as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Crew member {name}: {exc}",
            ) from exc

        first = seen_passports.get(passport_number)
        if first is not None:
            also = f" (listed as {first})" if first != name else ""
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Crew member {name}: this passport appears more than once "
                    f"in the upload{also}. Correct the file and upload it again"
                ),
            )
        seen_passports[passport_number] = name
        rank = normalize_rank(row.rank)
        try:
            nationality = normalize_nationality(row.nationality, strict=bool(row.nationality))
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Crew member {name}: {exc}",
            ) from exc

        if nationality is None:
            raise HTTPException(
                status_code=422,
                detail=f"Crew member {name}: Nationality is required",
            )
        generated_hpid = generate_hpid(passport_number, nationality, port)
        profile = _resolve_profile_or_queue_conflict(
            db,
            operation="BULK_MANIFEST",
            vessel=vessel,
            passport_number=passport_number,
            nationality=nationality,
            name=name,
            rank=rank,
            generated_hpid=generated_hpid,
        )

        proposed_identity = {
            "name": name,
            "rank": rank,
            "nationality": nationality,
            "passport_number": passport_number,
            "generated_hpid": generated_hpid,
        }
        crew = _existing_manifest_or_queue_conflict(
            db,
            operation="BULK_MANIFEST",
            vessel=vessel,
            passport_number=passport_number,
            generated_hpid=generated_hpid,
            proposed_identity=proposed_identity,
        )

        if crew is not None and not _same_manifest_identity(
            crew,
            name=name,
            nationality=nationality,
            passport_number=passport_number,
        ):
            message = (
                f"Crew member {name}: This passport is already on the vessel "
                "with different identity details; reconcile it before upload"
            )
            conflict = persist_identity_conflict(
                db,
                operation="BULK_MANIFEST",
                vessel_id=vessel.id,
                passport_number=passport_number,
                proposed_identity=proposed_identity,
                message=message,
            )
            raise HTTPException(status_code=409, detail={
                "message": message,
                "identity_conflict_id": conflict.id,
                "status": conflict.status,
                "version": conflict.version,
            })

        prepared.append({
            "row": row,
            "name": name,
            "rank": rank,
            "nationality": nationality,
            "passport_number": passport_number,
            "generated_hpid": generated_hpid,
            "profile": profile,
            "crew": crew,
        })

    saved = 0
    for item in prepared:
        row = item["row"]
        name = item["name"]
        rank = item["rank"]
        nationality = item["nationality"]
        passport_number = item["passport_number"]
        generated_hpid = item["generated_hpid"]
        profile = item["profile"]
        crew = item["crew"]

        # The shore-pass expiry belongs to the vessel, so uploaded crew inherit
        # it rather than carrying whatever the spreadsheet happened to contain.
        valid_upto = vessel.shore_pass_valid_upto or row.shore_pass_valid_upto

        if not crew:
            crew = VesselCrew(
                vessel_id=vessel.id,
                name=name,
                rank=rank or "",
                nationality=nationality,
                hp_id=(profile.hpid if profile and profile.hpid else generated_hpid),
                passport_number=passport_number,
                status="Mapped" if profile else "Pending",
                shore_pass_eligible=row.shore_pass_eligible,
                shore_pass_valid_upto=valid_upto,
            )
            db.add(crew)
        else:
            crew.rank = rank or crew.rank
            crew.status = "Mapped" if profile else "Pending"
            crew.shore_pass_eligible = row.shore_pass_eligible
            if valid_upto:
                crew.shore_pass_valid_upto = valid_upto

        db.flush()
        assignment = _assignment_for_added_crew(
            db, vessel, crew, profile=profile
        )
        assignment.shore_pass_eligible = bool(crew.shore_pass_eligible)
        agency_name = vessel.agency_name
        if not agency_name and vessel.agent and getattr(vessel.agent, "agent_profile", None):
            agency_name = vessel.agent.agent_profile.agency_name
        _ensure_crew_shore_pass(
            db,
            vessel=vessel,
            assignment=assignment,
            profile=profile,
            port=port,
            agency_name=agency_name,
        )
        saved += 1

    _refresh_vessel_crew_count(db, vessel)
    db.commit()
    return saved


@router.post("/{vessel_id}/crew/manifest/preview", response_model=ManifestPreviewOut)
async def preview_crew_manifest(
    vessel_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Read a manifest and report what was found, without saving anything.

    The agent confirms the list before it reaches the crew record, so a misread
    file — especially a scanned PDF — is caught before it does any damage.
    """
    from app.services.crew_manifest import ManifestError, parse_manifest

    vessel = _agent_vessel_or_404(db, vessel_id, current_user)
    data = await file.read()
    try:
        parsed = parse_manifest(data, file.filename or "")
    except ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if not parsed.crew:
        raise HTTPException(status_code=400, detail="No crew members were found in that file.")

    return ManifestPreviewOut(
        count=len(parsed.crew),
        source=parsed.source,
        warnings=parsed.warnings,
        crew=[ManifestCrewRowOut(**row.model_dump()) for row in parsed.crew],
    )


@router.post("/{vessel_id}/crew/manifest/confirm")
def confirm_crew_manifest(
    vessel_id: int,
    body: ManifestConfirmIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the crew the agent reviewed on the preview screen."""
    vessel = _agent_vessel_or_404(db, vessel_id, current_user)
    if not body.crew:
        raise HTTPException(status_code=400, detail="No crew members to save.")

    agent_profile = getattr(current_user, "agent_profile", None)
    port = agent_profile.assigned_port if agent_profile else None
    saved = _save_manifest_rows(db, vessel, body.crew, port)
    return {"message": f"Saved {saved} crew member{'' if saved == 1 else 's'}.", "saved": saved}


@router.post("/{vessel_id}/crew/upload")
async def upload_crew_manifest(
    vessel_id: int,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Parse and save in one step. Kept for callers that skip the preview."""
    from app.services.crew_manifest import ManifestError, parse_manifest

    vessel = _agent_vessel_or_404(db, vessel_id, current_user)
    data = await file.read()
    try:
        parsed = parse_manifest(data, file.filename or "")
    except ManifestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    agent_profile = getattr(current_user, "agent_profile", None)
    port = agent_profile.assigned_port if agent_profile else None
    saved = _save_manifest_rows(db, vessel, parsed.crew, port)
    return {
        "message": f"Successfully parsed and loaded {saved} crew members.",
        "filename": file.filename,
    }


@router.get("/crew/{hp_id}/profile", response_model=CrewProfileOut)
def get_crew_profile(hp_id: str, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Accepts either identifier form; the scoping below is unchanged.
    hp_id = resolve_crew_identifier(db, hp_id)
    query = db.query(VesselCrew)
    is_superadmin = current_user.role == "superadmin"
    if current_user.role == "agent":
        query = query.join(Vessel, Vessel.id == VesselCrew.vessel_id).filter(
            Vessel.agent_id == current_user.id
        )
    elif not is_superadmin:
        raise HTTPException(status_code=403, detail="Agent access required")
    v_crew = query.filter(VesselCrew.hp_id == hp_id).first()
    if not v_crew:
        raise HTTPException(status_code=404, detail="Crew member not found")

    c_profile = db.query(CrewProfile).filter(CrewProfile.hpid == hp_id).first()

    bookings = []
    visits = []
    if c_profile:
        # Appearing on this agent's current manifest authorises the crew member,
        # not the crew member's whole history. Without the scoping below an agent
        # reads another agency's pickup/drop addresses and port visits.
        booking_q = db.query(CabBooking).filter(CabBooking.crew_id == c_profile.id)
        pass_q = db.query(ShorePass).filter(ShorePass.crew_profile_id == c_profile.id)
        if not is_superadmin:
            from app.db.models.agent_profile import AgentProfile
            from app.db.models.vessel_call import VesselCall
            agency_id = db.query(AgentProfile.id).filter(
                AgentProfile.user_id == current_user.id
            ).scalar()
            # Same rule as _agent_incident_filter: match the immutable agency
            # stamp only. A null stamp is not necessarily missing data -- per the
            # owner decision on historical records it may legitimately mean an
            # unmanaged ("Other") vessel -- but either way the row is not this
            # agency's, so it stays superadmin-only until it is classified.
            booking_q = (
                booking_q.filter(CabBooking.agency_id == agency_id)
                if agency_id is not None
                else None
            )
            # shortcut: shore passes carry no immutable agency stamp, so they are
            # scoped through the pass's own vessel call; passes with no call are
            # excluded. Replace this with an agency stamp once the shore-pass
            # schema question is settled.
            pass_q = (
                pass_q.join(VesselCall, VesselCall.id == ShorePass.vessel_call_id)
                .join(Vessel, Vessel.id == VesselCall.vessel_id)
                .filter(Vessel.agent_id == current_user.id)
            )
        if booking_q is not None:
            bookings = page_all(
                booking_q.order_by(CabBooking.created_at.desc(), CabBooking.id.desc()),
                collection="routes_vessels.get_crew_profile.0",
                label="Crew profile",
            )
        shore_passes = page_all(
            pass_q.order_by(ShorePass.created_at.desc(), ShorePass.id.desc()),
            collection="routes_vessels.get_crew_profile.1",
            label="Crew profile",
        )
        visits = [sp.port_name for sp in shore_passes if sp.port_name]

    return {
        "id": v_crew.id,
        "name": v_crew.name,
        "rank": v_crew.rank,
        "hp_id": v_crew.hp_id,
        "status": v_crew.status,
        "visits": visits,
        "bookings": bookings
    }

class VesselOut(BaseModel):
    id: int
    name: str
    imo_number: str
    vessel_type: str
    berth_assignment: Optional[str] = None
    flag: Optional[str] = None
    agency_name: Optional[str] = None
    agent_id: Optional[int] = None
    crew_count: Optional[int] = 0
    total_crew: Optional[int] = 0
    eligible_crew_count: Optional[int] = 0
    crew_ashore_count: Optional[int] = 0
    eta: Optional[datetime] = None
    etd: Optional[datetime] = None
    status: str
    # One expiry for the whole crew of this port call; crew may override.
    shore_pass_valid_upto: Optional[datetime] = None

    class Config:
        from_attributes = True


def vessel_out(vessel: Vessel) -> VesselOut:
    """Serialize lifecycle state from server time without writing during GET."""
    from app.services.vessel_lifecycle import effective_vessel_status

    output = VesselOut.model_validate(vessel)
    agency_name = output.agency_name
    if not agency_name and vessel.agent and getattr(vessel.agent, "agent_profile", None):
        agency_name = vessel.agent.agent_profile.agency_name
    return output.model_copy(
        update={
            "agency_name": agency_name,
            "crew_count": vessel.total_crew,
            "total_crew": vessel.total_crew,
            "status": effective_vessel_status(vessel),
        }
    )

class VesselPublicOut(BaseModel):
    id: int
    name: str
    agency_name: Optional[str] = "Other"
    has_partnered_agency: bool = False
    # The port the vessel is currently calling at. Crew pick a vessel by name,
    # and two ships in different ports can share one, so the caller needs this
    # to tell them apart.
    port_code: Optional[str] = None
    port_name: Optional[str] = None
    # Active, Departing or Departed. Shown beside the name so a crew member
    # picking a ship that has sailed knows that is what they are picking.
    status: Optional[str] = None

    class Config:
        from_attributes = True

# --- Routes ---

def _vessel_by_imo(db: Session, imo_number):
    """The canonical vessel for an IMO, compared without spacing or case.

    Agents type the IMO by hand and it arrives as "9617741", "IMO 9617741" or
    " 9617741 ". Comparing raw strings would miss the match and let the unique
    constraint reject the insert instead.
    """
    compact = "".join((imo_number or "").upper().split())
    if not compact:
        return None

    # Match the value as typed, and the same value with an "IMO" prefix added
    # or removed — but only when what remains is the number itself. Stripping
    # the three letters unconditionally would corrupt any identifier that
    # merely begins with them rather than being prefixed by them.
    candidates = {compact}
    if compact.startswith("IMO"):
        rest = compact[3:].lstrip("-:.")
        if rest.isdigit():
            candidates.add(rest)
    elif compact.isdigit():
        candidates.add(f"IMO{compact}")

    return (
        db.query(Vessel)
        .filter(
            func.replace(func.upper(func.trim(Vessel.imo_number)), " ", "").in_(
                sorted(candidates)
            )
        )
        .first()
    )


def _start_return_call(db: Session, vessel: Vessel, body, *, agent_id, agency_name):
    """Open a fresh port call for a vessel that has sailed and come back.

    The vessel master keeps its identity — same row, same id, same history. The
    voyage details belong to the new call and are refreshed from the request.

    Overlapping open calls are refused. Two live calls for one hull would split
    its crew, trips and reports across both with nothing to say which is
    current, so an agent who submits twice is told what is already open rather
    than quietly given a duplicate.
    """
    from app.services.historical_context import active_vessel_call
    from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

    open_call = active_vessel_call(db, vessel, create=False)
    if open_call is not None:
        started = open_call.started_at.strftime("%d %b %Y") if open_call.started_at else "an earlier date"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{vessel.name} (IMO {vessel.imo_number}) already has an open "
                f"port call at {open_call.port_name or 'this port'} since "
                f"{started}. Close that call before starting another."
            ),
        )

    # The previous voyage's crew list does not come back with the ship.
    #
    # Opening a call materialises an assignment for every manifest row the
    # vessel holds, which is right for a reassignment — same voyage, new agency,
    # same people aboard — and wrong for a return visit weeks later, when the
    # crew have rotated. MV JIM MING 82 came back and its new call arrived
    # carrying everyone from the two before it.
    #
    # Clearing them loses no history. A finished call's roster lives in its own
    # crew_assignments rows, which carry their own name, rank, HPID and
    # eligibility rather than reading through to the manifest, and that is what
    # the report and every historical view already read. The foreign key back to
    # the manifest is ON DELETE SET NULL precisely so this is survivable.
    departed_manifest = db.query(VesselCrew).filter(
        VesselCrew.vessel_id == vessel.id
    ).all()
    for row in departed_manifest:
        db.delete(row)
    db.flush()
    # And the count cached beside them. total_crew reads the roster when there
    # is one and falls back to this number when there is not, so a cleared
    # manifest left the returning vessel reporting the crew of the call before
    # it: MV JIM MING 82 was onboarded with the crew field at zero and its
    # management row still read 23.
    _refresh_vessel_crew_count(db, vessel)

    vessel.name = body.name or vessel.name
    vessel.vessel_type = body.vessel_type or vessel.vessel_type
    vessel.berth_assignment = body.berth_assignment
    vessel.flag = body.flag or vessel.flag
    if agency_name:
        vessel.agency_name = agency_name
    vessel.eta = body.eta
    vessel.etd = body.etd
    # Reopening is what makes a call creatable again: active_vessel_call
    # refuses to manufacture one for a departed or unassigned vessel.
    vessel.agent_id = agent_id
    vessel.status = "Active"
    db.flush()

    active_vessel_call(db, vessel)
    synchronize_vessel_lifecycle(db, [vessel])
    db.commit()
    db.refresh(vessel)
    return vessel


@router.post("/", response_model=VesselOut, status_code=status.HTTP_201_CREATED)
def create_vessel(body: VesselIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role not in ["agent", "superadmin"]:
        raise HTTPException(status_code=403, detail="Not authorized to create vessels")
    
    resolved_agency = body.agency_name
    if not resolved_agency and current_user.role == "agent":
        if hasattr(current_user, "agent_profile") and current_user.agent_profile:
            resolved_agency = current_user.agent_profile.agency_name

    # A ship that comes back is the same ship. IMO identifies one canonical
    # vessel for its lifetime, so a return visit reuses that record and opens a
    # new port call against it — it does not create a second vessel, which the
    # unique IMO constraint would refuse anyway with a message that told the
    # agent nothing about why.
    returning = _vessel_by_imo(db, body.imo_number)
    if returning is not None:
        return _start_return_call(
            db, returning, body,
            agent_id=current_user.id, agency_name=resolved_agency,
        )

    vessel = Vessel(
        agent_id=current_user.id,
        name=body.name,
        imo_number=body.imo_number,
        vessel_type=body.vessel_type,
        berth_assignment=body.berth_assignment,
        flag=body.flag,
        agency_name=resolved_agency,
        crew_count=0,
        eta=body.eta,
        etd=body.etd,
        status="Active"
    )
    db.add(vessel)
    try:
        db.flush()
        from app.services.historical_context import active_vessel_call
        from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

        active_vessel_call(db, vessel)
        synchronize_vessel_lifecycle(db, [vessel])
        db.commit()
        db.refresh(vessel)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Vessel IMO possibly already exists")
    
    if not vessel.agency_name and vessel.agent and hasattr(vessel.agent, "agent_profile") and vessel.agent.agent_profile:
        vessel.agency_name = vessel.agent.agent_profile.agency_name
    
    return vessel

@router.patch("/{vessel_id}", response_model=VesselOut)
def update_vessel(vessel_id: int, body: VesselIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    if current_user.role == "agent" and vessel.agent_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this vessel")
        
    vessel.name = body.name
    vessel.imo_number = body.imo_number
    vessel.vessel_type = body.vessel_type
    vessel.berth_assignment = body.berth_assignment
    vessel.flag = body.flag
    if body.agency_name is not None:
        vessel.agency_name = body.agency_name
    
    vessel.eta = body.eta
    vessel.etd = body.etd
    from app.services.historical_context import refresh_active_vessel_call
    from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

    refresh_active_vessel_call(db, vessel)
    synchronize_vessel_lifecycle(db, [vessel])
        
    try:
        db.commit()
        db.refresh(vessel)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail="Vessel IMO possibly already exists")
        
    if not vessel.agency_name and vessel.agent and hasattr(vessel.agent, "agent_profile") and vessel.agent.agent_profile:
        vessel.agency_name = vessel.agent.agent_profile.agency_name

    return vessel

@router.get("/", response_model=List[VesselOut])
def get_vessels(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access their vessels")

    vessels = page_all(
        db.query(Vessel).filter(Vessel.agent_id == current_user.id),
        collection="routes_vessels.get_vessels.0",
        label="Vessels",
    )
    return [vessel_out(vessel) for vessel in vessels]

@router.get("/public", response_model=List[VesselPublicOut])
def get_public_vessels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.db.models.port import Port
    from app.db.models.vessel_call import VesselCall
    from app.services.vessel_lifecycle import effective_vessel_status

    # Where each vessel is. A vessel row carries no port of its own — the port
    # belongs to the call — so this is reported rather than filtered on, and the
    # caller shows it beside the name so two ships sharing one can be told apart.
    #
    # port_id is the reliable side; port_name is the legacy free string written
    # before the foreign key existed, and matches ports.code for current rows.
    open_calls = (
        db.query(VesselCall.vessel_id, Port.code, Port.name, VesselCall.port_name)
        .outerjoin(Port, Port.id == VesselCall.port_id)
        .filter(VesselCall.ended_at.is_(None))
        .order_by(VesselCall.id.asc())
        .all()
    )
    port_of = {}
    for vessel_id, code, name, legacy_name in open_calls:
        # Ascending, so the newest open call is written last and wins. A vessel
        # should only ever have one, but a legacy duplicate must not decide this.
        port_of[vessel_id] = (code or legacy_name, name or legacy_name)

    # Every vessel the platform knows, whoever onboarded it and wherever it is.
    #
    # This used to answer with the caller's port only, and only ships still
    # alongside. Both filters were doing authorisation's job badly: crew could
    # not find their vessel if an agent had not onboarded it, or if it had just
    # departed, and were stuck with nothing to select.
    #
    # Choosing a vessel here has never granted anything — the shore leave card
    # and every booking resolve through the crew assignment — so the list can
    # be honest about what exists and let that decide.
    #
    # Archived is still excluded: it means removed from operations, not sailed.
    vessels = page_all(
        db.query(Vessel),
        collection="routes_vessels.get_public_vessels.1",
        label="Public vessels",
    )
    out = []
    for v in vessels:
        status = effective_vessel_status(v)
        if status == "Archived":
            continue
        vessel_port_code, vessel_port_name = port_of.get(v.id, (None, None))
        agency = v.agency_name
        if not agency and v.agent and hasattr(v.agent, "agent_profile") and v.agent.agent_profile:
            agency = v.agent.agent_profile.agency_name
        if not agency:
            agency = "Other"

        has_partnered = is_partnered_agency(agency)
        out.append(VesselPublicOut(
            id=v.id,
            name=v.name,
            agency_name=agency,
            has_partnered_agency=has_partnered,
            port_code=vessel_port_code,
            port_name=vessel_port_name,
            status=status,
        ))
    return out


@router.get("/history/calls")
def get_agent_vessel_call_history(
    limit: int = 100,
    offset: int = 0,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Departed/archived calls remain visible after a vessel leaves the roster."""
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can access vessel history")
    if not 1 <= limit <= 500 or offset < 0:
        raise HTTPException(status_code=422, detail="Invalid history pagination")

    from app.db.models.agent_profile import AgentProfile
    from app.db.models.cab_booking import CabBooking
    from app.db.models.crew_sos import CrewSos
    from app.db.models.incident import Incident
    from app.db.models.report_snapshot import ReportSnapshot
    from app.db.models.vessel_call import VesselCall
    from app.services.vessel_lifecycle import effective_vessel_status

    agency_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == current_user.id
    ).scalar()
    if agency_id is None:
        return []

    calls = (
        db.query(VesselCall)
        .outerjoin(Vessel, VesselCall.vessel_id == Vessel.id)
        .filter(
            VesselCall.agency_id == agency_id,
            or_(
                VesselCall.ended_at.isnot(None),
                VesselCall.status.in_(["DEPARTED", "ARCHIVED", "REASSIGNED"]),
                Vessel.etd <= func.now(),
            ),
        )
        .order_by(
            VesselCall.ended_at.is_(None),
            VesselCall.ended_at.desc(),
            VesselCall.id.desc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    call_ids = [call.id for call in calls]

    def _counts(model):
        if not call_ids:
            return {}
        return dict(
            db.query(model.vessel_call_id, func.count(model.id))
            .filter(model.vessel_call_id.in_(call_ids))
            .group_by(model.vessel_call_id)
            .all()
        )

    trip_counts = _counts(CabBooking)
    incident_counts = _counts(Incident)
    sos_counts = _counts(CrewSos)
    report_counts = _counts(ReportSnapshot)
    output = []
    for call in calls:
        call_status = call.status
        if (
            call.ended_at is None
            and call.vessel is not None
            and effective_vessel_status(call.vessel) == "Departed"
        ):
            call_status = "DEPARTED"
        output.append({
            "vessel_call_id": call.id,
            "vessel_id": call.vessel_id,
            "vessel_name": call.vessel_name,
            "imo_number": call.imo_number,
            "flag": call.flag,
            "port_name": call.port_name,
            "eta": call.eta,
            "etd": call.etd,
            "started_at": call.started_at,
            "ended_at": call.ended_at,
            "status": call_status,
            "trip_count": trip_counts.get(call.id, 0),
            "incident_count": incident_counts.get(call.id, 0),
            "sos_count": sos_counts.get(call.id, 0),
            "report_count": report_counts.get(call.id, 0),
        })
    return output

@router.get("/{vessel_id}", response_model=VesselOut)
def get_vessel_details(vessel_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    from app.services.vessel_lifecycle import synchronize_vessel_lifecycle

    if synchronize_vessel_lifecycle(db, [vessel]):
        db.commit()
        db.refresh(vessel)
    return vessel

@router.get("/{vessel_id}/crew", response_model=List[CrewMemberOut])
def get_crew_manifest(vessel_id: int, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    query = db.query(Vessel).filter(Vessel.id == vessel_id)
    if current_user.role == "agent":
        query = query.filter(Vessel.agent_id == current_user.id)
    elif current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Only agents or superadmins can view crew manifests")
    vessel = query.first()
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")

    # The reference lives on the assignment, not the manifest row. One query
    # for the whole roster rather than one per crew member.
    #
    # Deliberately not restricted to open assignments: this screen shows the
    # manifest of a port call that may already have ended, and the reference is
    # that call's identifier. Filtering on ended_at left a departed vessel's
    # roster with no references at all. Most recent assignment wins where a
    # manifest row has served more than one call.
    manifest = vessel.crew_manifest
    refs = {}
    for crew_id, ref in (
        db.query(CrewAssignment.vessel_crew_id, CrewAssignment.public_ref)
        .filter(CrewAssignment.vessel_crew_id.in_([c.id for c in manifest] or [0]))
        .order_by(CrewAssignment.id.desc())
        .all()
    ):
        refs.setdefault(crew_id, ref)
    return [
        CrewMemberOut.model_validate(crew).model_copy(
            update={"reference": refs.get(crew.id)}
        )
        for crew in manifest
    ]

@router.post("/{vessel_id}/crew", response_model=CrewMemberOut, status_code=status.HTTP_201_CREATED)
def add_crew_member(vessel_id: int, body: CrewMemberIn, current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Serialise additions for one vessel. This makes retries deterministic and
    # closes the check-then-insert race even before the database uniqueness
    # indexes are applied by the release migration.
    vessel = (
        db.query(Vessel)
        .filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id)
        .with_for_update()
        .first()
    )
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")
    
    agent_profile = current_user.agent_profile
    port = agent_profile.assigned_port if agent_profile else None
    
    name = " ".join((body.name or "").strip().split())
    if not name:
        raise HTTPException(status_code=422, detail="Crew name is required")
    passport_number = normalize_passport_number(body.passport_number)
    if not passport_number:
        raise HTTPException(status_code=422, detail="Passport number is required")
    try:
        nationality = normalize_nationality(body.nationality, strict=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if nationality is None:  # Strict normalization is expected to return ISO-2.
        raise HTTPException(status_code=422, detail="Nationality is required")
    generated_hpid = generate_hpid(passport_number, nationality, port)
    profile = _resolve_profile_or_queue_conflict(
        db,
        operation="MANUAL_ADD",
        vessel=vessel,
        passport_number=passport_number,
        nationality=nationality,
        name=name,
        rank=normalize_rank(body.rank),
        generated_hpid=generated_hpid,
    )

    proposed_identity = {
        "name": name,
        "rank": normalize_rank(body.rank),
        "nationality": nationality,
        "passport_number": passport_number,
        "generated_hpid": generated_hpid,
    }
    crew = _existing_manifest_or_queue_conflict(
        db,
        operation="MANUAL_ADD",
        vessel=vessel,
        passport_number=passport_number,
        generated_hpid=generated_hpid,
        proposed_identity=proposed_identity,
    )
    if crew is not None:
        if not _same_manifest_identity(
            crew,
            name=name,
            nationality=nationality,
            passport_number=passport_number,
        ):
            message = (
                "This passport is already on the vessel with different identity "
                "details; update the existing member or reconcile the identity"
            )
            conflict = persist_identity_conflict(
                db,
                operation="MANUAL_ADD",
                vessel_id=vessel.id,
                passport_number=passport_number,
                proposed_identity=proposed_identity,
                message=message,
            )
            raise HTTPException(status_code=409, detail={
                "message": message,
                "identity_conflict_id": conflict.id,
                "status": conflict.status,
                "version": conflict.version,
            })
        # Mapping is server-owned and may legitimately change from Pending to
        # Mapped after the crew member creates an account.
        crew.status = "Mapped" if profile else "Pending"
        if body.shore_pass_eligible is not None:
            crew.shore_pass_eligible = body.shore_pass_eligible
        if body.shore_pass_valid_upto is not None:
            crew.shore_pass_valid_upto = body.shore_pass_valid_upto
    else:
        crew = VesselCrew(
            vessel_id=vessel.id,
            name=name,
            rank=normalize_rank(body.rank) or "other",
            nationality=nationality,
            # A registered account's stable HPID wins over a newly derived HPID.
            hp_id=(profile.hpid if profile and profile.hpid else generated_hpid),
            passport_number=passport_number,
            status="Mapped" if profile else "Pending",
            shore_pass_eligible=(
                body.shore_pass_eligible
                if body.shore_pass_eligible is not None
                else False
            ),
            shore_pass_valid_upto=body.shore_pass_valid_upto,
        )
        db.add(crew)
        db.flush()

    agency_name = vessel.agency_name
    if not agency_name and vessel.agent and hasattr(vessel.agent, "agent_profile") and vessel.agent.agent_profile:
        agency_name = vessel.agent.agent_profile.agency_name
    assignment = _assignment_for_added_crew(db, vessel, crew, profile=profile)
    assignment.shore_pass_eligible = bool(crew.shore_pass_eligible)
    _ensure_crew_shore_pass(
        db,
        vessel=vessel,
        assignment=assignment,
        profile=profile,
        port=port,
        agency_name=agency_name,
    )

    _refresh_vessel_crew_count(db, vessel)
    db.commit()
    db.refresh(crew)
    return crew

@router.patch("/{vessel_id}/crew/{crew_id}/eligibility", response_model=CrewMemberOut)
def update_crew_eligibility(
    vessel_id: int, 
    crew_id: int, 
    body: EligibilityUpdateIn, 
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    if current_user.role == "agent":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found or unauthorized")
    elif current_user.role == "superadmin":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found")
    else:
        raise HTTPException(status_code=403, detail="Only agents or superadmins can toggle eligibility")

    crew = db.query(VesselCrew).filter(VesselCrew.id == crew_id, VesselCrew.vessel_id == vessel.id).first()
    if not crew:
        raise HTTPException(status_code=404, detail="Crew member not found on this vessel")
    
    crew.shore_pass_eligible = body.shore_pass_eligible
    _sync_assignment_eligibility(db, vessel, crew)
    db.commit()
    db.refresh(crew)
    return crew

class ShorePassValidityIn(BaseModel):
    shore_pass_valid_upto: Optional[datetime] = None
    # False leaves crew who already have their own date untouched, so a
    # deliberate individual override is not silently undone.
    apply_to_all: bool = True


class ShorePassValidityOut(BaseModel):
    vessel_id: int
    shore_pass_valid_upto: Optional[datetime] = None
    crew_updated: int


class RosterUnlinkOut(BaseModel):
    action: str
    vessel_id: int
    crew_id: Optional[int] = None


@router.patch("/{vessel_id}/shore-pass-validity", response_model=ShorePassValidityOut)
def set_vessel_shore_pass_validity(
    vessel_id: int,
    body: ShorePassValidityIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Set one shore-pass expiry for the whole crew of this vessel.

    The date belongs to the port call, not to each person, so it is stored on
    the vessel and pushed down to the crew manifest. Individuals can still be
    given a different date afterwards through the per-crew endpoint; that
    override survives until the master date is applied again.
    """
    if current_user.role == "agent":
        vessel = db.query(Vessel).filter(
            Vessel.id == vessel_id, Vessel.agent_id == current_user.id
        ).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found or unauthorized")
    elif current_user.role == "superadmin":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found")
    else:
        raise HTTPException(
            status_code=403,
            detail="Only agents or superadmins can set shore pass validity",
        )

    vessel.shore_pass_valid_upto = body.shore_pass_valid_upto

    crew_query = db.query(VesselCrew).filter(VesselCrew.vessel_id == vessel.id)
    if not body.apply_to_all:
        crew_query = crew_query.filter(VesselCrew.shore_pass_valid_upto.is_(None))

    crew_updated = crew_query.update(
        {VesselCrew.shore_pass_valid_upto: body.shore_pass_valid_upto},
        synchronize_session=False,
    )
    db.commit()

    return ShorePassValidityOut(
        vessel_id=vessel.id,
        shore_pass_valid_upto=body.shore_pass_valid_upto,
        crew_updated=crew_updated or 0,
    )


class CrewMemberUpdate(BaseModel):
    name: Optional[str] = None
    rank: Optional[str] = None
    nationality: Optional[str] = None
    passport_number: Optional[str] = None
    shore_pass_eligible: Optional[bool] = None
    shore_pass_valid_upto: Optional[datetime] = None

@router.patch("/{vessel_id}/crew/{crew_id}", response_model=CrewMemberOut)
def update_crew_member(
    vessel_id: int,
    crew_id: int,
    body: CrewMemberUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if current_user.role == "agent":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found or unauthorized")
    elif current_user.role == "superadmin":
        vessel = db.query(Vessel).filter(Vessel.id == vessel_id).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found")
    else:
        raise HTTPException(status_code=403, detail="Only agents or superadmins can update crew members")

    crew = db.query(VesselCrew).filter(VesselCrew.id == crew_id, VesselCrew.vessel_id == vessel.id).first()
    if not crew:
        raise HTTPException(status_code=404, detail="Crew member not found on this vessel")
    
    if body.name is not None:
        crew.name = body.name
    if body.rank is not None:
        crew.rank = normalize_rank(body.rank) or crew.rank
    if body.nationality is not None:
        try:
            crew.nationality = normalize_nationality(body.nationality, strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.passport_number is not None:
        crew.passport_number = body.passport_number.strip().upper()
    if body.shore_pass_eligible is not None:
        crew.shore_pass_eligible = body.shore_pass_eligible
    if body.shore_pass_valid_upto is not None:
        crew.shore_pass_valid_upto = body.shore_pass_valid_upto

    if body.shore_pass_eligible is not None:
        _sync_assignment_eligibility(db, vessel, crew)

    db.commit()
    db.refresh(crew)
    return crew


@router.delete("/{vessel_id}/assignment", response_model=RosterUnlinkOut)
def unlink_vessel_from_agent(
    vessel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a vessel from an agent roster without deleting platform data."""
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can remove vessel assignments")

    vessel = db.query(Vessel).filter(
        Vessel.id == vessel_id,
        Vessel.agent_id == current_user.id,
    ).first()
    if not vessel:
        # Missing and somebody else's vessel are intentionally indistinguishable.
        raise HTTPException(status_code=404, detail="Vessel not found")

    from app.db.models.agent_roster_event import AgentRosterEvent
    from app.services.historical_context import finish_vessel_call

    db.add(AgentRosterEvent(
        actor_user_id=current_user.id,
        vessel_id=vessel.id,
        action="VESSEL_UNLINKED",
        subject_name=vessel.name,
    ))
    finish_vessel_call(db, vessel, status="ARCHIVED")
    vessel.agent_id = None
    vessel.status = "Archived"
    db.commit()
    return RosterUnlinkOut(action="vessel_unlinked", vessel_id=vessel.id)


@router.delete("/{vessel_id}/crew/{crew_id}", response_model=RosterUnlinkOut)
def unlink_crew_from_vessel(
    vessel_id: int,
    crew_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove one manifest association while preserving account and history."""
    if current_user.role == "agent":
        vessel = (
            db.query(Vessel)
            .filter(Vessel.id == vessel_id, Vessel.agent_id == current_user.id)
            .with_for_update()
            .first()
        )
    elif current_user.role == "superadmin":
        vessel = (
            db.query(Vessel)
            .filter(Vessel.id == vessel_id)
            .with_for_update()
            .first()
        )
    else:
        raise HTTPException(
            status_code=403, detail="Only agents or superadmins can remove crew"
        )
    if not vessel:
        raise HTTPException(status_code=404, detail="Vessel not found")

    crew = db.query(VesselCrew).filter(
        VesselCrew.id == crew_id,
        VesselCrew.vessel_id == vessel.id,
    ).first()
    if not crew:
        raise HTTPException(status_code=404, detail="Crew member not found")

    from app.db.models.agent_roster_event import AgentRosterEvent
    from app.services.historical_context import end_manifest_assignment

    db.add(AgentRosterEvent(
        actor_user_id=current_user.id,
        vessel_id=vessel.id,
        crew_manifest_id=crew.id,
        action="CREW_UNLINKED",
        subject_name=crew.name,
        subject_hpid=crew.hp_id,
    ))
    end_manifest_assignment(db, crew)
    db.delete(crew)
    _refresh_vessel_crew_count(db, vessel)
    db.commit()
    return RosterUnlinkOut(
        action="crew_unlinked", vessel_id=vessel.id, crew_id=crew_id
    )
