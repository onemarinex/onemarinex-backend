"""Full concurrent-vessel safety matrix for one authenticated crew account."""

import asyncio
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.orm import Session

import app.db.base  # noqa: F401
from app.api.v1.routes_crew import (
    GenerateShorePassIn,
    SOSTriggerIn,
    check_shorepass_eligibility,
    generate_shorepass,
    get_crew_profile,
    get_current_shorepass,
    get_sos_eligibility,
    sync_crew_manifest_helper,
    trigger_sos,
)
from app.api.v1.routes_sos import get_sos_timeline, list_sos_requests
from app.api.v1.routes_incidents import (
    IncidentCreate,
    create_agent_safety_report_snapshot,
    create_incident,
    get_agent_safety_report_snapshot,
    get_incidents,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_sos import CrewSos
from app.db.models.incident import Incident, IncidentType
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import assignment_for_manifest


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _run(coroutine):
    """Run an endpoint coroutine without closing pytest's process-wide loop.

    Several legacy suites call ``asyncio.get_event_loop()`` directly. Using
    ``asyncio.run`` here closes and clears that loop, so otherwise-successful
    tests fail later based only on collection order.
    """

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coroutine)


@pytest.fixture()
def db():
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


def _crew(db, name):
    user = User(
        name=name,
        email=f"{_uniq('crew')}@example.com",
        hashed_password="x",
        role="crew",
    )
    db.add(user)
    db.flush()
    profile = CrewProfile(
        user_id=user.id,
        full_name=name,
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
        passport_number=_uniq("PASS"),
    )
    db.add(profile)
    db.flush()
    return user, profile


def _context(db, crew, agency_name, vessel_name):
    agent = User(
        name=agency_name,
        email=f"{_uniq('agent')}@example.com",
        hashed_password="x",
        role="agent",
    )
    db.add(agent)
    db.flush()
    agency = AgentProfile(user_id=agent.id, agency_name=agency_name, location="Port")
    vessel = Vessel(
        agent_id=agent.id,
        name=vessel_name,
        imo_number=_uniq("IMO"),
        vessel_type="Bulk Carrier",
        agency_name=agency_name,
        status="Active",
    )
    db.add_all([agency, vessel])
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
    assignment.emergency_email = f"safety-{vessel.id}@example.com"
    db.flush()
    return agent, agency, vessel, manifest, assignment


def _trip(db, crew, assignment):
    call = assignment.vessel_call
    booking = CabBooking(
        booking_id=_uniq("CAB"),
        crew_id=crew.id,
        vessel_id=call.vessel_id,
        vessel_call_id=call.id,
        crew_assignment_id=assignment.id,
        agency_id=call.agency_id,
        port_id=call.port_id,
        context_resolution="assignment",
        pickup_address="Port gate",
        pickup_lat=17.7,
        pickup_lng=83.3,
        drop_address="City",
        drop_lat=17.72,
        drop_lng=83.31,
        vehicle_type="ac",
        vehicle_name="Sedan",
        estimated_price=100,
        distance_km=5,
        status=BookingStatus.ON_TRIP,
    )
    db.add(booking)
    db.flush()
    return booking


