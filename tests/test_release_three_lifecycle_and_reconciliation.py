from datetime import datetime, timedelta, timezone
from pathlib import Path
import uuid

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

import app.db.base  # noqa: F401
from app.api.v1.routes_superadmin import (
    HistoricalContextResolutionIn,
    SuperAdminVesselCreate,
    list_all_vessels_superadmin,
    list_unresolved_historical_context,
    reconcile_historical_context,
)
from app.db.models.crew_assignment import CrewAssignment
from app.api.v1.routes_vessels import (
    VesselIn,
    get_agent_vessel_call_history,
    get_public_vessels,
    get_vessels,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.event_context_reconciliation import EventContextReconciliation
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.report_snapshot import ReportSnapshot
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.session import engine
from app.services.historical_context import active_vessel_call, finish_vessel_call
from app.services.report_snapshots import canonical_payload, create_report_snapshot
from app.services.vessel_lifecycle import (
    effective_vessel_status,
    synchronize_vessel_lifecycle,
)


ROOT = Path(__file__).resolve().parents[1]


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


def _agent_and_vessel(db, *, etd=None):
    user = User(email=f"{_uniq('agent')}@example.com", hashed_password="x", role="agent")
    db.add(user)
    db.flush()
    agency = AgentProfile(user_id=user.id, agency_name=_uniq("Agency"), location="Port")
    vessel = Vessel(
        agent_id=user.id,
        name=_uniq("MV"),
        imo_number=_uniq("IMO"),
        vessel_type="Bulk Carrier",
        status="Active",
        etd=etd,
    )
    db.add_all([agency, vessel])
    db.flush()
    call = active_vessel_call(db, vessel)
    if call is None and etd is not None and etd <= datetime.now(timezone.utc):
        # Represent an existing stale row, rather than asking the operational
        # resolver to manufacture an already departed call.
        call = VesselCall(vessel_id=vessel.id, agency_id=agency.id,
            vessel_name=vessel.name, agency_name=agency.agency_name,
            started_at=etd-timedelta(days=1), etd=etd, status="ACTIVE")
        db.add(call)
        db.flush()
    return user, agency, vessel, call


def test_release_three_stays_on_the_single_linear_graph():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision("o6p7q8r9s0t1").down_revision == "n5o6p7q8r9s0"


def test_status_uses_server_time_and_five_hour_departing_window():
    now = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
    vessel = Vessel(status="Active", etd=now + timedelta(hours=6))
    assert effective_vessel_status(vessel, now=now) == "Active"
    # A whole day out is now well clear of departure, where it used to already
    # read as Departing.
    vessel.etd = now + timedelta(hours=24)
    assert effective_vessel_status(vessel, now=now) == "Active"
    vessel.etd = now + timedelta(hours=5)
    assert effective_vessel_status(vessel, now=now) == "Departing"
    vessel.etd = now
    assert effective_vessel_status(vessel, now=now) == "Departed"
    vessel.status = "Archived"
    vessel.etd = now + timedelta(days=100)
    assert effective_vessel_status(vessel, now=now) == "Archived"


def test_departure_finishes_call_at_etd_and_history_remains_agent_visible(db):
    now = datetime.now(timezone.utc)
    agent, agency, vessel, call = _agent_and_vessel(db, etd=now - timedelta(minutes=5))
    changed = synchronize_vessel_lifecycle(db, [vessel], now=now)
    db.flush()
    assert changed
    assert vessel.status == "Departed"
    assert call.status == "DEPARTED"
    assert call.ended_at == vessel.etd

    history = get_agent_vessel_call_history(current_user=agent, db=db)
    row = next(item for item in history if item["vessel_call_id"] == call.id)
    assert row["vessel_name"] == vessel.name
    assert row["status"] == "DEPARTED"
    assert agency.id == call.agency_id


def test_read_models_use_server_time_without_mutating_the_database(db):
    now = datetime.now(timezone.utc)
    agent, _agency, vessel, call = _agent_and_vessel(
        db, etd=now - timedelta(minutes=5)
    )
    db.flush()

    agent_rows = get_vessels(current_user=agent, db=db)
    superadmin_rows = list_all_vessels_superadmin(
        current_user=type("Superadmin", (), {"role": "superadmin"})(),
        db=db,
    )
    public_rows = get_public_vessels(current_user=agent, db=db)
    history = get_agent_vessel_call_history(current_user=agent, db=db)

    assert next(row for row in agent_rows if row.id == vessel.id).status == "Departed"
    assert next(row for row in superadmin_rows if row.id == vessel.id).status == "Departed"
    # The public list now offers departed vessels too, labelled as such, so
    # crew arriving after the paperwork says the ship has gone still have
    # something to select. What this test is about is unchanged: the read
    # models derive that status from server time without writing it.
    assert next(
        row for row in public_rows if row.id == vessel.id
    ).status == "Departed"
    assert next(row for row in history if row["vessel_call_id"] == call.id)["status"] == "DEPARTED"
    assert db.query(Vessel.status).filter(Vessel.id == vessel.id).scalar() == "Active"
    assert call.ended_at is None


def test_agent_history_does_not_expose_another_agencys_call(db):
    agent, _agency, _vessel, own_call = _agent_and_vessel(db)
    other_agent, _other_agency, _other_vessel, other_call = _agent_and_vessel(db)
    finish_vessel_call(db, _vessel, status="DEPARTED")
    finish_vessel_call(db, _other_vessel, status="DEPARTED")
    db.flush()

    history = get_agent_vessel_call_history(current_user=agent, db=db)
    visible_ids = {item["vessel_call_id"] for item in history}
    assert own_call.id in visible_ids
    assert other_call.id not in visible_ids
    assert other_agent.id != agent.id


def test_agent_history_is_bounded_and_paginated(db):
    agent, _agency, vessel, first_call = _agent_and_vessel(db)
    first_end = datetime(2026, 8, 12, 10, tzinfo=timezone.utc)
    second_end = first_end + timedelta(hours=1)
    finish_vessel_call(db, vessel, status="DEPARTED", ended_at=first_end)
    second_call = active_vessel_call(db, vessel, allow_new_call=True)
    finish_vessel_call(db, vessel, status="DEPARTED", ended_at=second_end)
    db.flush()

    first_page = get_agent_vessel_call_history(
        limit=1,
        offset=0,
        current_user=agent,
        db=db,
    )
    second_page = get_agent_vessel_call_history(
        limit=1,
        offset=1,
        current_user=agent,
        db=db,
    )
    assert [item["vessel_call_id"] for item in first_page] == [second_call.id]
    assert [item["vessel_call_id"] for item in second_page] == [first_call.id]


def test_superadmin_vessel_list_does_not_write_lifecycle(db):
    now = datetime.now(timezone.utc)
    _agent, _agency, vessel, call = _agent_and_vessel(
        db,
        etd=now - timedelta(minutes=5),
    )
    admin = User(email=f"{_uniq('admin')}@example.com", hashed_password="x", role="superadmin")
    db.add(admin)
    db.flush()

    list_all_vessels_superadmin(db=db, current_user=admin)
    assert vessel.status == "Active"
    assert call.ended_at is None


def test_manual_reconciliation_updates_context_and_writes_audit(db):
    _agent, agency, vessel, call = _agent_and_vessel(db)
    incident = Incident(
        incident_id=_uniq("INC"),
        type=IncidentType.CREW,
        title="Legacy event",
        description="Needs reviewed historical ownership.",
        status=IncidentStatus.RESOLVED,
        context_resolution="unresolved",
    )
    admin = User(email=f"{_uniq('admin')}@example.com", hashed_password="x", role="superadmin")
    db.add_all([incident, admin])
    db.flush()

    result = reconcile_historical_context(
        record_kind="incident",
        record_id=incident.id,
        body=HistoricalContextResolutionIn(
            vessel_call_id=call.id,
            evidence_type="agency_confirmation",
            evidence_reference="EMAIL-2026-08-12",
            notes="Agency confirmed the vessel and port-call record.",
        ),
        db=db,
        current_user=admin,
    )
    assert result["context"]["vessel_id"] == vessel.id
    assert result["context"]["agency_id"] == agency.id
    audit = db.query(EventContextReconciliation).filter(
        EventContextReconciliation.record_kind == "incident",
        EventContextReconciliation.record_id == incident.id,
    ).one()
    assert audit.previous_context["vessel_call_id"] is None
    assert audit.resolved_context["vessel_call_id"] == call.id
    assert audit.reconciled_by_user_id == admin.id


def test_reconciliation_queue_filters_and_paginates_in_database(db):
    _agent, _agency, _vessel, _call = _agent_and_vessel(db)
    admin = User(email=f"{_uniq('admin')}@example.com", hashed_password="x", role="superadmin")
    incidents = [
        Incident(
            incident_id=_uniq("INC"),
            type=IncidentType.CREW,
            title=f"Legacy event {index}",
            description="Needs reviewed historical ownership.",
            status=IncidentStatus.ACTIVE,
            context_resolution="unresolved",
            created_at=datetime(2099, 1, index + 1, tzinfo=timezone.utc),
        )
        for index in range(3)
    ]
    db.add_all([admin, *incidents])
    db.flush()

    page = list_unresolved_historical_context(
        record_kind="incident",
        record_limit=2,
        record_offset=0,
        vessel_call_limit=1,
        vessel_call_offset=0,
        db=db,
        current_user=admin,
    )
    assert [row["record_id"] for row in page["records"]] == [incidents[2].id, incidents[1].id]
    assert page["record_total"] >= 3
    assert len(page["vessel_calls"]) <= 1
    assert page["vessel_call_total"] >= len(page["vessel_calls"])


def test_ambiguous_reconciliation_candidates_never_guess_and_snapshot_stays_frozen(db):
    """Candidate discovery is read-only; an audited choice cannot rewrite an old PDF."""

    _agent_a, agency_a, vessel_a, call_a = _agent_and_vessel(db)
    _agent_b, _agency_b, _vessel_b, call_b = _agent_and_vessel(db)
    incident = Incident(
        incident_id=_uniq("INC"),
        type=IncidentType.CREW,
        title="Ambiguous historical event",
        description="Two historical vessel calls require reviewed evidence.",
        status=IncidentStatus.ACTIVE,
        context_resolution="unresolved",
    )
    admin = User(
        email=f"{_uniq('admin')}@example.com",
        hashed_password="x",
        role="superadmin",
    )
    db.add_all([incident, admin])
    db.flush()

    frozen_payload = {
        "report": "incident",
        "incident_id": incident.incident_id,
        "vessel": "Unresolved at generation time",
    }
    snapshot = create_report_snapshot(
        db,
        report_kind="incident",
        source_id=incident.id,
        source_reference=incident.incident_id,
        agency_id=agency_a.id,
        vessel_call_id=None,
        generated_by_user_id=admin.id,
        payload=frozen_payload,
    )
    db.flush()
    original_payload = dict(snapshot.payload)
    original_digest = snapshot.payload_sha256

    queue = list_unresolved_historical_context(
        record_kind="incident",
        record_limit=500,
        record_offset=0,
        vessel_call_limit=500,
        vessel_call_offset=0,
        db=db,
        current_user=admin,
    )
    queued = next(row for row in queue["records"] if row["record_id"] == incident.id)
    candidate_ids = {row["id"] for row in queue["vessel_calls"]}
    assert {call_a.id, call_b.id}.issubset(candidate_ids)
    assert queued["current_context"]["vessel_call_id"] is None
    assert incident.vessel_call_id is None
    assert incident.context_resolution == "unresolved"

    reconcile_historical_context(
        record_kind="incident",
        record_id=incident.id,
        body=HistoricalContextResolutionIn(
            vessel_call_id=call_a.id,
            evidence_type="manual_document",
            evidence_reference="SIGNED-MANIFEST-1",
            notes="Signed historical manifest confirms the selected vessel call.",
            expected_context=queued["current_context"],
        ),
        db=db,
        current_user=admin,
    )
    assert incident.vessel_id == vessel_a.id
    assert incident.vessel_call_id == call_a.id
    stored = db.query(ReportSnapshot).filter(ReportSnapshot.id == snapshot.id).one()
    assert stored.payload == original_payload
    assert stored.payload_sha256 == original_digest
    assert canonical_payload(stored.payload)[1] == original_digest


def test_reconciliation_rejects_conflicting_crew_assignment(db):
    _agent, _agency, _vessel, selected_call = _agent_and_vessel(db)
    _other_agent, _other_agency, _other_vessel, assignment_call = _agent_and_vessel(db)
    assignment = CrewAssignment(
        vessel_call_id=assignment_call.id,
        crew_name="Historical Crew",
    )
    incident = Incident(
        incident_id=_uniq("INC"),
        type=IncidentType.CREW,
        title="Contradictory legacy event",
        description="The assignment must be reviewed separately.",
        status=IncidentStatus.ACTIVE,
        crew_assignment_id=None,
        context_resolution="unresolved",
    )
    admin = User(email=f"{_uniq('admin')}@example.com", hashed_password="x", role="superadmin")
    db.add_all([assignment, incident, admin])
    db.flush()
    incident.crew_assignment_id = assignment.id
    db.flush()

    with pytest.raises(HTTPException) as conflict:
        reconcile_historical_context(
            record_kind="incident",
            record_id=incident.id,
            body=HistoricalContextResolutionIn(
                vessel_call_id=selected_call.id,
                evidence_type="agency_confirmation",
                notes="Agency evidence conflicts with the stored crew assignment.",
            ),
            db=db,
            current_user=admin,
        )
    assert conflict.value.status_code == 409
    assert incident.vessel_call_id is None
    assert incident.crew_assignment_id == assignment.id


def test_reconciliation_notes_are_trimmed_before_length_validation():
    body = HistoricalContextResolutionIn(
        vessel_call_id=1,
        evidence_type="manual_document",
        notes="   Confirmed by signed port-call document.   ",
    )
    assert body.notes == "Confirmed by signed port-call document."
    with pytest.raises(ValidationError):
        HistoricalContextResolutionIn(
            vessel_call_id=1,
            evidence_type="manual_document",
            notes="          short          ",
        )


def test_reconciliation_rejects_stale_expected_context(db):
    _agent, _agency, _vessel, call = _agent_and_vessel(db)
    incident = Incident(
        incident_id=_uniq("INC"),
        type=IncidentType.CREW,
        title="Legacy event changed while under review",
        description="The stale review must not overwrite newer ownership.",
        status=IncidentStatus.ACTIVE,
        context_resolution="unresolved",
    )
    admin = User(email=f"{_uniq('admin')}@example.com", hashed_password="x", role="superadmin")
    db.add_all([incident, admin])
    db.flush()

    with pytest.raises(HTTPException) as stale:
        reconcile_historical_context(
            record_kind="incident",
            record_id=incident.id,
            body=HistoricalContextResolutionIn(
                vessel_call_id=call.id,
                evidence_type="manual_document",
                notes="Signed historical document reviewed by the operator.",
                expected_context={
                    "vessel_id": 999999,
                    "vessel_call_id": None,
                    "agency_id": None,
                    "crew_assignment_id": None,
                    "port_id": None,
                    "context_resolution": "unresolved",
                },
            ),
            db=db,
            current_user=admin,
        )

    assert stale.value.status_code == 409
    assert stale.value.detail["current_context"]["vessel_id"] is None
    assert incident.vessel_call_id is None
    assert not db.query(EventContextReconciliation).filter(
        EventContextReconciliation.record_kind == "incident",
        EventContextReconciliation.record_id == incident.id,
    ).count()


def test_vessel_creation_rejects_client_supplied_lifecycle_status():
    vessel = {
        "name": "MV Status Test",
        "imo_number": "IMO-STATUS-TEST",
        "vessel_type": "Bulk Carrier",
    }
    assert VesselIn(**vessel).status == "Active"
    assert SuperAdminVesselCreate(**vessel).status == "Active"
    with pytest.raises(ValidationError):
        VesselIn(**vessel, status="Departed")
    with pytest.raises(ValidationError):
        SuperAdminVesselCreate(**vessel, status="Departing")


def test_agent_cannot_reconcile_historical_context(db):
    agent, _agency, _vessel, call = _agent_and_vessel(db)
    incident = Incident(
        incident_id=_uniq("INC"),
        type=IncidentType.CREW,
        title="Legacy event",
        description="Must remain superadmin-only.",
        status=IncidentStatus.ACTIVE,
        context_resolution="unresolved",
    )
    db.add(incident)
    db.flush()
    with pytest.raises(HTTPException) as denied:
        reconcile_historical_context(
            record_kind="incident",
            record_id=incident.id,
            body=HistoricalContextResolutionIn(
                vessel_call_id=call.id,
                evidence_type="manual_document",
                notes="This should never be accepted from an agent.",
            ),
            db=db,
            current_user=agent,
        )
    assert denied.value.status_code == 403


def test_reconciliation_migration_preserves_audit_on_rollback():
    source = (ROOT / "alembic/versions/o6p7q8r9s0t1_historical_context_reconciliation.py").read_text()
    assert '"event_context_reconciliations"' in source
    assert "ondelete=\"SET NULL\"" in source
    assert "pass" in source.split("def downgrade():", 1)[1]


def test_lifecycle_script_uses_one_clock_and_rolls_back_dry_runs():
    source = (ROOT / "scripts/sync_vessel_lifecycle.py").read_text()
    assert "now = datetime.now(timezone.utc)" in source
    assert "effective_vessel_status(vessel, now=now)" in source
    assert "synchronize_vessel_lifecycle(db, vessels, now=now)" in source
    assert source.count("db.rollback()") >= 3
