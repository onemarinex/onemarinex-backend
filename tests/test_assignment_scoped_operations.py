"""P0 regression coverage for assignment-scoped operational writes."""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.db.base  # noqa: F401
from app.api.v1.routes_crew import (
    CabBookingCreateIn,
    SOSConfigIn,
    _active_sos_booking_for_trip,
    list_eligible_crew_assignments,
    update_sos_config,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.cab_booking import BookingStatus, CabBooking
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.driver_magic_link import DriverMagicLink
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import (
    assignment_for_manifest,
    crew_profile_for_manifest,
)
from app.services.magic_link_service import create_or_refresh_magic_link


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


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


def _crew(db):
    user = User(email=f"{_uniq('crew')}@example.com", hashed_password="x", role="crew")
    db.add(user)
    db.flush()
    profile = CrewProfile(
        user_id=user.id,
        full_name="Concurrent Crew",
        rank="officer",
        nationality="IN",
        hpid=_uniq("HP"),
        # Passport and nationality are what link a manifest row to this
        # profile (HPID spec §4.1); the HPID is no longer a key.
        passport_number=_uniq("P"),
    )
    db.add(profile)
    db.flush()
    return user, profile


def _assignment(db, profile, suffix):
    agent = User(email=f"{_uniq('agent')}@example.com", hashed_password="x", role="agent")
    db.add(agent)
    db.flush()
    agency = AgentProfile(
        user_id=agent.id, agency_name=f"Agency {suffix}", location="Port"
    )
    vessel = Vessel(
        agent_id=agent.id,
        name=f"MV {suffix}",
        imo_number=_uniq("IMO"),
        vessel_type="Cargo",
        status="Active",
    )
    db.add_all([agency, vessel])
    db.flush()
    manifest = VesselCrew(
        vessel_id=vessel.id,
        name=profile.full_name,
        rank=profile.rank,
        nationality=profile.nationality,
        hp_id=profile.hpid,
        passport_number=profile.passport_number,
    )
    db.add(manifest)
    db.flush()
    return assignment_for_manifest(db, vessel, manifest, profile=profile)


def _booking(db, profile, assignment, status=BookingStatus.ON_TRIP):
    call = assignment.vessel_call
    row = CabBooking(
        booking_id=_uniq("CAB"),
        crew_id=profile.id,
        vessel_id=call.vessel_id,
        vessel_call_id=call.id,
        crew_assignment_id=assignment.id,
        agency_id=call.agency_id,
        context_resolution="assignment",
        pickup_address="Port",
        pickup_lat=1,
        pickup_lng=1,
        drop_address="City",
        drop_lat=2,
        drop_lng=2,
        vehicle_type="ac",
        vehicle_name="Sedan",
        estimated_price=100,
        distance_km=5,
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def test_sos_resolves_only_the_exact_active_trip(db):
    _user, profile = _crew(db)
    first = _booking(db, profile, _assignment(db, profile, "A"))
    second = _booking(db, profile, _assignment(db, profile, "B"))

    assert _active_sos_booking_for_trip(db, profile.id, first.booking_id).id == first.id
    assert _active_sos_booking_for_trip(db, profile.id, second.booking_id).id == second.id
    assert _active_sos_booking_for_trip(db, profile.id, "CAB-FOREIGN") is None


def test_terminal_trip_is_not_sos_eligible(db):
    _user, profile = _crew(db)
    completed = _booking(
        db, profile, _assignment(db, profile, "DONE"), BookingStatus.COMPLETED
    )
    assert _active_sos_booking_for_trip(db, profile.id, completed.booking_id) is None


def test_sos_config_requires_assignment_when_multiple_are_active(db):
    user, profile = _crew(db)
    first = _assignment(db, profile, "A")
    _assignment(db, profile, "B")
    actor = SimpleNamespace(id=user.id, role="crew")

    with pytest.raises(HTTPException) as ambiguous:
        update_sos_config(
            SOSConfigIn(sos_email="ship@example.com"), db=db, current_user=actor
        )
    assert ambiguous.value.status_code == 409

    result = update_sos_config(
        SOSConfigIn(
            crew_assignment_id=first.id, sos_email="ship-a@example.com"
        ),
        db=db,
        current_user=actor,
    )
    assert result["crew_assignment_id"] == first.id
    assert first.emergency_email == "ship-a@example.com"


def test_sos_config_without_an_assignment_is_a_conflict_not_500(db):
    user, _profile = _crew(db)
    with pytest.raises(HTTPException) as missing:
        update_sos_config(
            SOSConfigIn(sos_email="ship@example.com"),
            db=db,
            current_user=SimpleNamespace(id=user.id, role="crew"),
        )
    assert missing.value.status_code == 409


def test_booking_idempotency_key_is_unique_per_crew(db):
    _user, profile = _crew(db)
    assignment = _assignment(db, profile, "IDEMPOTENT")
    first = _booking(db, profile, assignment)
    first.client_idempotency_key = "same-client-action"
    first.request_fingerprint = "a" * 64
    db.flush()

    duplicate = _booking(db, profile, assignment)
    duplicate.client_idempotency_key = "same-client-action"
    duplicate.request_fingerprint = "a" * 64
    with pytest.raises(IntegrityError):
        db.flush()


def test_booking_otp_is_server_owned():
    assert "otp" not in CabBookingCreateIn.model_fields


def test_eligible_assignment_read_does_not_create_transaction_local_ids(db):
    user, profile = _crew(db)
    agent = User(
        email=f"{_uniq('agent')}@example.com", hashed_password="x", role="agent"
    )
    db.add(agent)
    db.flush()
    vessel = Vessel(
        agent_id=agent.id,
        name="MV Legacy Manifest",
        imo_number=_uniq("IMO"),
        vessel_type="Cargo",
        status="Active",
    )
    db.add(vessel)
    db.flush()
    db.add(VesselCrew(
        vessel_id=vessel.id,
        name=profile.full_name,
        rank=profile.rank,
        nationality=profile.nationality,
        hp_id=profile.hpid,
        passport_number=profile.passport_number,
    ))
    db.flush()

    result = list_eligible_crew_assignments(
        db=db, current_user=SimpleNamespace(id=user.id, role="crew")
    )

    assert result["assignments"] == []
    assert db.query(VesselCall).filter(VesselCall.vessel_id == vessel.id).count() == 0
    assert db.query(CrewAssignment).filter(
        CrewAssignment.crew_profile_id == profile.id
    ).count() == 0


def test_manifest_passport_matching_uses_canonical_whitespace(db):
    _user, profile = _crew(db)
    profile.hpid = None
    profile.passport_number = "AB123"
    vessel = Vessel(
        name="MV Normalized",
        imo_number=_uniq("IMO"),
        vessel_type="Cargo",
        status="Active",
    )
    db.add(vessel)
    db.flush()
    manifest = VesselCrew(
        vessel_id=vessel.id,
        name=profile.full_name,
        rank=profile.rank,
        nationality=profile.nationality,
        passport_number="AB 123",
    )
    db.add(manifest)
    db.flush()

    assert crew_profile_for_manifest(db, manifest).id == profile.id


def test_magic_link_retry_returns_same_row_and_token(db):
    _user, profile = _crew(db)
    booking = _booking(db, profile, _assignment(db, profile, "LINK"))
    first = create_or_refresh_magic_link(db, booking, None, [])
    first_id, first_token = first.id, first.token
    second = create_or_refresh_magic_link(db, booking, None, [])

    assert second.id == first_id
    assert second.token == first_token
    assert db.query(DriverMagicLink).filter(
        DriverMagicLink.booking_id == booking.id
    ).count() == 1
