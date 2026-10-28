"""Explicit historical context for synthetic fixtures; never infer it in runtime code."""

from datetime import datetime, timezone
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.models.crew_profile import CrewProfile


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
    from app.services.historical_context import assignment_for_manifest

    for manifest in db.query(VesselCrew).filter_by(vessel_id=vessel.id).all():
        profile = (
            db.query(CrewProfile).filter_by(hpid=manifest.hp_id).first()
            if manifest.hp_id
            else None
        )
        assignment_for_manifest(db, vessel, manifest, profile=profile)
    return call
