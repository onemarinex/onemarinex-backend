"""Cab aggregators have no incident surface. Owner decision, 26 October 2026.

The screens were deleted after being found unrouted since June 2026, and these
endpoints carried matching branches: an aggregator could create an incident
that only they could then read. Nothing else could see into that loop, and
nothing fed it -- `aggregator_id` was set in exactly one place, the
aggregator's own create branch, so a crew member's incident about a cab trip
never reached the provider who ran it. It goes to the agency managing the
vessel, which is where the crew branch routes it.

So the branches are gone. An aggregator gets 403, which is the honest answer,
rather than an empty list that reads like "no incidents yet".
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException, Response
from sqlalchemy.orm import Session
from types import SimpleNamespace

from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.incident import Incident, IncidentStatus, IncidentType
from app.db.models.user import User
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _run(coroutine):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coroutine)


class AggregatorIsRefusedTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        user = User(email=_uniq("agg") + "@example.com", hashed_password="x", role="aggregator")
        self.db.add(user)
        self.db.flush()
        from app.db.models.port import Port

        port = self.db.query(Port).first()
        if port is None:
            port = Port(name=_uniq("Port"))
            self.db.add(port)
            self.db.flush()
        profile = AggregatorProfile(
            user_id=user.id, company_name=_uniq("Cabs"), operating_port_id=port.id,
        )
        self.db.add(profile)
        self.db.flush()

        self.aggregator = SimpleNamespace(id=user.id, role="aggregator")
        self.profile_id = profile.id

        # An incident already stamped with this aggregator, the way the removed
        # create branch would have left one.
        self.incident = Incident(
            incident_id=_uniq("INC"), aggregator_id=profile.id,
            type=IncidentType.DRIVER, title="t", description="d",
            status=IncidentStatus.ACTIVE,
        )
        self.db.add(self.incident)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def test_listing_is_refused_not_answered_with_an_empty_list(self):
        from app.api.v1.routes_incidents import get_incidents

        with self.assertRaises(HTTPException) as caught:
            _run(get_incidents(response=Response(), db=self.db, current_user=self.aggregator))
        self.assertEqual(caught.exception.status_code, 403)

    def test_reading_one_is_refused_even_when_it_carries_their_id(self):
        """The row exists and is stamped with them; they still may not read it."""
        from app.api.v1.routes_incidents import get_incident

        with self.assertRaises(HTTPException) as caught:
            _run(get_incident(self.incident.id, self.db, self.aggregator))
        self.assertEqual(caught.exception.status_code, 403)

    def test_creating_one_is_refused(self):
        from app.api.v1.routes_incidents import IncidentCreate, create_incident

        payload = IncidentCreate(type=IncidentType.DRIVER, title="t", description="d")
        with self.assertRaises(HTTPException) as caught:
            _run(create_incident(payload, db=self.db, current_user=self.aggregator))
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("Not authorized to create incidents", caught.exception.detail)

    def test_no_aggregator_branch_survives_in_the_module(self):
        import pathlib

        source = pathlib.Path("app/api/v1/routes_incidents.py").read_text()
        self.assertNotIn('role == "aggregator"', source)

    def test_the_column_is_kept_so_no_row_loses_its_provenance(self):
        """Removing access is not the same as destroying history."""
        self.assertEqual(self.incident.aggregator_id, self.profile_id)


class OtherRolesAreUnaffectedTests(unittest.TestCase):
    """The removal must not have taken anyone else's access with it."""

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def test_superadmin_still_lists_every_incident(self):
        from app.api.v1.routes_incidents import get_incidents

        rows = _run(get_incidents(
            response=Response(),
            db=self.db, current_user=SimpleNamespace(id=1, role="superadmin"),
        ))
        self.assertIsInstance(rows, list)

    def test_an_agent_with_no_vessels_gets_an_empty_list_not_a_refusal(self):
        """Agents are scoped, not excluded — a different thing from 403."""
        from app.api.v1.routes_incidents import get_incidents

        user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        rows = _run(get_incidents(
            response=Response(),
            db=self.db, current_user=SimpleNamespace(id=user.id, role="agent"),
        ))
        self.assertEqual(rows, [])

    def test_an_unknown_role_is_still_refused(self):
        from app.api.v1.routes_incidents import get_incidents

        with self.assertRaises(HTTPException) as caught:
            _run(get_incidents(
            response=Response(),
                db=self.db, current_user=SimpleNamespace(id=1, role="driver"),
            ))
        self.assertEqual(caught.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
