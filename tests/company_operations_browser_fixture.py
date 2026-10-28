"""Loopback-only disposable browser acceptance server; NEVER a deployed app.

Requires DATABASE_URL pointed at the exact company_ops_fixture database and
COMPANY_OPS_BROWSER_FIXTURE=1. Resets synthetic data. SMTP is an in-memory outbox.
Run with the isolation sitecustomize documented in the completion report.
"""

import os
from fastapi.middleware.cors import CORSMiddleware
from app.db.session import engine

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
)
from app.services import email

fixture = OperationsHTTP()
fixture.setUp()
app = fixture.client.app
for router, prefix in [
    (routes_agents.router, "/api/v1/agents"),
    (routes_aggregators.router, "/api/v1/aggregators"),
    (routes_drivers.router, "/api/v1/drivers"),
    (routes_vessels.router, "/api/v1/vessels"),
    (routes_users.router, "/api/v1/users"),
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
fixture.db.commit()
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


def capture(to, subject, body, **kwargs):
    outbox.append({"to": to, "subject": subject, "body": body})
    return True


email.is_configured = lambda: True
email.send_email = capture


@app.get("/__fixture/outbox")
def fixture_outbox():
    return outbox
