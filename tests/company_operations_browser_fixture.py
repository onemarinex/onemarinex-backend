"""Loopback-only disposable browser acceptance server; NEVER a deployed app.

Requires DATABASE_URL pointed at the exact company_ops_fixture database and
COMPANY_OPS_BROWSER_FIXTURE=1. Resets synthetic data. SMTP is an in-memory outbox.
Run with the isolation sitecustomize documented in the completion report.
"""

import os
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import inspect
from app.db.session import SessionLocal, engine

if (
    os.environ.get("COMPANY_OPS_BROWSER_FIXTURE") != "1"
    or engine.url.database != "company_ops_fixture"
    or engine.url.host != "127.0.0.1"
    or engine.url.port != 55957
):
    raise RuntimeError(
        "Only the explicitly enabled disposable loopback fixture is allowed"
    )

from test_company_operations_http import OperationsHTTP
from app.api.v1 import (
    routes_agents,
    routes_aggregators,
    routes_drivers,
    routes_vessels,
    routes_users,
    routes_incidents,
    routes_company_administration,
    routes_company_onboarding,
    routes_company_intelligence,
    routes_company_disclosure,
    routes_company_reports,
    routes_company_fleet,
    routes_welfare,
    routes_crew,
    registration,
    routes_ports,
    routes_sos,
    routes_bookings,
)
from app.db.models.agent_profile import AgentProfile
from app.db.models.agent_roster_event import AgentRosterEvent
from app.db.models.port import Port
from app.db.models.shipping_company import CompanyAuditEvent
from app.db.models.user import User
from app.db.models.vessel_call import VesselCall
from app.db.models.crew_sos import CrewSos, CrewSosTimelineEvent
from app.db.models.cab_booking import CabBooking
from app.services import email

fixture = OperationsHTTP()
fixture.setUp()
app = fixture.client.app
from batcha_observer import install as install_qa_observer
install_qa_observer(app)
for router, prefix in [
    (routes_agents.router, "/api/v1/agents"),
    (routes_aggregators.router, "/api/v1/aggregators"),
    (routes_drivers.router, "/api/v1/drivers"),
    (routes_vessels.router, "/api/v1/vessels"),
    (routes_users.router, "/api/v1/users"),
    (routes_crew.router, "/api/v1/crew"),
    (routes_incidents.router, "/api/v1/incidents"),
    (routes_company_administration.router, "/api/v1/shipping-company/administration"),
    (routes_company_onboarding.router, "/api/v1/superadmin/company-onboarding"),
    (routes_company_intelligence.router, "/api/v1/shipping-company/intelligence"),
    (routes_company_disclosure.company_router, "/api/v1/shipping-company"),
    (routes_company_disclosure.admin_router, "/api/v1/superadmin"),
    (routes_company_reports.router, "/api/v1/shipping-company/report-centre"),
    (routes_company_fleet.router, "/api/v1/shipping-company/fleet"),
    (routes_company_fleet.admin_router, "/api/v1/superadmin"),
    (routes_welfare.crew_router, "/api/v1/crew"),
    (routes_welfare.company_router, "/api/v1/shipping-company/welfare-intelligence"),
    (routes_welfare.admin_router, "/api/v1/superadmin/welfare-intelligence"),
    (registration.router, "/api/v1/registration"),
    (routes_ports.router, "/api/v1/ports"),
    (routes_bookings.router, "/api/v1/bookings"),
]:
    app.include_router(router, prefix=prefix)
# Separate synthetic survey identities. Their names do not collide with the
# manifest identity used in the connected agency transport test.
from test_welfare_feedback_http import WelfareHTTP
from test_company_operations_http import NOW
from app.db.models.welfare_feedback import WelfareFeedback
from app.db.models.incident import Incident, IncidentType, IncidentNote

for number in range(5):
    user, assignment = WelfareHTTP.person(fixture, number)
    assignment.crew_name = f"Survey fixture {number}"
    if number:
        fixture.db.add(
            WelfareFeedback(
                user_id=user.id,
                assignment_id=assignment.id,
                vessel_call_id=fixture.call.id,
                vessel_id=fixture.vessel.id,
                port_id=fixture.port.id,
                experience_at=NOW,
                answers={
                    "went_ashore": "NO",
                    "difficulty": "PREVENTED",
                    "barriers": ["transport_unavailable"],
                    "extra_charge": "NOT_REQUESTED",
                },
                message="",
                contact_opt_in=False,
            )
        )
fixture.call.port_name = fixture.port.name
fixture.call.imo_number = fixture.vessel.imo_number
case = Incident(
    incident_id="FIXTURE-CASE",
    type=IncidentType.CREW,
    title="Connected synthetic case",
    description="PRIVATE UNAPPROVED DESCRIPTION",
    vessel_id=fixture.vessel.id,
    vessel_call_id=fixture.call.id,
    agency_id=fixture.agency.id,
    context_resolution="CONFIRMED_AGENCY",
    created_at=NOW,
)
fixture.db.add(case)
fixture.db.flush()
fixture.db.add(
    IncidentNote(
        incident_id=case.id, note="Alice synthetic private note", created_at=NOW
    )
)
# Canonical fleet identities for the real declaration/import browser flow.
from app.db.models.vessel import Vessel
from app.db.models.shipping_company import ShippingCompany, VesselRoleClaim

