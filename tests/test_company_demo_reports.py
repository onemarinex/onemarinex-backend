"""Read-only demo API tests. No database fixture setup or table resets."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch, Mock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api.v1 import routes_company_demo_reports as routes
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_shipping_company import current_company_member
from app.services.company_demo_fixture import create_demo_data, month_start
from app.services.company_demo_reports import prepare, survey_statistics
from app.services.company_review_statistics import summarize_review_rows
from app.services.auth import decode_subject, verify_refresh_token


class DemoReports(unittest.TestCase):
    def setUp(self):
        self.anchor = datetime.now(timezone.utc).replace(microsecond=0)
        self.params = {"report_type": "EXECUTIVE", "start": month_start(self.anchor, -2).isoformat(), "end": month_start(self.anchor, 1).isoformat()}
        self.actor, self.company = SimpleNamespace(id=11), SimpleNamespace(id=7)
        app = FastAPI()
        app.include_router(routes.router, prefix="/reports")
        app.dependency_overrides[current_company_member] = lambda: (self.company, SimpleNamespace(role="ADMIN"))
        app.dependency_overrides[get_current_user] = lambda: self.actor
        self.client = TestClient(app)

    def test_monthly_charts_do_not_invent_zero_months_outside_the_selected_period(self):
        start = month_start(self.anchor)
        result = prepare(dict(self.params, start=start.isoformat()), self.anchor)
        for chart in result["charts"]:
            self.assertTrue(all(point["label"] == start.strftime("%Y-%m") for point in chart["items"]))

    def test_fleet_preserves_port_comparison_and_complete_booking_totals(self):
        result = prepare(dict(self.params, report_type="FLEET"), self.anchor)
        self.assertEqual(len(result["port_comparison"]), 3)
        self.assertEqual(sum(p["recorded_calls"] for p in result["port_comparison"]),result["summary"]["recorded_calls"])
        self.assertEqual(sum(p["booked_rides"] for p in result["port_comparison"]),result["summary"]["booked_rides"])

    def tearDown(self):
        self.client.close()

    def preview(self):
        response = self.client.post("/reports/demo/preview", json=self.params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def test_options_and_calls_are_numeric_scoped_paged_and_private(self):
        options = self.client.get("/reports/demo/options")
        self.assertEqual(options.status_code, 200)
        self.assertEqual(options.headers["cache-control"], "no-store")
        self.assertEqual(len(options.json()["types"]), 6)
        self.assertEqual(len(options.json()["scope"]["vessels"]), 4)
        self.assertEqual(options.json()["scope"]["countries"], ["IN"])
        calls = self.client.get("/reports/demo/calls?port_id=2&limit=2")
        self.assertEqual(calls.status_code, 200)
        self.assertEqual(calls.json()["total"], 3)
        self.assertEqual(len(calls.json()["items"]), 2)
        self.assertTrue(all(isinstance(c["id"], int) and c["port_name"] == "Chennai" for c in calls.json()["items"]))
        self.assertEqual(self.client.get("/reports/demo/calls?country_code=XX").status_code, 422)

    def test_unauthenticated_demo_endpoints_require_the_normal_bearer_session(self):
        self.client.app.dependency_overrides.clear()
        self.assertEqual(self.client.get("/reports/demo/options").status_code, 401)
        self.assertEqual(self.client.post("/reports/demo/render", json={"token": "x" * 30}).status_code, 401)

    def test_signed_preview_renders_same_pdf_without_database_writes(self):
        with patch("sqlalchemy.orm.Session.commit", side_effect=AssertionError("No writes allowed")), patch("sqlalchemy.orm.Session.flush", side_effect=AssertionError("No writes allowed")):
            response = self.preview()
            first = self.client.post("/reports/demo/render", json={"token": response["token"]})
            second = self.client.post("/reports/demo/render", json={"token": response["token"]})
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.content.startswith(b"%PDF"))
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(response["payload"]["data_mode"], "DEMO")
        self.assertIsNone(response["payload"]["branding"]["logo"])
        self.assertEqual(response["payload"]["branding"]["name"], "OneMarineX")

    def test_demo_tokens_are_not_access_or_refresh_tokens(self):
        token = self.preview()["token"]
        self.assertIsNone(decode_subject(token))
        self.assertIsNone(verify_refresh_token(token))

    def test_token_cannot_cross_actor_or_company_and_rejects_signature_changes(self):
        token = self.preview()["token"]
        self.actor.id = 12
        self.assertEqual(self.client.post("/reports/demo/render", json={"token": token}).status_code, 403)
        self.actor.id = 11
        self.company.id = 8
        self.assertEqual(self.client.post("/reports/demo/render", json={"token": token}).status_code, 403)
        self.company.id = 7
        parts = token.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        self.assertEqual(self.client.post("/reports/demo/render", json={"token": ".".join(parts)}).status_code, 403)

    def test_expired_token_and_arbitrary_client_payload_are_rejected(self):
        issued = routes.issue_preview(routes.DemoReportIn(**self.params), actor_id=11, company_id=7, anchor=self.anchor - timedelta(minutes=31))
        self.assertEqual(self.client.post("/reports/demo/render", json={"token": issued["token"]}).status_code, 403)
        self.assertEqual(self.client.post("/reports/demo/preview", json={**self.params, "calls": [{"private": "injected"}]}).status_code, 422)
        good = self.preview()
        self.assertEqual(self.client.post("/reports/demo/render", json={"token": good["token"], "payload": {"company_name": "Injected"}}).status_code, 422)

    def test_shared_statistics_deduplicate_synthetic_people_across_months(self):
        fixture = create_demo_data(self.anchor)
        result = survey_statistics(fixture["surveys"])
        self.assertEqual(result["respondents"], 40)
        self.assertEqual(result["responses"], 120)
        self.assertEqual(result["satisfaction"]["denominator"], 34)
        self.assertEqual(result["satisfaction"]["numerator"], 28)
        report = prepare(self.params, self.anchor)
        driver = next(g for g in report["service_reviews"]["groups"] if g["type"] == "driver")
        self.assertEqual(driver["respondents"], 26)
        self.assertEqual(driver["reviews"], 78)
        self.assertGreater(report["operations"]["average_time_ashore_minutes"], 0)
        self.assertEqual(report["operations"]["distinct_crew"], 72)
        self.assertIsNone(report["safety"]["incident_count"])
        self.assertEqual(report["safety"]["cases"], [])

    def test_partial_day_filters_use_explicit_generated_event_times(self):
        fixture = create_demo_data(self.anchor)
        moment = datetime.fromisoformat(fixture["trips"][10]["created_at"])
        params = {**self.params, "start": moment.isoformat(), "end": (moment + timedelta(minutes=1)).isoformat()}
        report = prepare(params, self.anchor)
        expected = sum(moment <= datetime.fromisoformat(t["created_at"]) < moment + timedelta(minutes=1) for t in fixture["trips"])
        self.assertEqual(report["summary"]["booked_rides"], expected)
        self.assertNotIn("_fixture_surveys", report)

    def test_call_template_requires_exact_in_scope_call(self):
        bad = self.client.post("/reports/demo/preview", json={**self.params, "report_type": "CALL"})
        self.assertEqual(bad.status_code, 422)
        good = self.client.post("/reports/demo/preview", json={**self.params, "report_type": "CALL", "call_id": 1, "vessel_id": 1})
        self.assertEqual(good.status_code, 200)
        self.assertEqual(len(good.json()["payload"]["calls"]), 1)
        mismatch = self.client.post("/reports/demo/preview", json={**self.params, "report_type": "CALL", "call_id": 1, "vessel_id": 2})
        self.assertEqual(mismatch.status_code, 422)

    def test_live_review_summary_reuses_reducer_and_keeps_query_scope(self):
        from app.services.welfare_analytics import review_summary
        rows = [SimpleNamespace(review_type="driver", crew_id=i, rating=4) for i in range(7)]
        query = Mock()
        query.yield_per.return_value = iter(rows)
        with patch("app.services.welfare_analytics.reviews_query", return_value=query) as source:
            result = review_summary(None, self.anchor - timedelta(days=1), self.anchor, company_id=7, call_id=9)
        self.assertEqual(result, summarize_review_rows(rows, suppress=True))
        self.assertEqual(source.call_args.args[3], 7)
        self.assertEqual(source.call_args.args[-1], 9)


if __name__ == "__main__":
    unittest.main()
