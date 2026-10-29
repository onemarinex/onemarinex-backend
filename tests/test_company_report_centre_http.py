"""Six scoped templates, fixed PDF parity and current disclosure permission."""

import unittest
from datetime import timedelta

import test_company_disclosure_reports_http as disclosure_fixture
import test_company_operations_http as fixture
from app.db.models.company_disclosure import CompanyDisclosureRequest

BASE = "/api/v1/shipping-company/report-centre"


class ReportCentreHTTP(unittest.TestCase):
    setUp = disclosure_fixture.DisclosureHTTP.setUp
    tearDown = disclosure_fixture.DisclosureHTTP.tearDown
    headers = disclosure_fixture.DisclosureHTTP.headers
    request = disclosure_fixture.DisclosureHTTP.request
    booking = disclosure_fixture.DisclosureHTTP.booking
    escalation = disclosure_fixture.DisclosureHTTP.escalation
    ask = disclosure_fixture.DisclosureHTTP.ask
    review = disclosure_fixture.DisclosureHTTP.review

    def body(self, kind="FLEET", **extra):
        return dict(report_type=kind, start=fixture.JAN.isoformat(),
                    end=(fixture.NOW+timedelta(days=1)).isoformat(), **extra)

    def generate(self, **extra):
        return self.request("POST", BASE, self.admin, self.body(**extra), 201).json()

    def test_six_types_use_scoped_payloads_and_never_include_unescalated_cases(self):
        options = self.request("GET", BASE+"/options", self.admin).json()
        self.assertEqual(len(options["types"]), 6)
        self.assertEqual(len(options["sections"]), 13)
        for kind in ("EXECUTIVE", "FLEET", "PORT", "CALL", "WELFARE", "SAFETY"):
            body = self.body(kind)
            if kind == "CALL":
                body["call_id"] = self.call.id
            result = self.request("POST", BASE+"/preview", self.admin, body).json()
            self.assertEqual(result["data_mode"], "LIVE")
            self.assertEqual(result["report_type"], kind)
            self.assertEqual(result["safety"]["cases"], [])
            self.assertNotIn("PRIVATE DESCRIPTION", str(result))
            self.assertNotIn("Alice", str(result))
        self.request("POST", BASE+"/preview", self.admin, self.body("CALL"), 422)

    def test_saved_preview_and_export_are_byte_identical_and_private(self):
        report = self.generate()
        preview = self.request("GET", BASE+f"/{report['id']}/preview.pdf", self.admin)
        export = self.request("GET", BASE+f"/{report['id']}/export?format=pdf", self.admin)
        self.assertTrue(preview.content.startswith(b"%PDF-"))
        self.assertEqual(preview.content, export.content)
        self.assertIn("inline", preview.headers["content-disposition"])
        self.assertIn("no-store", preview.headers["cache-control"])
        self.assertIn("private", preview.headers["cache-control"])
        self.request("GET", BASE+f"/{report['id']}/preview.pdf", self.agent, status=403)

    def test_configuration_is_validated_and_frozen_in_report_snapshot(self):
        configuration = {"sections":["fleet_operations"], "detail":"summary", "charts":False, "tables":True}
        report = self.generate(configuration=configuration)
        self.assertEqual(report["payload"]["configuration"], configuration)
        self.request("POST", BASE+"/preview", self.admin, self.body(configuration={"sections":["crew.phone"]}), 422)
        self.request("POST", BASE+"/preview", self.admin, self.body(configuration={"sections":[]}), 422)
        self.request("POST", BASE+"/preview", self.admin, self.body(configuration={"arbitrary":True}), 422)

    def test_explicit_escalation_releases_progress_but_not_narratives_or_people(self):
        self.escalation()
        report = self.generate(kind="SAFETY")
        safety = report["payload"]["safety"]
        self.assertEqual(safety["incident_count"], 1)
        self.assertEqual(safety["cases"][0]["status"], "OPEN")
        self.assertIsNone(safety["cases"][0]["severity"])
        self.assertEqual(safety["unresolved_count"], 1)
        self.assertNotIn("PRIVATE DESCRIPTION", str(report))
        self.assertNotIn("Alice", str(report))

    def test_expired_field_grant_invalidates_saved_case_status_in_preview_and_export(self):
        escalation = self.escalation()
        request_id = self.ask(escalation)
        self.review(request_id)
        report = self.generate(kind="SAFETY")
        self.assertIsNotNone(report["payload"]["safety"]["cases"][0]["status"])
        self.assertNotIn("PRIVATE DESCRIPTION", str(report))
        self.assertNotIn("Alice", str(report))
        self.db.expire_all()
        grant = self.db.get(CompanyDisclosureRequest, request_id)
        grant.access_expires_at = fixture.NOW-timedelta(minutes=1)
        self.db.commit()
        for suffix in ("", "/preview.pdf", "/export?format=pdf"):
            self.request("GET", BASE+f"/{report['id']}"+suffix, self.admin, status=403)

    def test_revoked_escalation_invalidates_saved_safety_report(self):
        escalation = self.escalation()
        report = self.generate(kind="SAFETY")
        self.request("DELETE", f"/api/v1/superadmin/safety-escalations/{escalation}", self.sa, status=204)
        self.request("GET", BASE+f"/{report['id']}/preview.pdf", self.admin, status=404)

    def test_country_and_period_filters_apply_to_call_options_and_all_sources(self):
        calls = self.request("GET", BASE+"/calls?country_code=UNKNOWN&limit=1", self.admin).json()
        self.assertEqual(calls["total"], 1)
        self.assertEqual(len(calls["items"]), 1)
        self.assertEqual(self.request("GET", BASE+"/calls?country_code=US", self.admin).json()["total"], 0)
        report = self.generate(country_code="US")
        self.assertEqual(report["payload"]["summary"]["recorded_calls"], 0)
        self.assertEqual(report["payload"]["vessels"], [])
        self.request("GET", BASE+"/calls?country_code=invalid", self.admin, status=422)
        self.request("GET", BASE+"/calls?start="+fixture.JAN.isoformat().replace("+", "%2B"), self.admin, status=422)

    def test_future_period_does_not_turn_open_calls_into_future_recorded_activity(self):
        body = self.body()
        body.update(start=(fixture.NOW+timedelta(days=1)).isoformat(), end=(fixture.NOW+timedelta(days=2)).isoformat())
        report = self.request("POST", BASE+"/preview", self.admin, body).json()
        self.assertEqual(report["summary"]["recorded_calls"], 0)
        self.assertEqual(report["calls"], [])
