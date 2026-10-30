"""Authenticated HTTP lifecycle, tenure, scope and period regressions."""
from datetime import timedelta
from urllib.parse import urlencode
import unittest

import test_company_operations_http as fixture
import test_company_intelligence_http as existing
from app.db.models.shipping_company import VesselRoleClaim
from app.db.models.port_geography import PortGeography


class CompanyLifecycleHTTP(unittest.TestCase):
    setUp = fixture.OperationsHTTP.setUp
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request
    booking = fixture.OperationsHTTP.booking
    api = existing.IntelligenceHTTP.api

    def test_clock_expired_stored_active_call_moves_to_history(self):
        self.call.etd = self.vessel.etd = fixture.NOW-timedelta(days=1)
        self.db.commit()
        self.assertEqual(self.api("/calls").json()["total"], 0)
        history = self.api("/calls?active=false&historical_only=true").json()
        self.assertEqual([c["id"] for c in history["items"]], [self.call.id])
        self.assertFalse(history["items"][0]["current_port_call"])
        self.assertEqual(self.api("/overview").json()["metrics"]["active_vessels"], 0)

    def test_departing_remains_current(self):
        self.call.status = "DEPARTING"
        self.call.etd = self.vessel.etd = fixture.NOW+timedelta(hours=1)
        self.db.commit()
        self.assertEqual(self.api("/calls").json()["total"], 1)

    def test_ended_tenure_keeps_authorized_history_and_excludes_current(self):
        self.call.ended_at = fixture.NOW-timedelta(days=2)
        self.db.query(VesselRoleClaim).update({"effective_end": fixture.NOW-timedelta(days=1)})
        self.db.commit()
        self.assertEqual(self.api("/calls").json()["total"], 0)
        self.assertEqual(self.api("/calls?active=false&historical_only=true").json()["total"], 1)
        self.assertEqual(self.api(f"/calls/{self.call.id}").status_code, 200)

    def test_claim_after_actual_departure_cannot_read_stale_active_call(self):
        self.call.etd = self.vessel.etd = fixture.NOW-timedelta(days=2)
        self.db.query(VesselRoleClaim).update({"effective_start": fixture.NOW-timedelta(days=1)})
        self.db.commit()
        self.assertEqual(self.api("/calls?active=false").json()["total"], 0)

    def test_period_activity_is_separate_from_current_snapshot(self):
        self.booking(created_at=fixture.NOW-timedelta(days=3))
        self.booking(created_at=fixture.NOW-timedelta(hours=1))
        params = urlencode({"period_start": (fixture.NOW-timedelta(days=1)).isoformat(), "period_end": fixture.NOW.isoformat()})
        data = self.api("/overview?"+params).json()
        self.assertEqual(data["metrics"]["active_vessels"], 1)
        self.assertEqual(data["period_metrics"]["total_booked_rides"], 1)
        activities = self.api(f"/calls/{self.call.id}/activities?"+params).json()
        self.assertEqual(activities["total"], 1)
        self.assertEqual(self.api(f"/calls/{self.call.id}?"+params).json()["metrics"]["total_booked_rides"], 1)

    def test_country_filter_matches_reviewed_metadata_only(self):
        self.db.add(PortGeography(port_id=self.port.id, country_code="IN", evidence_reference="fixture", updated_by_user_id=self.sa.id))
        self.db.commit()
        self.assertEqual(self.api("/calls?country=IN").json()["total"], 1)
        self.assertEqual(self.api("/calls?country=AE").json()["total"], 0)
        self.assertEqual(self.api("/ports/page?country=AE").json()["total"], 0)
