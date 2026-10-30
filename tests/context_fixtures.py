"""Explicit historical context for synthetic fixtures; never infer it in runtime code."""

from datetime import datetime, timezone
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_assignment import CrewAssignment
from app.services.assignment_ref import issue_assignment_ref


def immutable_call(db, vessel, agency_id):
    call = (
        db.query(VesselCall).filter_by(vessel_id=vessel.id, agency_id=agency_id).first()
    )
    if call is None:
        call = VesselCall(
            vessel_id=vessel.id,
            agency_id=agency_id,
            vessel_name=vessel.name,
            eta=vessel.eta,
            etd=vessel.etd,
            started_at=vessel.eta or datetime(2020, 1, 1, tzinfo=timezone.utc),
        )
        db.add(call)
        db.flush()
    for manifest in db.query(VesselCrew).filter_by(vessel_id=vessel.id).all():
        profile = (
            db.query(CrewProfile).filter_by(hpid=manifest.hp_id).first()
            if manifest.hp_id
            else None
        )
        # These rows describe an explicitly historical fixture. Runtime
        # assignment creation intentionally refuses a past/closed port call.
        assignment = db.query(CrewAssignment).filter_by(
            vessel_call_id=call.id, vessel_crew_id=manifest.id).first()
        if assignment is None:
            assignment = CrewAssignment(vessel_call_id=call.id, vessel_crew_id=manifest.id,
                crew_profile_id=profile.id if profile else None,
                crew_name=manifest.name, rank=manifest.rank, nationality=manifest.nationality,
                hpid=manifest.hp_id, passport_number=manifest.passport_number,
                shore_pass_eligible=bool(manifest.shore_pass_eligible),
                public_ref=issue_assignment_ref(db), started_at=call.started_at)
            db.add(assignment)
            db.flush()
    return call
