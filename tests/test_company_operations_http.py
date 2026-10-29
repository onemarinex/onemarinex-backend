"""Actual HTTP/auth/Postgres acceptance. Run ONLY against company_ops_fixture."""

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.db.models.user import User
from app.db.models.agent_profile import AgentProfile
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.port import Port
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.driver import Driver
from app.db.models.cab_booking import CabBooking
from app.db.models.shipping_company import (
    ShippingCompany,
    CompanyUserMembership,
    VesselRoleClaim,
)
from app.db.models.company_invitation import CompanyInvitation
from app.services.auth import create_access_token, get_password_hash
from app.services.collection_pages import collection_paging
from app.services.event_origin import (
    classify,
    CONFIRMED_AGENCY,
    CONFIRMED_UNMANAGED,
    UNRESOLVED,
    CONFLICTED,
)
from app.api.v1 import (
    routes_shipping_company,
    routes_company_staff,
    routes_agency_transfers,
    routes_auth,
    routes_superadmin,
    routes_bookings,
    routes_trips,
    routes_company_review,
)
from app.services import company_aggregates as agg

UTC = timezone.utc
JAN = datetime(2026, 1, 1, tzinfo=UTC)
JUL = datetime(2026, 7, 1, tzinfo=UTC)
NOW = datetime.now(UTC)

FIXTURE_PASSWORD_HASH = get_password_hash("fixture-only-secret")