def test_two_active_vessels_keep_sos_and_history_with_the_selected_trip(db):
    crew_user, crew = _crew(db, "Concurrent Crew")
    agent_a, agency_a, vessel_a, manifest_a, assignment_a = _context(
        db, crew, "Agency Alpha", "MV ALPHA"
    )
    agent_b, agency_b, vessel_b, _manifest_b, assignment_b = _context(
        db, crew, "Agency Bravo", "MV BRAVO"
    )
    trip_a = _trip(db, crew, assignment_a)
    trip_b = _trip(db, crew, assignment_b)

    ambiguous = get_sos_eligibility(db=db, current_user=crew_user)
    assert ambiguous["eligible"] is False
    assert "Select an active trip" in ambiguous["reason"]
    selected = get_sos_eligibility(
        trip_id=trip_a.booking_id, db=db, current_user=crew_user
    )
    assert selected["eligible"] is True
    assert selected["trip_id"] == trip_a.booking_id

    with patch("app.services.email.send_sos_alert"):
        first_result = trigger_sos(
            SOSTriggerIn(trip_id=trip_a.booking_id, lat=17.7, lng=83.3),
            db=db,
            current_user=crew_user,
        )
    first = db.query(CrewSos).filter(CrewSos.id == first_result["id"]).one()
    assert (first.vessel_id, first.vessel_call_id, first.agency_id, first.crew_assignment_id) == (
        vessel_a.id, assignment_a.vessel_call_id, agency_a.id, assignment_a.id,
    )
    assert first.sos_email == assignment_a.emergency_email
    assert [row["id"] for row in list_sos_requests(db=db, current_user=agent_a)] == [first.id]
    assert list_sos_requests(db=db, current_user=agent_b) == []

    first.status = "CANCELLED"
    db.flush()
    with patch("app.services.email.send_sos_alert"):
        second_result = trigger_sos(
            SOSTriggerIn(trip_id=trip_b.booking_id, lat=18.0, lng=84.0),
            db=db,
            current_user=crew_user,
        )
    second = db.query(CrewSos).filter(CrewSos.id == second_result["id"]).one()
    assert (second.vessel_id, second.vessel_call_id, second.agency_id, second.crew_assignment_id) == (
        vessel_b.id, assignment_b.vessel_call_id, agency_b.id, assignment_b.id,
    )
    assert second.sos_email == assignment_b.emergency_email

    # Incidents use the same immutable trip/assignment selection. Each agency
    # sees only its own event, and each downloaded report is frozen against the
    # historical vessel call before either assignment changes.
    incident_a_result = _run(create_incident(
        IncidentCreate(
            type=IncidentType.CREW,
            title="Alpha safety event",
            description="Event on the Alpha trip",
            trip_id=trip_a.booking_id,
            crew_assignment_id=assignment_a.id,
        ),
        db=db,
        current_user=crew_user,
    ))
    incident_b_result = _run(create_incident(
        IncidentCreate(
            type=IncidentType.CREW,
            title="Bravo safety event",
            description="Event on the Bravo trip",
            trip_id=trip_b.booking_id,
            crew_assignment_id=assignment_b.id,
        ),
        db=db,
        current_user=crew_user,
    ))
    incident_a = db.query(Incident).filter(
        Incident.id == incident_a_result["id"]
    ).one()
    incident_b = db.query(Incident).filter(
        Incident.id == incident_b_result["id"]
    ).one()
    assert (
        incident_a.vessel_id,
        incident_a.vessel_call_id,
        incident_a.agency_id,
        incident_a.crew_assignment_id,
    ) == (vessel_a.id, assignment_a.vessel_call_id, agency_a.id, assignment_a.id)
    assert (
        incident_b.vessel_id,
        incident_b.vessel_call_id,
        incident_b.agency_id,
        incident_b.crew_assignment_id,
    ) == (vessel_b.id, assignment_b.vessel_call_id, agency_b.id, assignment_b.id)
    assert [row.id for row in _run(get_incidents(response=Response(), db=db, current_user=agent_a))] == [incident_a.id]
    assert [row.id for row in _run(get_incidents(response=Response(), db=db, current_user=agent_b))] == [incident_b.id]

    snapshot_a = create_agent_safety_report_snapshot(
        "incident", incident_a.id, db=db, current_user=agent_a
    )
    snapshot_b = create_agent_safety_report_snapshot(
        "incident", incident_b.id, db=db, current_user=agent_b
    )
    assert snapshot_a["payload"]["vessel"]["name"] == vessel_a.name
    assert snapshot_b["payload"]["vessel"]["name"] == vessel_b.name
    assert snapshot_a["payload"]["vessel"]["vessel_call_id"] == assignment_a.vessel_call_id
    assert snapshot_b["payload"]["vessel"]["vessel_call_id"] == assignment_b.vessel_call_id
    with pytest.raises(HTTPException) as hidden_report:
        get_agent_safety_report_snapshot(
            snapshot_a["snapshot_id"], db=db, current_user=agent_b
        )
    assert hidden_report.value.status_code == 404
    frozen_a = snapshot_a["payload"]

    # Removing the first membership and changing the display preference must
    # not move or hide the old alert.
    assignment_a.ended_at = assignment_b.started_at
    db.delete(manifest_a)
    crew.vessel = vessel_b.name
    db.flush()
    old_report = get_sos_timeline(first.id, db=db, current_user=agent_a)
    assert old_report.vessel_details["name"] == vessel_a.name
    assert old_report.vessel_details["vessel_call_id"] == assignment_a.vessel_call_id
    assert [row["id"] for row in list_sos_requests(db=db, current_user=agent_a)] == [first.id]
    assert [row["id"] for row in list_sos_requests(db=db, current_user=agent_b)] == [second.id]
    assert [row.id for row in _run(get_incidents(response=Response(), db=db, current_user=agent_a))] == [incident_a.id]
    assert get_agent_safety_report_snapshot(
        snapshot_a["snapshot_id"], db=db, current_user=agent_a
    )["payload"] == frozen_a


