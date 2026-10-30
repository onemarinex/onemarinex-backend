"""One time-aware entitlement decision for display and new crew operations."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from app.db.models.shore_pass import ShorePass
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.services.assignment_source import is_agent_attested
from app.services.vessel_lifecycle import _aware_utc, call_is_current


@dataclass(frozen=True)
class ShoreLeaveDecision:
    eligible: bool
    status: str
    reason: Optional[str] = None
    valid_until: Optional[datetime] = None


def lock_shore_leave_context(db, assignment_id):
    """Serialize decisions against the same sources in the same lock order.

    populate_existing is essential: a locked SELECT must replace an ORM row
    loaded before another transaction revoked or expired its permission.
    """
    call_id = db.query(CrewAssignment.vessel_call_id).filter(CrewAssignment.id == assignment_id).scalar()
    if call_id is None:
        return None, []
    vessel_id = db.query(VesselCall.vessel_id).filter(VesselCall.id == call_id).scalar()
    if vessel_id is not None:
        db.query(Vessel).filter(Vessel.id == vessel_id).populate_existing().with_for_update().first()
    db.query(VesselCall).filter(VesselCall.id == call_id).populate_existing().with_for_update().first()
    assignment = db.query(CrewAssignment).filter(CrewAssignment.id == assignment_id).populate_existing().with_for_update().first()
    if assignment is None:
        return None, []
    if assignment.vessel_crew_id is not None:
        db.query(VesselCrew).filter(VesselCrew.id == assignment.vessel_crew_id).populate_existing().with_for_update().first()
    passes = db.query(ShorePass).filter(
        ShorePass.crew_assignment_id == assignment.id,
        ShorePass.vessel_call_id == call_id,
    ).order_by(ShorePass.created_at.desc(), ShorePass.id.desc()).populate_existing().with_for_update().all()
    return assignment, passes


def shore_leave_decision(db, assignment, *, call=None, now=None, lock_pass=False, include_pass=True):
    """A manifest's passport expiry is never a shore-pass validity source.

    Individual validity overrides the vessel master; a recorded pass can
    shorten that permission. Missing validity remains unknown. Unmanaged
    self-selection explicitly requires no agency-issued shore permission.
    """
    current = _aware_utc(now) or datetime.now(timezone.utc)
    call = call or (assignment.vessel_call if assignment is not None else None)
    if not call_is_current(call, now=current):
        return ShoreLeaveDecision(False, "NO_ACTIVE_CALL", "No active port call is available. Select your current vessel or contact your agent.")
    if assignment is not None and (
        assignment.ended_at is not None or assignment.signed_off_at is not None
        or (_aware_utc(assignment.started_at) and _aware_utc(assignment.started_at) > current)
    ):
        return ShoreLeaveDecision(False, "NOT_ELIGIBLE", "This vessel assignment is no longer active.")
    if assignment is not None and not is_agent_attested(assignment.source):
        # An unmanaged source cannot bypass a call actually run by an agency.
        agency = (call.agency_name or call.vessel.agency_name or "").strip().lower()
        if agency in {"", "other", "others", "none", "n/a", "other agency"}:
            return ShoreLeaveDecision(True, "NOT_REQUIRED_UNMANAGED")
        return ShoreLeaveDecision(False, "NOT_ELIGIBLE", "An agent-authorized assignment is required for this vessel.")
    if assignment is None:
        return ShoreLeaveDecision(False, "PENDING", "Your agent has not added you to this port call's crew list.")
    manifest = assignment.vessel_crew
    if not assignment.shore_pass_eligible or (manifest is not None and not manifest.shore_pass_eligible):
        return ShoreLeaveDecision(False, "NOT_ELIGIBLE", "Your agent has not authorized shore leave for this port call.")
    validity = _aware_utc(manifest.shore_pass_valid_upto if manifest and manifest.shore_pass_valid_upto is not None else call.vessel.shore_pass_valid_upto)
    pass_query = db.query(ShorePass).filter(
        ShorePass.crew_assignment_id == assignment.id,
        ShorePass.vessel_call_id == call.id,
    ).order_by(ShorePass.created_at.desc(), ShorePass.id.desc()).populate_existing()
    if lock_pass:
        pass_query = pass_query.with_for_update()
    latest_pass = pass_query.first() if include_pass else None
    if latest_pass is not None:
        if str(latest_pass.status or "").lower() in {"rejected", "revoked", "cancelled"}:
            return ShoreLeaveDecision(False, "NOT_ELIGIBLE", "Your shore pass was rejected or revoked. Contact your agent.")
        pass_end = _aware_utc(latest_pass.expires_at)
        if pass_end is not None:
            validity = min(validity, pass_end) if validity is not None else pass_end
    if validity is None:
        return ShoreLeaveDecision(False, "VALIDITY_UNKNOWN", "Shore-pass validity has not been confirmed. Contact your agent.")
    if validity <= current:
        return ShoreLeaveDecision(False, "EXPIRED", "Your shore-pass validity has expired. Contact your agent.", validity)
    for departure in (call.etd, call.vessel.etd):
        if departure is not None:
            validity = min(validity, _aware_utc(departure))
    return ShoreLeaveDecision(True, "ELIGIBLE", valid_until=validity)