class OperationsHTTP(unittest.TestCase):
    def setUp(self):
        if (
            engine.url.database != "company_ops_fixture"
            or engine.url.host != "127.0.0.1"
        ):
            self.skipTest(
                "Requires disposable company_ops_fixture; never run on a shared database"
            )
        with engine.begin() as c:
            names = ",".join(
                '"' + t.name + '"' for t in reversed(Base.metadata.sorted_tables)
            )
            c.execute(text("TRUNCATE " + names + " RESTART IDENTITY CASCADE"))
        self.db = SessionLocal()

        def user(name, role):
            u = User(
                email=name + "@example.com",
                name=name,
                role=role,
                hashed_password=FIXTURE_PASSWORD_HASH,
            )
            self.db.add(u)
            self.db.flush()
            return u

        self.agent = user("agent", "agent")
        self.other_agent = user("otheragent", "agent")
        self.provider_user = user("provider", "aggregator")
        self.other_provider_user = user("otherprovider", "aggregator")
        self.admin = user("admin", "shipping_company")
        self.admin2 = user("admin2", "shipping_company")
        self.sa = user("superadmin", "superadmin")
        self.port = Port(name="Fixture port", code="fixture")
        self.db.add(self.port)
        self.db.flush()
        self.agency = AgentProfile(
            user_id=self.agent.id, agency_name="Agency", location="Fixture"
        )
        self.agency2 = AgentProfile(
            user_id=self.other_agent.id, agency_name="Other", location="Fixture"
        )
        self.db.add_all([self.agency, self.agency2])
        self.db.flush()
        self.provider = AggregatorProfile(
            user_id=self.provider_user.id,
            company_name="Provider",
            operating_port_id=self.port.id,
            status="Active",
        )
        self.provider2 = AggregatorProfile(
            user_id=self.other_provider_user.id,
            company_name="OtherProvider",
            operating_port_id=self.port.id,
            status="Active",
        )
        self.db.add_all([self.provider, self.provider2])
        self.db.flush()
        self.driver = Driver(
            aggregator_id=self.provider.id,
            name="Driver",
            phone="0000000000",
            vehicle_number="FIXTURE",
        )
        self.vessel = Vessel(
            name="Fixture MV",
            imo_number="IMO-FIXTURE",
            vessel_type="Bulk Carrier",
            status="Active",
            agent_id=self.agent.id,
        )
        self.company = ShippingCompany(
            legal_name="Fixture company",
            normalized_name="FIXTURE COMPANY",
            status="VERIFIED",
        )
        self.db.add_all([self.driver, self.vessel, self.company])
        self.db.flush()
        self.call = VesselCall(
            vessel_id=self.vessel.id,
            agency_id=self.agency.id,
            port_id=self.port.id,
            vessel_name=self.vessel.name,
            agency_name="Agency",
            started_at=JAN,
        )
        self.db.add(self.call)
        self.db.flush()
        self.assignment = CrewAssignment(
            vessel_call_id=self.call.id,
            crew_name="Synthetic crew",
            passport_number="P1234567",
            nationality="IN",
            public_ref="HP-FIXTURE",
            source="SIGN_ON",
            started_at=JAN,
        )
        self.db.add(self.assignment)
        self.db.add_all(
            [
                CompanyUserMembership(
                    company_id=self.company.id,
                    user_id=self.admin.id,
                    role="ADMIN",
                    enabled=True,
                ),
                CompanyUserMembership(
                    company_id=self.company.id,
                    user_id=self.admin2.id,
                    role="ADMIN",
                    enabled=True,
                ),
            ]
        )
        self.db.add(
            VesselRoleClaim(
                company_id=self.company.id,
                vessel_id=self.vessel.id,
                role="ISM_MANAGER",
                effective_start=JAN,
                status="APPROVED",
            )
        )
        self.db.commit()
        app = FastAPI()
        from app.services.company_private_responses import company_private_responses
        app.middleware("http")(company_private_responses)
        app.middleware("http")(collection_paging)
        for router, prefix in [
            (routes_shipping_company.router, "/api/v1/shipping-company"),
            (routes_company_staff.router, "/api/v1/shipping-company"),
            (routes_agency_transfers.router, "/api/v1/agency-transfers"),
            (routes_agency_transfers.public_router, "/api/v1/agency-transfer-trip"),
            (routes_auth.router, "/api/v1/auth"),
            (routes_superadmin.router, "/api/v1/superadmin"),
            (routes_company_review.router, "/api/v1/superadmin"),
        ]:
            app.include_router(router, prefix=prefix)
        self.client = TestClient(app)

    def tearDown(self):
        if hasattr(self, "db"):
            self.db.close()
            self.client.close()

    def headers(self, u):
        return {"Authorization": "Bearer " + create_access_token(u.email)}

    def request(self, method, path, actor=None, body=None, status=200):
        headers = self.headers(actor) if actor else {}
        if path.startswith("/api/v1/agency-transfer-trip/"):
            headers["X-Transfer-Token"] = path.rsplit("/", 1)[1]
            path = "/api/v1/agency-transfer-trip"
        response = self.client.request(method, path, headers=headers, json=body)
        self.assertEqual(response.status_code, status, response.text)
        return response

    def transfer(self):
        return self.request(
            "POST",
            "/api/v1/agency-transfers",
            self.agent,
            {
                "vessel_call_id": self.call.id,
                "provider_id": self.provider.id,
                "assignment_ids": [self.assignment.id],
                "movement_type": "SIGN_ON",
                "pickup": "Airport",
                "destination": "Port gate",
                "travel_at": (NOW + timedelta(hours=2)).isoformat(),
            },
            201,
        ).json()

    def action(self, t, actor, action, status=200, **kw):
        return self.request(
            "POST",
            f"/api/v1/agency-transfers/{t['id']}/actions",
            actor,
            {"action": action, **kw},
            status,
        ).json()

    def booking(self, **kw):
        body = dict(
            booking_id="FIX-" + str(self.db.query(CabBooking).count()),
            vessel_id=self.vessel.id,
            vessel_call_id=self.call.id,
            agency_id=self.agency.id,
            context_resolution=CONFIRMED_AGENCY,
            pickup_address="Gate",
            pickup_lat=0,
            pickup_lng=0,
            drop_address="City",
            drop_lat=0,
            drop_lng=0,
            vehicle_type="ac",
            vehicle_name="Sedan",
            estimated_price=0,
            distance_km=1,
            status="completed",
            num_passengers=3,
            created_at=NOW,
            trip_completed_at=NOW,
        )
        body.update(kw)
        b = CabBooking(**body)
        self.db.add(b)
        self.db.commit()
        return b

    def test_complete_transport_is_exclusive_and_does_not_confirm_movement(self):
        t = self.transfer()
        self.assertEqual(
            self.request(
                "GET", "/api/v1/agency-transfers", self.other_provider_user
            ).json(),
            [],
        )
        self.action(t, self.other_provider_user, "accept", 404)
        self.action(t, self.provider_user, "accept")
        assigned = self.action(
            t, self.provider_user, "assign", driver_id=self.driver.id
        )
        token = assigned["driver_token"]
        public = self.request("GET", "/api/v1/agency-transfer-trip/" + token).json()
        self.assertEqual(
            set(public),
            {"id", "pickup", "destination", "travel_at", "passenger_count", "status"},
        )
        self.request(
            "POST",
            "/api/v1/agency-transfer-trip/" + token,
            body={"action": "complete"},
            status=409,
        )
        self.request(
            "POST", "/api/v1/agency-transfer-trip/" + token, body={"action": "start"}
        )
        self.action(
            t,
            self.agent,
            "reassign",
            409,
            provider_id=self.provider2.id,
            reason="Changed provider",
        )
        self.request(
            "POST", "/api/v1/agency-transfer-trip/" + token, body={"action": "complete"}
        )
        self.request("GET", "/api/v1/agency-transfer-trip/" + token, status=404)
        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.signed_on_at)
        timeline = self.request(
            "GET", f"/api/v1/agency-transfers/{t['id']}/audit", self.agent
        ).json()
        self.assertEqual(len(timeline), 5)

    def test_reassignment_revokes_provider_and_driver_access(self):
        t = self.transfer()
        self.action(t, self.provider_user, "accept")
        old = self.action(t, self.provider_user, "assign", driver_id=self.driver.id)[
            "driver_token"
        ]
        self.action(
            t,
            self.agent,
            "reassign",
            provider_id=self.provider2.id,
            reason="Flight changed",
        )
        self.request("GET", "/api/v1/agency-transfer-trip/" + old, status=404)
        self.action(t, self.provider_user, "accept", 404)
        self.assertEqual(
            len(
                self.request(
                    "GET", "/api/v1/agency-transfers", self.other_provider_user
                ).json()
            ),
            1,
        )

    def test_cancel_and_role_restrictions(self):
        t = self.transfer()
        self.action(t, self.agent, "accept", 403)
        self.action(t, self.agent, "cancel", 422)
        self.action(t, self.agent, "cancel", reason="Withdrawn")
        self.action(t, self.provider_user, "accept", 409)
        self.request("GET", "/api/v1/agency-transfers", self.admin, status=403)
        self.request("GET", "/api/v1/agency-transfers", status=401)

    def test_persistent_assignment_selection_and_confirmed_metrics(self):
        rows = self.request(
            "GET",
            f"/api/v1/agency-transfers/assignments?vessel_id={self.vessel.id}",
            self.agent,
        ).json()
        self.assertEqual(rows[0]["assignment_id"], self.assignment.id)
        url = f"/api/v1/agency-transfers/assignments/{self.assignment.id}/confirm"
        self.request("POST", url, self.other_agent, {"action": "SIGN_ON"}, 404)
        self.request("POST", url, self.agent, {"action": "SIGN_ON"})
        self.request("POST", url, self.agent, {"action": "SIGN_ON"})
        result = self.request(
            "GET", "/api/v1/shipping-company/movements", self.admin
        ).json()
        figures = {f["key"]: f for f in result["figures"]}
        self.assertEqual(figures["sign_ons"]["value"], 1)
        self.assertEqual(figures["sign_offs"]["value"], 0)
        self.request("POST", url, self.agent, {"action": "SIGN_OFF"})
        self.request("POST", url, self.agent, {"action": "SIGN_OFF"}, 409)
        self.assertEqual(agg.sign_offs(self.db, company_id=self.company.id).value, 1)

    def test_prepare_incoming_passenger_is_not_a_confirmed_movement(self):
        response = self.request(
            "POST",
            "/api/v1/agency-transfers/passengers",
            self.agent,
            {
                "vessel_id": self.vessel.id,
                "crew_name": "Incoming",
                "passport_number": "P9999999",
                "nationality": "IN",
            },
            201,
        ).json()
        self.assertIsNone(response["signed_on_at"])
        self.assertIsNotNone(response["reference"])
        self.assertEqual(agg.sign_ons(self.db, company_id=self.company.id).value, 0)

    def test_member_removal_denies_existing_token_and_last_admin_is_protected(self):
        self.request(
            "PATCH",
            f"/api/v1/shipping-company/members/{self.admin2.id}",
            self.admin,
            {"role": "VIEWER", "enabled": False},
        )
        self.request(
            "GET", "/api/v1/shipping-company/overview", self.admin2, status=403
        )
        self.request(
            "PATCH",
            f"/api/v1/shipping-company/members/{self.admin.id}",
            self.admin,
            {"role": "VIEWER", "enabled": True},
            409,
        )
        self.request(
            "PATCH",
            f"/api/v1/shipping-company/members/{self.provider_user.id}",
            self.admin,
            {"role": "VIEWER", "enabled": False},
            404,
        )

    def test_concurrent_admin_demotions_keep_one_active_admin(self):
        url = lambda uid: f"/api/v1/shipping-company/members/{uid}"
        requests = [
            (self.admin.id, self.headers(self.admin)),
            (self.admin2.id, self.headers(self.admin2)),
        ]

        def demote(request):
            return self.client.patch(
                url(request[0]),
                headers=request[1],
                json={"role": "VIEWER", "enabled": True},
            ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(demote, requests))
        self.assertEqual(sorted(statuses), [200, 409])

    def invite(self, email="newstaff@example.com"):
        with (
            patch("app.services.email.is_configured", return_value=True),
            patch("app.services.email.send_email", return_value=True) as sent,
        ):
            row = self.request(
                "POST",
                "/api/v1/shipping-company/invitations",
                self.admin,
                {"email": email, "role": "ANALYST"},
                201,
            ).json()
            token = sent.call_args.args[2].split("#")[1].split(" ")[0]
        return row, token

    def test_invitation_context_survives_refresh_without_exposing_bearer(self):
        row, token = self.invite()
        opened = self.request("POST", "/api/v1/shipping-company/invitations/session", body={"token": token})
        self.assertFalse(opened.json()["account_exists"])
        self.assertNotIn(token, opened.text)
        cookie = opened.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)
        self.assertNotIn(token, cookie)
        resumed = self.request("GET", "/api/v1/shipping-company/invitations/session")
        self.assertEqual(resumed.json()["email"], "newstaff@example.com")
        self.request("POST", "/api/v1/shipping-company/invitations/accept",
                     body={"name": "New staff", "password": "fixture-only-secret"})
        self.request("GET", "/api/v1/shipping-company/invitations/session", status=410)
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(CompanyInvitation, row["id"]).accepted_at)

    def test_context_retains_existing_identity_checks_and_membership_discovery(self):
        crew = User(email="dual@example.com", name="Crew company member", role="crew", hashed_password=FIXTURE_PASSWORD_HASH)
        self.db.add(crew); self.db.commit()
        initial = self.request("GET", "/api/v1/auth/workspaces", crew).json()
        self.assertEqual([w["id"] for w in initial["workspaces"]], ["crew"])
        _, token = self.invite(crew.email)
        self.request("POST", "/api/v1/shipping-company/invitations/session", body={"token": token})
        self.request("POST", "/api/v1/shipping-company/invitations/accept", body={}, status=403)
        self.request("POST", "/api/v1/shipping-company/invitations/accept", self.admin, {}, 403)
        self.request("POST", "/api/v1/shipping-company/invitations/accept", crew, {})
        self.db.refresh(crew)
        self.assertEqual(crew.role, "crew")
        self.assertEqual(crew.hashed_password, FIXTURE_PASSWORD_HASH)
        result = self.request("GET", "/api/v1/auth/workspaces", crew).json()
        self.assertEqual([w["id"] for w in result["workspaces"]], ["crew", "company"])
        self.request("GET", "/api/v1/shipping-company/me", crew)
        self.request("POST", "/api/v1/shipping-company/invitations/accept", crew, {}, 410)

    def test_invitation_context_expires_and_cannot_be_reused_as_authentication(self):
        from jose import jwt
        from app.core.config import settings
        _, token = self.invite()
        opened = self.request("POST", "/api/v1/shipping-company/invitations/session", body={"token": token})
        self.assertIn("Max-Age=", opened.headers["set-cookie"])
        value = self.client.cookies.get("company_invitation_context")
        payload = jwt.decode(value, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        self.assertLessEqual(payload["exp"] - datetime.now(UTC).timestamp(), 900)
        denied = self.client.get("/api/v1/auth/workspaces", headers={"Authorization": "Bearer " + value})
        self.assertEqual(denied.status_code, 401)
        payload["exp"] = int(datetime.now(UTC).timestamp()) - 1
        self.client.cookies.clear()
        self.client.cookies.set("company_invitation_context", jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM),
                                path="/api/v1/shipping-company/invitations")
        self.request("GET", "/api/v1/shipping-company/invitations/session", status=410)

    def test_invitation_context_revalidates_revocation_and_ignores_access_tokens(self):
        row, token = self.invite()
        self.request("POST", "/api/v1/shipping-company/invitations/session", body={"token": token})
        self.request("DELETE", f"/api/v1/shipping-company/invitations/{row['id']}", self.admin, status=204)
        self.request("GET", "/api/v1/shipping-company/invitations/session", status=410)
        self.request("POST", "/api/v1/shipping-company/invitations/accept", body={}, status=410)
        self.client.cookies.set("company_invitation_context", create_access_token(self.admin.email),
                                path="/api/v1/shipping-company/invitations")
        self.request("GET", "/api/v1/shipping-company/invitations/session", status=410)
        self.request("GET", "/api/v1/auth/workspaces", status=401)

    def test_invitation_creates_verified_email_account_once(self):
        row, token = self.invite()
        self.assertNotIn(token, str(row))
        saved = self.db.get(CompanyInvitation, row["id"])
        self.assertNotEqual(saved.token_hash, token)
        body = {"token": token, "name": "New staff", "password": "fixture-only-secret"}
        self.request("POST", "/api/v1/shipping-company/invitations/accept", body=body)
        self.request(
            "POST", "/api/v1/shipping-company/invitations/accept", body=body, status=410
        )
        login = self.request(
            "POST",
            "/api/v1/auth/login",
            body={"email": "newstaff@example.com", "password": "fixture-only-secret"},
        ).json()
        response = self.client.get(
            "/api/v1/shipping-company/me",
            headers={"Authorization": "Bearer " + login["access_token"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["membership_role"], "ANALYST")

    def test_existing_account_invitation_cannot_reset_password(self):
        row, token = self.invite(self.provider_user.email)
        self.request(
            "POST",
            "/api/v1/shipping-company/invitations/accept",
            body={"token": token, "password": "attack-password"},
            status=403,
        )
        self.request(
            "POST",
            "/api/v1/shipping-company/invitations/accept",
            self.admin,
            {"token": token},
            403,
        )
        self.request(
            "POST",
            "/api/v1/shipping-company/invitations/accept",
            self.provider_user,
            {"token": token},
        )

    def test_invitation_revocation_expiry_and_delivery_failure(self):
        row, token = self.invite()
        self.request(
            "DELETE",
            f"/api/v1/shipping-company/invitations/{row['id']}",
            self.admin,
            status=204,
        )
        self.request(
            "POST",
            "/api/v1/shipping-company/invitations/accept",
            body={"token": token},
            status=410,
        )
        row, token = self.invite()
        inv = self.db.get(CompanyInvitation, row["id"])
        inv.expires_at = NOW - timedelta(seconds=1)
        self.db.commit()
        self.request(
            "POST",
            "/api/v1/shipping-company/invitations/accept",
            body={"token": token},
            status=410,
        )
        with patch("app.services.email.is_configured", return_value=False):
            self.request(
                "POST",
                "/api/v1/shipping-company/invitations",
                self.admin,
                {"email": "x@example.com"},
                503,
            )

    def test_resolved_origin_excludes_legacy_and_conflicts_from_every_aggregate(self):
        good = self.booking()
        legacy = self.booking(vessel_call_id=None, agency_id=None)
        conflict = self.booking(agency_id=self.agency2.id)
        self.assertEqual(classify(good, self.call), CONFIRMED_AGENCY)
        self.assertEqual(classify(legacy, None), UNRESOLVED)
        self.assertEqual(classify(conflict, self.call), CONFLICTED)
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.company.id).value, 1
        )
        self.assertEqual(agg.booked_seats(self.db, company_id=self.company.id).value, 3)
        self.assertTrue(
            routes_bookings._agent_may_view_booking(self.db, self.agent.id, good)
        )
        self.assertFalse(
            routes_bookings._agent_may_view_booking(self.db, self.agent.id, legacy)
        )
        self.assertFalse(
            routes_trips._agent_owns_trip(self.db, self.other_agent.id, conflict)
        )

    def test_unmanaged_context_is_legitimate_but_missing_agency_is_not_proof(self):
        self.call.agency_id = None
        self.call.agency_name = None
        self.db.commit()
        b = self.booking(agency_id=None, context_resolution=None)
        self.assertEqual(classify(b, self.call), UNRESOLVED)
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.company.id).value, 0
        )
        b.context_resolution = CONFIRMED_UNMANAGED
        self.db.commit()
        self.assertEqual(classify(b, self.call), CONFIRMED_UNMANAGED)
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.company.id).value, 1
        )

    def test_legacy_booking_reconciliation_is_admin_only_and_audited(self):
        b = self.booking(vessel_call_id=None, agency_id=None)
        queue = self.request(
            "GET",
            "/api/v1/superadmin/historical-context/unresolved?record_kind=booking",
            self.sa,
        ).json()
        self.assertEqual(queue["record_total"], 1)
        body = {
            "vessel_call_id": self.call.id,
            "evidence_type": "trip_record",
            "evidence_reference": "FIXTURE-EVIDENCE",
            "notes": "Verified original dispatch record",
            "expected_context": queue["records"][0]["current_context"],
        }
        path = f"/api/v1/superadmin/historical-context/booking/{b.id}/reconcile"
        self.request("POST", path, self.agent, body, 403)
        self.request("POST", path, self.sa, body)
        self.db.refresh(b)
        self.assertEqual(classify(b, self.call), CONFIRMED_AGENCY)
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.company.id).value, 1
        )

    def test_incoming_company_sees_partial_overlapping_call(self):
        claim = self.db.query(VesselRoleClaim).one()
        claim.effective_start = JUL
        self.db.commit()
        calls = self.request(
            "GET",
            f"/api/v1/shipping-company/vessels/{self.vessel.imo_number}/calls",
            self.admin,
        ).json()
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["fully_within_tenure"])
        self.assertEqual(
            agg.qualifying_calls(self.db, company_id=self.company.id).value, 1
        )

    def test_collection_pagination_is_ordered_complete_and_excludes_internal_counts(
        self,
    ):
        for i in range(5):
            self.booking()
        # Company request list is a paged SQL query; a different target does not page it.
        for i in range(4):
            self.request(
                "POST",
                "/api/v1/shipping-company/members",
                self.admin,
                {"email": self.admin2.email, "role": "ADMIN"},
                201,
            )
        base = "/api/v1/shipping-company/members?collection=routes_shipping_company.list_members.0&collection_limit=1"
        first = self.request("GET", base, self.admin)
        import json

        metadata = json.loads(first.headers["X-Collection-Pages"])[
            "routes_shipping_company.list_members.0"
        ]
        self.assertTrue(metadata["has_more"])
        self.assertEqual(len(first.json()), 1)
        second = self.request("GET", base + "&collection_offset=1", self.admin)
        self.assertNotEqual(first.json()[0]["user_id"], second.json()[0]["user_id"])
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.company.id).value, 5
        )
        self.request("GET", base + "&collection_offset=-1", self.admin, status=422)

    def test_sensitive_events_use_people_not_repeated_event_count(self):
        from app.db.models.crew_profile import CrewProfile
        from app.db.models.crew_sos import CrewSos

        profile = CrewProfile(
            user_id=self.admin.id, full_name="Synthetic", nationality="IN", rank="AB"
        )
        self.db.add(profile)
        self.db.flush()
        for _ in range(5):
            self.db.add(
                CrewSos(
                    crew_profile_id=profile.id,
                    vessel_id=self.vessel.id,
                    vessel_call_id=self.call.id,
                    agency_id=self.agency.id,
                    context_resolution=CONFIRMED_AGENCY,
                    created_at=NOW,
                )
            )
        self.db.commit()
        self.assertTrue(agg.sos_raised(self.db, company_id=self.company.id).suppressed)
        for n in range(4):
            user = User(
                email=f"sensitive-{n}@example.com",
                hashed_password=FIXTURE_PASSWORD_HASH,
                role="crew",
            )
            self.db.add(user)
            self.db.flush()
            person = CrewProfile(
                user_id=user.id, full_name="Synthetic", nationality="IN", rank="AB"
            )
            self.db.add(person)
            self.db.flush()
            self.db.add(
                CrewSos(
                    crew_profile_id=person.id,
                    vessel_id=self.vessel.id,
                    vessel_call_id=self.call.id,
                    agency_id=self.agency.id,
                    context_resolution=CONFIRMED_AGENCY,
                    created_at=NOW,
                )
            )
        self.db.commit()
        result = agg.sos_raised(self.db, company_id=self.company.id)
        self.assertFalse(result.suppressed)
        self.assertEqual(result.value, 9)

    def test_confirmed_movement_handover_uses_each_timestamp(self):
        outgoing = self.db.query(VesselRoleClaim).one()
        outgoing.effective_end = JUL
        company = ShippingCompany(
            legal_name="Incoming", normalized_name="INCOMING", status="VERIFIED"
        )
        self.db.add(company)
        self.db.flush()
        self.db.add(
            VesselRoleClaim(
                company_id=company.id,
                vessel_id=self.vessel.id,
                role="ISM_MANAGER",
                effective_start=JUL,
                status="APPROVED",
            )
        )
        self.assignment.signed_on_at = JUL - timedelta(seconds=1)
        self.assignment.signed_off_at = JUL
        self.db.commit()
        self.assertEqual(agg.sign_ons(self.db, company_id=self.company.id).value, 1)
        self.assertEqual(agg.sign_offs(self.db, company_id=self.company.id).value, 0)
        self.assertEqual(agg.sign_ons(self.db, company_id=company.id).value, 0)
        self.assertEqual(agg.sign_offs(self.db, company_id=company.id).value, 1)

    def test_historical_link_proposals_are_separate_per_voyage_and_admin_approved(self):
        from app.db.models.crew_profile import CrewProfile
        from app.db.models.crew_identity_conflict import CrewIdentityConflictRecord
        from app.services.historical_linkage import link_historical_assignments

        self.call.ended_at = JUL
        self.assignment.ended_at = JUL
        second = VesselCall(
            vessel_id=self.vessel.id,
            agency_id=self.agency.id,
            vessel_name=self.vessel.name,
            started_at=JUL,
            ended_at=JUL + timedelta(days=5),
        )
        self.db.add(second)
        self.db.flush()
        other = CrewAssignment(
            vessel_call_id=second.id,
            crew_name="Synthetic",
            passport_number=self.assignment.passport_number,
            nationality="IN",
            started_at=JUL,
            ended_at=second.ended_at,
        )
        profile = CrewProfile(
            user_id=self.admin.id,
            full_name="Synthetic",
            passport_number=self.assignment.passport_number,
            nationality="IN",
            rank="AB",
        )
        self.db.add_all([other, profile])
        self.db.commit()
        result = link_historical_assignments(self.db, profile)
        self.assertEqual(result["linked"], [])
        self.assertEqual(len(result["conflicts"]), 2)
        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)
        conflict = self.db.get(CrewIdentityConflictRecord, result["conflicts"][0])
        body = {
            "expected_version": conflict.version,
            "action": "SELECT_PROFILE",
            "crew_profile_id": profile.id,
            "evidence_type": "manual_document",
            "evidence_reference": "FIXTURE-PROOF",
            "reason": "Original manifest identity independently checked",
        }
        path = f"/api/v1/superadmin/identity-conflicts/{conflict.id}/resolve"
        self.request("POST", path, self.agent, body, 403)
        self.request("POST", path, self.sa, body)
        assignment_id = conflict.proposed_identity["assignment_id"]
        self.db.expire_all()
        self.assertEqual(
            self.db.get(CrewAssignment, assignment_id).crew_profile_id, profile.id
        )
        other_id = (
            other.id if assignment_id == self.assignment.id else self.assignment.id
        )
        self.assertIsNone(self.db.get(CrewAssignment, other_id).crew_profile_id)

    def test_distinct_and_grouped_sql_pages_keep_valid_ordering(self):
        from app.services.collection_pages import page_all
        from sqlalchemy import func

        roles = page_all(self.db.query(User.role).distinct(), collection="roles")
        self.assertEqual(len(roles), len(set(row[0] for row in roles)))
        grouped = page_all(
            self.db.query(User.role, func.count(User.id)).group_by(User.role),
            collection="counts",
        )
        self.assertEqual(sum(row[1] for row in grouped), self.db.query(User).count())

    def test_durable_invitation_change_and_driver_link_expiry(self):
        row, token = self.invite()
        self.request(
            "PATCH",
            f"/api/v1/shipping-company/members/{self.admin.id}",
            self.admin,
            {"role": "VIEWER", "enabled": True},
        )
        self.request(
            "POST",
            "/api/v1/shipping-company/invitations",
            self.admin,
            {"email": "blocked@example.com"},
            403,
        )
        t = self.transfer()
        self.action(t, self.provider_user, "accept")
        token = self.action(t, self.provider_user, "assign", driver_id=self.driver.id)[
            "driver_token"
        ]
        from app.db.models.agency_transfer import AgencyTransfer

        transfer = self.db.get(AgencyTransfer, t["id"])
        transfer.driver_link_expires_at = NOW - timedelta(seconds=1)
        self.db.commit()
        self.request("GET", "/api/v1/agency-transfer-trip/" + token, status=410)

    def test_second_transfer_cannot_reserve_an_already_busy_driver(self):
        first = self.transfer()
        second = self.transfer()
        self.action(first, self.provider_user, "accept")
        self.action(second, self.provider_user, "accept")
        self.action(first, self.provider_user, "assign", driver_id=self.driver.id)
        self.action(second, self.provider_user, "assign", 422, driver_id=self.driver.id)
        self.action(first, self.agent, "cancel", reason="Withdrawn by agent")
        self.action(second, self.provider_user, "assign", driver_id=self.driver.id)

    def test_reports_export_approved_units_and_definition_lineage(self):
        self.booking(num_passengers=4)
        report = self.request(
            "GET",
            "/api/v1/shipping-company/reports?period_start=2026-01-01T00:00:00Z&period_end=2027-01-01T00:00:00Z",
            self.admin,
        ).json()
        figures = {f["key"]: f for f in report["figures"]}
        self.assertEqual(figures["booked_seats"]["value"], 4)
        self.assertEqual(figures["returned_seats"]["value"], 4)
        self.assertIn("not unique crew", figures["booked_seats"]["caveat"])
        self.assertEqual(figures["booked_seats"]["suppression_threshold"], 0)
        self.assertNotIn("sos_raised", figures)
        self.assertNotIn("incidents_raised", figures)
        for f in figures.values():
            self.assertTrue(f["source_version"])
            self.assertTrue(f["event_time"])

    def test_planned_transport_is_shown_separately_from_actual_movements(self):
        self.transfer()
        figures = {
            f["key"]: f
            for f in self.request(
                "GET", "/api/v1/shipping-company/movements", self.admin
            ).json()["figures"]
        }
        self.assertEqual(figures["pending_transfers"]["value"], 1)
        self.assertEqual(figures["sign_ons"]["value"], 0)
        self.assertEqual(figures["sign_offs"]["value"], 0)

    def test_ashore_estimate_uses_stop_evidence_and_reports_completion_disagreement(
        self,
    ):
        from app.db.models.driver_magic_link import (
            DriverMagicLink,
            DriverMagicLinkReachEvent,
        )

        b = self.booking(
            status="on_trip",
            num_passengers=4,
            trip_started_at=NOW - timedelta(minutes=5),
            trip_completed_at=None,
        )
        link = DriverMagicLink(
            booking_id=b.id,
            token="fixture-cab-link",
            itinerary_stops=[
                {"id": "pickup", "type": "pickup"},
                {"id": "port-end", "type": "trip_end"},
            ],
        )
        self.db.add(link)
        self.db.flush()
        event = DriverMagicLinkReachEvent(
            magic_link_id=link.id,
            stop_id="pickup",
            stop_name="Port gate",
            latitude=0,
            longitude=0,
            reached_at=NOW,
        )
        self.db.add(event)
        self.db.commit()
        self.assertEqual(agg._ashore_snapshot(self.db, self.company.id), (4, 0))
        b.status = "completed"
        b.trip_completed_at = NOW
        self.db.commit()
        # Completion is the owner's separate returned-journey proxy, not a port-end stop.
        self.assertEqual(
            agg.returned_seats(self.db, company_id=self.company.id).value, 4
        )
        self.assertEqual(agg._ashore_snapshot(self.db, self.company.id), (4, 4))
        self.db.add(
            DriverMagicLinkReachEvent(
                magic_link_id=link.id,
                stop_id="port-end",
                stop_name="Port",
                latitude=0,
                longitude=0,
                reached_at=NOW + timedelta(microseconds=1),
            )
        )
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(agg._ashore_snapshot(self.db, self.company.id), (0, 0))

    def test_missing_pickup_and_tenure_gap_do_not_invent_current_ashore(self):
        self.booking(
            status="on_trip",
            num_passengers=3,
            trip_started_at=NOW - timedelta(minutes=2),
            trip_completed_at=None,
        )
        self.assertEqual(agg._ashore_snapshot(self.db, self.company.id), (0, 3))
        self.db.query(VesselRoleClaim).one().effective_end = JUL
        self.db.commit()
        self.assertEqual(agg._ashore_snapshot(self.db, self.company.id), (0, 0))

    def test_immutable_agency_history_without_call_is_preserved_but_excluded_from_company(
        self,
    ):
        b = self.booking(vessel_call_id=None)
        self.assertTrue(
            routes_bookings._agent_may_view_booking(self.db, self.agent.id, b)
        )
        self.assertFalse(
            routes_bookings._agent_may_view_booking(self.db, self.other_agent.id, b)
        )
        self.assertEqual(
            agg.booking_activity(self.db, company_id=self.company.id).value, 0
        )

    def test_retry_key_is_idempotent_and_payload_changes_are_refused(self):
        body = {
            "vessel_call_id": self.call.id,
            "provider_id": self.provider.id,
            "assignment_ids": [self.assignment.id],
            "movement_type": "SIGN_ON",
            "pickup": "Airport",
            "destination": "Port",
            "travel_at": (NOW + timedelta(hours=1)).isoformat(),
            "client_request_id": "fixture-retry-key",
        }
        first = self.request(
            "POST", "/api/v1/agency-transfers", self.agent, body, 201
        ).json()
        second = self.request(
            "POST", "/api/v1/agency-transfers", self.agent, body, 201
        ).json()
        self.assertEqual(first["id"], second["id"])
        body["destination"] = "Different port"
        self.request("POST", "/api/v1/agency-transfers", self.agent, body, 409)

    def test_sql_and_python_origin_classification_agree(self):
        from app.services.event_origin import origin_expression

        good = self.booking()
        missing = self.booking(vessel_call_id=None)
        conflict = self.booking(agency_id=self.agency2.id)
        result = dict(self.db.query(CabBooking.id, origin_expression(CabBooking)).all())
        for row in (good, missing, conflict):
            call = (
                self.db.get(VesselCall, row.vessel_call_id)
                if row.vessel_call_id
                else None
            )
            self.assertEqual(result[row.id], classify(row, call))

    def test_claim_conflict_details_are_bounded_and_admin_only(self):
        # The proposed claim may overlap; only approved claims are constrained.
        proposed = VesselRoleClaim(
            company_id=self.company.id,
            vessel_id=self.vessel.id,
            role="ISM_MANAGER",
            effective_start=JAN,
            status="SUBMITTED",
        )
        self.db.add(proposed)
        self.db.commit()
        path = f"/api/v1/superadmin/company-claims/{proposed.id}/conflicts"
        self.request("GET", path, self.agent, status=403)
        first = self.request("GET", path + "?limit=1", self.sa).json()
        self.assertEqual(first["total"], 1)
        self.assertEqual(len(first["items"]), 1)
        second = self.request("GET", path + "?limit=1&offset=1", self.sa).json()
        self.assertEqual(second["items"], [])
        self.request("GET", path + "?limit=101", self.sa, status=422)

    def test_migration_is_idempotent_schema_only_and_has_one_head(self):
        import importlib.util
        from pathlib import Path
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        path = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/e1f2a3b4c5d6_company_operations.py"
        )
        spec = importlib.util.spec_from_file_location("operations_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        self.assertEqual(len(ScriptDirectory.from_config(Config("alembic.ini")).get_heads()), 1)
        before = self.db.execute(
            text("SELECT id, email, hashed_password FROM users ORDER BY id")
        ).all()
        self.db.rollback()
        with engine.begin() as connection:
            # These four tables are empty synthetic fixture tables after setUp.
            # Exercise fresh creation as well as repeat-upgrade idempotency.
            connection.execute(
                text(
                    "DROP TABLE crew_movement_events, agency_transfer_events, agency_transfers, company_invitations"
                )
            )
            with patch.object(migration.op, "execute", side_effect=connection.execute):
                migration.upgrade()
                migration.upgrade()
                migration.downgrade()
        after = self.db.execute(
            text("SELECT id, email, hashed_password FROM users ORDER BY id")
        ).all()
        self.assertEqual(before, after)

    def test_identity_audit_has_separate_authorized_page(self):
        from app.db.models.crew_identity_conflict import (
            CrewIdentityConflictRecord,
            CrewIdentityConflictAudit,
        )

        row = CrewIdentityConflictRecord(
            operation="FIXTURE",
            vessel_id=self.vessel.id,
            passport_key="FIXTURE",
            identity_fingerprint="f" * 64,
            proposed_identity={},
            candidate_profile_ids=[],
            conflict_message="Fixture",
        )
        self.db.add(row)
        self.db.flush()
        for i in range(3):
            self.db.add(
                CrewIdentityConflictAudit(
                    conflict_id=row.id,
                    actor_user_id=self.sa.id,
                    action="FIXTURE",
                    expected_version=i + 1,
                    before_state={},
                    after_state={},
                    evidence_type="fixture",
                    reason="Fixture evidence",
                )
            )
        self.db.commit()
        path = f"/api/v1/superadmin/identity-conflicts/{row.id}/audit"
        self.request("GET", path, self.agent, status=403)
        a = self.request("GET", path + "?limit=2", self.sa).json()
        b = self.request("GET", path + "?limit=2&offset=2", self.sa).json()
        self.assertEqual(a["total"], 3)
        self.assertEqual(len(a["items"]), 2)
        self.assertEqual(len(b["items"]), 1)
        self.assertFalse(
            set(r["id"] for r in a["items"]) & set(r["id"] for r in b["items"])
        )

    def test_vessel_without_call_never_receives_agency_wide_trip_history(self):
        self.booking()
        empty = Vessel(
            name="No call MV",
            imo_number="IMO-EMPTY",
            vessel_type="Bulk Carrier",
            status="Active",
            agent_id=self.agent.id,
        )
        self.db.add(empty)
        self.db.commit()
        result = routes_trips.get_trip_monitoring(
            vessel_id=empty.id, db=self.db, current_user=self.agent
        )
        self.assertEqual(
            result.model_dump(), {"ongoing": [], "requested": [], "completed": []}
        )
        # Immutable ownership remains useful after the agency has handed over.
        self.vessel.agent_id = self.other_agent.id
        self.db.commit()
        result = routes_trips.get_trip_monitoring(
            vessel_id=None, db=self.db, current_user=self.agent
        )
        self.assertEqual(len(result.completed), 1)


if __name__ == "__main__":
    unittest.main()
