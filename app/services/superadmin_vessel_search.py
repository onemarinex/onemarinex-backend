"""Bounded Super Admin list predicates matching the read-only vessel serializer."""
from datetime import datetime, timezone
from sqlalchemy import and_, case, func, select
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.agent_profile import AgentProfile
from app.services.vessel_lifecycle import DEPARTING_WINDOW


def display_status_expression(now=None):
    """Equivalent to effective_vessel_status plus vessel_out's last-call rule.

    Archived wins. ETD is authoritative when present; legacy manual departed /
    departing states survive when it is missing. An explicitly closed latest
    call remains historical even if the vessel row was never reconciled.
    """
    now = now or datetime.now(timezone.utc)
    stored = func.lower(func.trim(func.coalesce(Vessel.status, "")))
    base = case(
        (stored == "archived", "Archived"),
        (Vessel.etd <= now, "Departed"),
        (and_(Vessel.etd > now, Vessel.etd <= now + DEPARTING_WINDOW), "Departing"),
        (and_(Vessel.etd.is_(None), stored == "departed"), "Departed"),
        (and_(Vessel.etd.is_(None), stored == "departing"), "Departing"),
        else_="Active",
    )
    latest_end = select(VesselCall.ended_at).where(VesselCall.vessel_id == Vessel.id).order_by(VesselCall.id.desc()).limit(1).correlate(Vessel).scalar_subquery()
    latest_status = select(VesselCall.status).where(VesselCall.vessel_id == Vessel.id).order_by(VesselCall.id.desc()).limit(1).correlate(Vessel).scalar_subquery()
    return case((and_(base.in_(("Active", "Departing")), latest_end.isnot(None)), case((func.upper(latest_status) == "ARCHIVED", "Archived"), else_="Departed")), else_=base)


def searchable_agency_expression():
    agency = select(AgentProfile.agency_name).where(AgentProfile.user_id == Vessel.agent_id).limit(1).correlate(Vessel).scalar_subquery()
    return func.coalesce(func.nullif(Vessel.agency_name, ""), agency, "Other")
