"""Release 1 acceptance tests for immutable vessel and agency ownership."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

import app.db.base  # noqa: F401
from app.api.v1.routes_sos import get_sos_timeline, list_sos_requests
from app.api.v1.routes_incidents import (
    agent_incident_detail,
    agent_incident_list,
    agent_safety_report_records,
)
from app.api.v1.routes_agents import get_dashboard_data
from app.api.v1.routes_vessels import get_agent_vessel_call_history
from app.api.v1.routes_superadmin import (
    archive_vessel_superadmin,
    delete_vessel_superadmin,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_sos import CrewSos
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.report_snapshot import ReportSnapshot
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import (
    active_vessel_call,
    assignment_for_manifest,
    end_manifest_assignment,
    event_context,
    finish_vessel_call,
)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def db():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


def _agent(db, agency):
    user = User(
        email=f"{_uniq('agent')}@example.com",
        hashed_password="x",
        role="agent",
    )
    db.add(user)
    db.flush()
    profile = AgentProfile(
        user_id=user.id,
        agency_name=agency,
        location="Test Port",
        assigned_port=None,
    )
    db.add(profile)
    db.flush()
    return user, profile


def _vessel_assignment(db, agent, profile, crew, name):
    vessel = Vessel(
        agent_id=agent.id,
        name=name,
        imo_number=_uniq("IMO"),
        vessel_type="Bulk Carrier",
        agency_name=profile.agency_name,
        status="Active",
    )
    db.add(vessel)
    db.flush()
    manifest = VesselCrew(
        vessel_id=vessel.id,
        name=crew.full_name,
        rank=crew.rank,
        nationality=crew.nationality,
        hp_id=crew.hpid,
        passport_number=crew.passport_number,
        status="Mapped",
    )
    db.add(manifest)
    db.flush()
    assignment = assignment_for_manifest(db, vessel, manifest, profile=crew)
    return vessel, manifest, assignment


def _sos(db, crew_user, crew, context):
    call = context["vessel_call"]
    row = CrewSos(
        user_id=crew_user.id,
        crew_profile_id=crew.id,
        vessel_call_id=call.id,
        vessel_id=context["vessel_id"],
        agency_id=context["agency_id"],
        crew_assignment_id=context["crew_assignment_id"],
        port_id=context["port_id"],
        context_resolution=context["context_resolution"],
        vessel=call.vessel_name,
        crew_email=crew_user.email,
        status="ACTIVE",
    )
    db.add(row)
    db.flush()
    return row


def _incident(db, crew, context):
    call = context["vessel_call"]
    row = Incident(
        incident_id=_uniq("INC"),
        type=IncidentType.CREW,
        title="Historical incident",
        description="Keep this with the call where it happened.",
        status=IncidentStatus.ACTIVE,
        reporter_name=crew.full_name,
        reporter_id=crew.hpid,
        vessel_call_id=call.id,
        vessel_id=context["vessel_id"],
        agency_id=context["agency_id"],
        crew_profile_id=crew.id,
        crew_assignment_id=context["crew_assignment_id"],
        port_id=context["port_id"],
        context_resolution=context["context_resolution"],
    )
    db.add(row)
    db.flush()
    return row


def _booking(db, crew, context):
    row = CabBooking(
        booking_id=_uniq("CAB"),
        crew_id=crew.id,
        vessel_id=context["vessel_id"],
        vessel_call_id=context["vessel_call"].id,
        crew_assignment_id=context["crew_assignment_id"],
        agency_id=context["agency_id"],
        port_id=context["port_id"],
        context_resolution=context["context_resolution"],
        pickup_address="Port",
        pickup_lat=0,
        pickup_lng=0,
        drop_address="City",
        drop_lat=0,
        drop_lng=0,
        vehicle_type="ac",
        vehicle_name="Sedan",
        estimated_price=100,
        distance_km=5,
        status=BookingStatus.COMPLETED,
    )
    db.add(row)
    db.flush()
    return row


def test_same_crew_two_agencies_keeps_each_sos_with_its_original_call(db):
    agent_a, agency_a = _agent(db, "Agency A")
    agent_b, agency_b = _agent(db, "Agency B")
    crew_user = User(
        email=f"{_uniq('crew')}@example.com",
        hashed_password="x",
        role="crew",
    )
    db.add(crew_user)
    db.flush()
    crew = CrewProfile(
        user_id=crew_user.id,
        full_name="Career Crew",
        rank="able_seaman",
        nationality="IN",
        hpid=_uniq("HP"),
        passport_number=_uniq("PASS"),
    )
    db.add(crew)
    db.flush()

    old_vessel, old_manifest, old_assignment = _vessel_assignment(
        db, agent_a, agency_a, crew, "MV OLD CALL"
    )
    old_context = event_context(db, profile=crew, vessel=old_vessel)
    old_booking = _booking(db, crew, old_context)
    old_sos = _sos(db, crew_user, crew, old_context)
    old_incident = _incident(db, crew, old_context)

    end_manifest_assignment(db, old_manifest)
    finish_vessel_call(db, old_vessel, status="DEPARTED")
    old_vessel.agent_id = None
    db.delete(old_manifest)
    db.flush()

    new_vessel, _new_manifest, new_assignment = _vessel_assignment(
        db, agent_b, agency_b, crew, "MV NEW CALL"
    )
    new_context = event_context(db, profile=crew, vessel=new_vessel)
    new_sos = _sos(db, crew_user, crew, new_context)
    new_incident = _incident(db, crew, new_context)

    assert old_assignment.id != new_assignment.id
    assert old_sos.vessel_call_id != new_sos.vessel_call_id
    assert old_sos.agency_id == agency_a.id
    assert new_sos.agency_id == agency_b.id
    selected_old_context = event_context(db, booking=old_booking, profile=crew)
    assert selected_old_context["vessel_call"].id == old_context["vessel_call"].id
    assert selected_old_context["crew_assignment_id"] == old_assignment.id

    listed_a = list_sos_requests(db=db, current_user=agent_a)
    listed_b = list_sos_requests(db=db, current_user=agent_b)
    assert [row["id"] for row in listed_a] == [old_sos.id]
    assert [row["id"] for row in listed_b] == [new_sos.id]
    with pytest.raises(HTTPException) as denied_history:
        agent_safety_report_records(
            vessel_call_id=old_context["vessel_call"].id,
            db=db,
            current_user=agent_b,
        )
    assert denied_history.value.status_code == 404

    # Historical lists retain the old call, while the live dashboard is scoped
    # to vessels the agency is operating now.
    dashboard_a = get_dashboard_data(db=db, current_user=agent_a)
    dashboard_b = get_dashboard_data(db=db, current_user=agent_b)
    assert dashboard_a.stats.open_sos == 0
    assert dashboard_b.stats.open_sos == 1

    incidents_a = agent_incident_list(db=db, current_user=agent_a)["incidents"]
    incidents_b = agent_incident_list(db=db, current_user=agent_b)["incidents"]
    assert [row["id"] for row in incidents_a] == [old_incident.id]
    assert [row["id"] for row in incidents_b] == [new_incident.id]

    old_report = get_sos_timeline(old_sos.id, db=db, current_user=agent_a)
    assert old_report.vessel_details["name"] == "MV OLD CALL"
    assert old_report.vessel_details["vessel_call_id"] == old_sos.vessel_call_id
    old_incident_report = agent_incident_detail(
        old_incident.id, db=db, current_user=agent_a
    )
    assert old_incident_report["vessel"]["name"] == "MV OLD CALL"
    assert old_incident_report["vessel"]["vessel_call_id"] == old_incident.vessel_call_id


def test_ambiguous_current_assignments_require_a_selected_call(db):
    agent, agency = _agent(db, "One Agency")
    crew_user = User(
        email=f"{_uniq('crew')}@example.com",
        hashed_password="x",
        role="crew",
    )
    db.add(crew_user)
    db.flush()
    crew = CrewProfile(
        user_id=crew_user.id,
        full_name="Two Ships",
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
        # The manifest fixture copies this pair across; without a passport
        # neither assignment links to the profile and there is no ambiguity
        # left to test (HPID spec §4.1).
        passport_number=_uniq("P"),
    )
    db.add(crew)
    db.flush()
    first, _, first_assignment = _vessel_assignment(
        db, agent, agency, crew, "MV FIRST"
    )
    second, _, _ = _vessel_assignment(db, agent, agency, crew, "MV SECOND")

    unresolved = event_context(db, profile=crew)
    selected = event_context(db, profile=crew, vessel=first)

    assert unresolved["context_resolution"] == "unresolved"
    assert unresolved["vessel_call"] is None
    assert selected["vessel_id"] == first.id
    assert selected["crew_assignment_id"] == first_assignment.id
    assert selected["vessel_id"] != second.id


def test_superadmin_remove_archives_vessel_and_preserves_sos(db):
    agent, agency = _agent(db, "Archive Agency")
    crew_user = User(
        email=f"{_uniq('crew')}@example.com",
        hashed_password="x",
        role="crew",
    )
    db.add(crew_user)
    db.flush()
    crew = CrewProfile(
        user_id=crew_user.id,
        full_name="Archive Crew",
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
    )
    db.add(crew)
    db.flush()
    vessel, _, _ = _vessel_assignment(db, agent, agency, crew, "MV ARCHIVE")
    context = event_context(db, profile=crew, vessel=vessel)
    sos = _sos(db, crew_user, crew, context)
    incident = _incident(db, crew, context)
    booking = _booking(db, crew, context)
    snapshot = ReportSnapshot(
        report_kind="incident",
        source_id=incident.id,
        source_reference=incident.incident_id,
        agency_id=agency.id,
        vessel_call_id=context["vessel_call"].id,
        generated_by_user_id=agent.id,
        payload={"vessel": {"name": vessel.name}},
        payload_sha256="0" * 64,
    )
    db.add(snapshot)
    db.flush()

    archive_vessel_superadmin(
        vessel_id=vessel.id,
        db=db,
        current_user=SimpleNamespace(role="superadmin"),
    )

    db.refresh(vessel)
    assert vessel.agent_id is None
    assert vessel.status == "Archived"
    assert db.query(CrewSos).filter(CrewSos.id == sos.id).one().vessel_call_id
    assert db.query(Incident).filter(Incident.id == incident.id).one().vessel_call_id
    assert db.query(CabBooking).filter(CabBooking.id == booking.id).one().vessel_call_id
    assert db.query(ReportSnapshot).filter(ReportSnapshot.id == snapshot.id).one()
    assert sos.vessel_call.ended_at is not None
    history = get_agent_vessel_call_history(current_user=agent, db=db)
    archived = next(
        row for row in history if row["vessel_call_id"] == context["vessel_call"].id
    )
    assert archived["status"] == "ARCHIVED"
    assert archived["trip_count"] == 1
    assert archived["incident_count"] == 1
    assert archived["sos_count"] == 1
    assert archived["report_count"] == 1
    safety_reports = agent_safety_report_records(
        vessel_call_id=context["vessel_call"].id,
        db=db,
        current_user=agent,
    )
    assert safety_reports["vessel"]["name"] == vessel.name
    assert {row["reference"] for row in safety_reports["records"]} == {
        incident.incident_id,
        f"SOS-{sos.id}",
    }


def test_superadmin_hard_delete_is_rejected_and_history_is_unchanged(db):
    agent, agency = _agent(db, "Delete Guard Agency")
    crew_user = User(
        email=f"{_uniq('crew')}@example.com",
        hashed_password="x",
        role="crew",
    )
    db.add(crew_user)
    db.flush()
    crew = CrewProfile(
        user_id=crew_user.id,
        full_name="Delete Guard Crew",
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
    )
    db.add(crew)
    db.flush()
    vessel, _manifest, assignment = _vessel_assignment(
        db, agent, agency, crew, "MV DELETE GUARD"
    )
    call = assignment.vessel_call
    sos = _sos(db, crew_user, crew, event_context(db, profile=crew, vessel=vessel))

    with pytest.raises(HTTPException) as error:
        delete_vessel_superadmin(
            vessel_id=vessel.id,
            db=db,
            current_user=SimpleNamespace(role="superadmin"),
        )

    assert error.value.status_code == 409
    assert db.query(Vessel).filter(Vessel.id == vessel.id).one()
    assert db.query(CrewSos).filter(CrewSos.id == sos.id).one()
    assert call.ended_at is None


def test_deleting_crew_profile_does_not_delete_safety_history(db):
    agent, agency = _agent(db, "Retention Agency")
    crew_user = User(
        email=f"{_uniq('crew')}@example.com",
        hashed_password="x",
        role="crew",
    )
    db.add(crew_user)
    db.flush()
    crew = CrewProfile(
        user_id=crew_user.id,
        full_name="Retained Crew",
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
    )
    db.add(crew)
    db.flush()
    vessel, _, _ = _vessel_assignment(db, agent, agency, crew, "MV RETAIN")
    context = event_context(db, profile=crew, vessel=vessel)
    sos = _sos(db, crew_user, crew, context)
    incident = _incident(db, crew, context)
    booking = _booking(db, crew, context)
    sos_id, incident_id, booking_id = sos.id, incident.id, booking.id

    db.delete(crew)
    db.flush()
    db.expire_all()

    retained_sos = db.query(CrewSos).filter(CrewSos.id == sos_id).one()
    retained_incident = db.query(Incident).filter(Incident.id == incident_id).one()
    retained_booking = db.query(CabBooking).filter(CabBooking.id == booking_id).one()
    assert retained_sos.crew_profile_id is None
    assert retained_incident.crew_profile_id is None
    assert retained_booking.crew_id is None
    assert retained_sos.vessel_call_id == context["vessel_call"].id
    assert retained_incident.vessel_call_id == context["vessel_call"].id
    assert retained_booking.vessel_call_id == context["vessel_call"].id


def test_reassigning_a_vessel_starts_fresh_crew_assignments(db):
    agent_a, agency_a = _agent(db, "First Operator")
    agent_b, agency_b = _agent(db, "Second Operator")
    crew_user = User(
        email=f"{_uniq('crew')}@example.com", hashed_password="x", role="crew"
    )
    db.add(crew_user)
    db.flush()
    crew = CrewProfile(
        user_id=crew_user.id,
        full_name="Transferred Crew",
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
    )
    db.add(crew)
    db.flush()
    vessel, manifest, old_assignment = _vessel_assignment(
        db, agent_a, agency_a, crew, "MV REASSIGNED"
    )

    finish_vessel_call(db, vessel, status="REASSIGNED")
    vessel.agent_id = agent_b.id
    vessel.agency_name = agency_b.agency_name
    new_call = active_vessel_call(db, vessel)
    db.flush()

    assert old_assignment.ended_at is not None
    new_assignment = assignment_for_manifest(db, vessel, manifest, profile=crew)
    assert new_assignment.vessel_call_id == new_call.id
    assert new_assignment.id != old_assignment.id
    assert new_call.agency_id == agency_b.id
