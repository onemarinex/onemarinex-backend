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
]:
    app.include_router(router, prefix=prefix)
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
