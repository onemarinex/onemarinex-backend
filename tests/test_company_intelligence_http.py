"""Real authenticated HTTP tests for the company call workspace."""

import unittest
import test_company_operations_http as fixture
from app.api.v1 import routes_company_intelligence as intelligence
from app.db.models.shipping_company import ShippingCompany, CompanyUserMembership


class IntelligenceHTTP(unittest.TestCase):
    setUp = fixture.OperationsHTTP.setUp
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request
    booking = fixture.OperationsHTTP.booking

    def api(self, path):
        self.client.app.include_router(
            intelligence.router, prefix="/api/v1/shipping-company/intelligence"
        )
        return self.request(
            "GET", "/api/v1/shipping-company/intelligence" + path, self.admin
        )

    def test_open_call_counts_without_bookings(self):
        data = self.api("/overview").json()
        self.assertEqual(
            data["metrics"],
            dict(
                onboarded_vessels=1,
                active_vessels=1,
                active_ports=1,
                crew_ashore=0,
                ongoing_rides=0,
            ),
        )

    def test_call_header_uses_stored_call_context(self):
        self.call.agency_name = "Recorded handling agent"
        self.call.imo_number = "9544762"
        self.db.commit()
        data = self.api("/calls/1").json()
        self.assertEqual(data["agent"], "Recorded handling agent")
        self.assertEqual(data["imo_number"], "9544762")
        self.assertNotIn("crew", data)
        self.assertNotIn("sos", data["metrics"])

    def test_other_company_cannot_read_call_or_counts(self):
        company = ShippingCompany(
            legal_name="Other company",
            normalized_name="OTHER COMPANY",
            status="VERIFIED",
        )
        self.db.add(company)
        self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update(
            {"company_id": company.id}
        )
        self.db.commit()
        self.assertEqual(self.api("/overview").json()["metrics"]["active_vessels"], 0)
        self.request(
            "GET",
            "/api/v1/shipping-company/intelligence/calls/1",
            self.admin,
            status=404,
        )

    def test_revocation_blocks_call_read(self):
        self.api("/overview")
        self.db.query(fixture.VesselRoleClaim).update({"revoked_at": fixture.NOW})
        self.db.commit()
        self.request(
            "GET",
            "/api/v1/shipping-company/intelligence/calls/1",
            self.admin,
            status=404,
        )

    def test_unresolved_booking_is_not_counted(self):
        self.booking(vessel_call_id=None)
        self.assertEqual(
            self.api("/calls/1").json()["metrics"]["total_booked_rides"], 0
        )

    def test_other_agency_and_personal_records_are_not_disclosed(self):
        self.api("/overview")
        self.request(
            "GET", "/api/v1/shipping-company/intelligence/calls", self.agent, status=403
        )
        data = self.api("/calls/1/activities?kind=SHORE_LEAVE").json()
        self.assertEqual(data["items"], [])