def test_exact_trip_sos_does_not_reveal_or_accept_another_crews_booking(db):
    requester, _requester_profile = _crew(db, "Requester")
    _owner_user, owner = _crew(db, "Owner")
    _agent, _agency, _vessel, _manifest, assignment = _context(
        db, owner, "Scoped Agency", "MV PRIVATE"
    )
    foreign_trip = _trip(db, owner, assignment)

    with pytest.raises(HTTPException) as denied:
        trigger_sos(
            SOSTriggerIn(trip_id=foreign_trip.booking_id, lat=17.7, lng=83.3),
            db=db,
            current_user=requester,
        )
    assert denied.value.status_code == 400
    assert "belonging to this crew member" in denied.value.detail
    assert not db.query(CrewSos).filter(CrewSos.trip_id == foreign_trip.booking_id).count()


def test_profile_and_shore_pass_require_and_preserve_exact_assignment(db):
    crew_user, crew = _crew(db, "Two Ship Crew")
    _agent_a, _agency_a, vessel_a, _manifest_a, assignment_a = _context(
        db, crew, "Agency Alpha", "MV ALPHA PROFILE"
    )
    _agent_b, _agency_b, vessel_b, _manifest_b, assignment_b = _context(
        db, crew, "Agency Bravo", "MV BRAVO PROFILE"
    )

    with pytest.raises(HTTPException) as ambiguous_profile:
        get_crew_profile(db=db, current_user=crew_user)
    assert ambiguous_profile.value.status_code == 409
    assert get_crew_profile(
        crew_assignment_id=assignment_a.id, db=db, current_user=crew_user
    ).vessel == vessel_a.name
    assert get_crew_profile(
        crew_assignment_id=assignment_b.id, db=db, current_user=crew_user
    ).vessel == vessel_b.name

    with pytest.raises(HTTPException) as ambiguous_pass:
        check_shorepass_eligibility(db=db, current_user=crew_user)
    assert ambiguous_pass.value.status_code == 409
    eligibility = check_shorepass_eligibility(
        crew_assignment_id=assignment_a.id, db=db, current_user=crew_user
    )
    assert eligibility["crew_assignment_id"] == assignment_a.id
    assert eligibility["agent_name"] == "Agency Alpha"

    with pytest.raises(HTTPException) as missing_port:
        generate_shorepass(
            GenerateShorePassIn(
                crew_assignment_id=assignment_a.id,
                port_name="port_test",
            ),
            db=db,
            current_user=crew_user,
        )
    assert missing_port.value.status_code == 409

    assignment_a.vessel_call.port_name = "port_test"
    db.flush()
    generated = generate_shorepass(
        GenerateShorePassIn(
            crew_assignment_id=assignment_a.id,
            port_name="port_test",
        ),
        db=db,
        current_user=crew_user,
    )
    assert generated.crew_assignment_id == assignment_a.id
    assert generated.vessel_call_id == assignment_a.vessel_call_id
    assert generated.vessel_name == vessel_a.name
    assert get_current_shorepass(
        crew_assignment_id=assignment_a.id, db=db, current_user=crew_user
    ).id == generated.id
    assert get_current_shorepass(
        crew_assignment_id=assignment_b.id, db=db, current_user=crew_user
    ) is None


def test_manifest_sync_does_not_claim_a_different_person_with_same_passport(db):
    _user, profile = _crew(db, "Correct Person")
    profile.passport_number = "SHARED123"
    profile.nationality = "IN"
    _agent, _agency, vessel, manifest, _assignment = _context(
        db, profile, "Correct Agency", "MV CORRECT"
    )
    manifest.passport_number = "SHARED123"

    other_agent = User(
        email=f"{_uniq('other-agent')}@example.com",
        hashed_password="x",
        role="agent",
    )
    db.add(other_agent)
    db.flush()
    other_vessel = Vessel(
        agent_id=other_agent.id,
        name="MV CONFLICT",
        imo_number=_uniq("IMO"),
        vessel_type="Cargo",
        status="Active",
    )
    db.add(other_vessel)
    db.flush()
    conflicting = VesselCrew(
        vessel_id=other_vessel.id,
        name="Different Person",
        rank="master",
        nationality="PH",
        hp_id=_uniq("OTHER-HP"),
        passport_number=" shared123 ",
        status="Pending",
    )
    db.add(conflicting)
    db.flush()

    sync_crew_manifest_helper(profile, db)
    db.refresh(conflicting)

    assert conflicting.status == "Pending"
    assert not db.query(CrewAssignment).filter(
        CrewAssignment.vessel_crew_id == conflicting.id,
        CrewAssignment.crew_profile_id == profile.id,
    ).count()