for number, name in (
    ("9000015", "Fleet fixture one"),
    ("9000027", "Fleet fixture two"),
):
    fixture.db.add(
        Vessel(
            name=name,
            imo_number=number,
            vessel_type="Bulk Carrier",
            status="Active",
            agent_id=fixture.other_agent.id,
        )
    )
fixture.db.flush()
competitor = ShippingCompany(
    legal_name="Private fleet competitor",
    normalized_name="PRIVATE FLEET COMPETITOR",
    status="VERIFIED",
)
fixture.db.add(competitor)
fixture.db.flush()
vessel = fixture.db.query(Vessel).filter_by(imo_number="9000027").one()
fixture.db.add(
    VesselRoleClaim(
        company_id=competitor.id,
        vessel_id=vessel.id,
        role="REGISTERED_OWNER",
        status="AUTO_ACCEPTED",
        effective_start=fixture.call.started_at,
        evidence_reference="Private competitor proof",
    )
)
fixture.db.add(Port(name="Mumbai Fixture"))
# QA prerequisites only: no SOS creation service or notification is invoked.
fixture.db.add(CrewSos(agency_id=fixture.agency2.id, status="ACTIVE",
                       port_name="G03 synthetic port", vessel="G03 synthetic SOS vessel",
                       context_resolution="CONFIRMED_AGENCY"))
fixture.db.commit()
fixture.booking(port_id=fixture.port.id)
fixture.db.close()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:15173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Collection-Pages"],
)

outbox = []
app.include_router(routes_sos.router, prefix="/api/v1/sos")


def capture(to, subject, body, **kwargs):
    outbox.append({"to": to, "subject": subject, "body": body})
    return True


email.is_configured = lambda: True
email.send_email = capture


@app.get("/__fixture/outbox")
def fixture_outbox():
    return outbox


class G03AgentStatusIn(BaseModel):
    email: str
    status: str


@app.get("/__fixture/g03/snapshot")
def g03_snapshot():
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        profiles = db.query(AgentProfile).order_by(AgentProfile.id).all()
        calls = db.query(VesselCall).order_by(VesselCall.id).all()
        company_audit_events = (
            db.query(CompanyAuditEvent).order_by(CompanyAuditEvent.id).all()
        )
        agent_roster_events = (
            db.query(AgentRosterEvent).order_by(AgentRosterEvent.id).all()
        )
        users_by_id = {user.id: user for user in users}
        audit_tables = sorted(
            table
            for table in inspect(db.get_bind()).get_table_names()
            if "audit" in table.lower()
        )
        return {
            "users": [
                {"id": user.id, "email": user.email, "role": user.role}
                for user in users
            ],
            "agent_profiles": [
                {
                    "id": profile.id,
                    "user_id": profile.user_id,
                    "email": users_by_id.get(profile.user_id).email
                    if users_by_id.get(profile.user_id)
                    else None,
                    "agency_name": profile.agency_name,
                    "status": profile.status,
                    "assigned_port": profile.assigned_port,
                }
                for profile in profiles
            ],
            "vessel_calls": [
                {
                    "id": call.id,
                    "vessel_id": call.vessel_id,
                    "vessel_name": call.vessel_name,
                    "agency_name": call.agency_name,
                    "port_id": call.port_id,
                }
                for call in calls
            ],
            "company_audit_events": [
                {
                    "id": event.id,
                    "actor_user_id": event.actor_user_id,
                    "company_id": event.company_id,
                    "target_type": event.target_type,
                    "target_id": event.target_id,
                    "action": event.action,
                    "before_state": event.before_state,
                    "after_state": event.after_state,
                }
                for event in company_audit_events
            ],
            "agent_roster_events": [
                {
                    "id": event.id,
                    "actor_user_id": event.actor_user_id,
                    "vessel_id": event.vessel_id,
                    "action": event.action,
                    "subject_name": event.subject_name,
                    "subject_hpid": event.subject_hpid,
                }
                for event in agent_roster_events
            ],
            "audit_tables": audit_tables,
            "vessels": [{"id": v.id, "name": v.name, "imo_number": v.imo_number,
                          "vessel_type": v.vessel_type, "berth_assignment": v.berth_assignment}
                         for v in db.query(Vessel).order_by(Vessel.id).all()],
            "bookings": [{"id": b.id, "port_id": b.port_id, "agency_id": b.agency_id}
                         for b in db.query(CabBooking).order_by(CabBooking.id).all()],
            "sos": [{"id": s.id, "agency_id": s.agency_id, "status": s.status}
                    for s in db.query(CrewSos).order_by(CrewSos.id).all()],
            "sos_events": [{"id": e.id, "sos_id": e.sos_id, "event_type": e.event_type}
                           for e in db.query(CrewSosTimelineEvent).order_by(CrewSosTimelineEvent.id).all()],
        }
    finally:
        db.close()


@app.post("/__fixture/g03/agent-status")
def g03_agent_status(body: G03AgentStatusIn):
    if body.status not in {"Active", "Inactive", "Suspended"}:
        raise HTTPException(status_code=400, detail="Unsupported fixture status")
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.email == body.email.lower().strip(), User.role == "agent")
            .first()
        )
        if not user:
            raise HTTPException(status_code=404, detail="Fixture agent not found")
        profile = (
            db.query(AgentProfile).filter(AgentProfile.user_id == user.id).first()
        )
        if not profile:
            raise HTTPException(status_code=404, detail="Fixture agent profile not found")
        profile.status = body.status
        db.commit()
        return {
            "user_id": user.id,
            "email": user.email,
            "status": profile.status,
        }
    finally:
        db.close()
