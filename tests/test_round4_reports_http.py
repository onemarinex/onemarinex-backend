"""Round 4 real HTTP regressions. Run only in the guarded disposable fixture."""
import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlencode

import test_company_operations_http as fixture
import test_welfare_feedback_http as welfare_fixture
import test_round3_welfare_http as round3_fixture
from app.api.v1 import routes_company_reports as reports
from app.api.v1.routes_welfare import statistics
from app.db.models.company_disclosure import CompanyManagementReport
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.port import Port
from app.db.models.shipping_company import VesselRoleClaim
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.welfare_feedback import WelfareFeedback

BASE = "/api/v1/shipping-company/report-centre"


class Round4ReportsHTTP(unittest.TestCase):
    tearDown = welfare_fixture.WelfareHTTP.tearDown
    headers = welfare_fixture.WelfareHTTP.headers
    request = welfare_fixture.WelfareHTTP.request
    person = welfare_fixture.WelfareHTTP.person
    body = welfare_fixture.WelfareHTTP.body
    populate = round3_fixture.Round3WelfareHTTP.populate

    def setUp(self):
        welfare_fixture.WelfareHTTP.setUp(self)
        self.client.app.include_router(reports.router, prefix=BASE)

    def report_body(self, **extra):
        return dict(report_type="WELFARE", start="2026-10-01T00:00:00Z",
                    end="2026-11-01T00:00:00Z", **extra)

    def preview(self, **extra):
        return self.request("POST", BASE + "/preview", self.admin, self.report_body(**extra)).json()

    def dashboard(self, **extra):
        return self.request("GET", "/api/v1/shipping-company/welfare-intelligence/summary?" + urlencode(
            dict(start="2026-10-01T00:00:00Z", end="2026-11-01T00:00:00Z", **extra)), self.admin).json()

    def overlap(self):
        original = self.db.query(WelfareFeedback).order_by(WelfareFeedback.id).all()
        other, _ = self.person(5)
        port = Port(name="Other authorized port", code="round4-other")
        vessel = Vessel(name="Other authorized vessel", imo_number="ROUND4-OTHER", vessel_type="Bulk Carrier", status="Active", agent_id=self.agent.id)
        self.db.add_all([port, vessel]); self.db.flush()
        self.db.add(VesselRoleClaim(company_id=self.company.id, vessel_id=vessel.id, role="ISM_MANAGER", effective_start=fixture.JAN, status="APPROVED"))
        call = VesselCall(vessel_id=vessel.id, agency_id=self.agency.id, port_id=port.id, vessel_name=vessel.name, port_name=port.name, started_at=fixture.JAN)
        self.db.add(call); self.db.flush()
        for who in [row.user_id for row in original[:4]] + [other.id]:
            crew = self.db.query(CrewProfile).filter_by(user_id=who).one()
            assignment = CrewAssignment(vessel_call_id=call.id, crew_profile_id=crew.id, crew_name="Synthetic", passport_number=crew.passport_number, nationality="IN", source="MANIFEST", started_at=fixture.JAN)
            self.db.add(assignment); self.db.flush()
            self.db.add(WelfareFeedback(user_id=who, assignment_id=assignment.id, vessel_call_id=call.id, vessel_id=vessel.id, port_id=port.id, experience_at=fixture.NOW, answers=original[0].answers, message="Good service"))
        self.db.commit()
        return port

    def test_safe_dashboard_report_port_and_trend_agree(self):
        self.populate()
        dashboard, report = self.dashboard(), self.preview()
        self.assertEqual(dashboard["respondents"], 5)
        for key in ("respondents", "status", "obstacles", "satisfaction", "barriers"):
            self.assertEqual(report["survey"][key], dashboard[key])
        self.assertEqual(report["port_comparison"][0]["respondents"], 5)
        chart = next(c for c in report["charts"] if c["section"] == "obstacles")
        self.assertEqual(chart["items"][0]["value"], dashboard["obstacles"]["percent"])

    def test_overlapping_global_cells_stay_hidden_in_all_report_reducers(self):
        self.populate(); self.overlap()
        for filters in ({"port_id": self.port.id}, {"vessel_id": self.vessel.id}, {}):
            report, dashboard = self.preview(**filters), self.dashboard(**filters)
            self.assertTrue(dashboard["suppressed"])
            self.assertEqual(report["survey"]["respondents"], dashboard["respondents"])
            self.assertTrue(all(row["respondents"] is None for row in report["port_comparison"]))
            for chart in report["charts"]:
                if chart["section"] in ("obstacles", "crew_experience"):
                    self.assertTrue(all(row["value"] is None for row in chart["items"]))

    def test_changed_global_cohort_blocks_saved_json_pdf_and_csv_without_rewriting(self):
        self.populate()
        saved = self.request("POST", BASE, self.admin, self.report_body(), 201).json()
        original = copy.deepcopy(saved["payload"])
        self.request("GET", BASE + f"/{saved['id']}/preview.pdf", self.admin)
        self.overlap()
        for suffix in ("", "/preview.pdf", "/export?format=pdf", "/export?format=csv"):
            self.request("GET", BASE + f"/{saved['id']}" + suffix, self.admin, status=403)
        self.db.expire_all()
        row = self.db.get(CompanyManagementReport, saved["id"])
        self.assertEqual(row.payload, original)
        self.assertEqual(row.payload_sha256, saved["payload_sha256"])
        self.assertEqual(self.request("GET", BASE, self.admin).json()["total"], 1)

    def test_unsafe_legacy_snapshot_cannot_bypass_current_publication_policy(self):
        self.populate(); self.overlap()
        saved = self.request("POST", BASE, self.admin, self.report_body(port_id=self.port.id), 201).json()
        row = self.db.get(CompanyManagementReport, saved["id"])
        legacy = copy.deepcopy(row.payload)
        local_rows = self.db.query(WelfareFeedback).filter_by(port_id=self.port.id).all()
        legacy["survey"] = statistics(local_rows, True)  # The pre-fix reducer.
        legacy.pop("survey_publication_fingerprint", None)
        self.assertEqual(legacy["survey"]["respondents"], 5)
        row.payload = legacy
        row.payload_sha256 = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        self.db.commit()
        for suffix in ("", "/preview.pdf", "/export?format=csv"):
            self.request("GET", BASE + f"/{saved['id']}" + suffix, self.admin, status=403)

    def test_equal_totals_with_different_people_do_not_revalidate_old_release(self):
        from app.services.company_report_data import welfare_projection
        self.populate()
        saved = self.request("POST", BASE, self.admin, self.report_body(), 201).json()
        self.db.query(WelfareFeedback).delete(); self.db.commit()
        for n in range(5, 10):
            user, assignment = self.person(n)
            body = self.body(assignment)
            body["message"] = "Good service"
            self.request("POST", "/api/v1/crew/welfare-feedback", user, body, 201)
        current = self.preview()
        self.assertEqual(welfare_projection(saved["payload"]), welfare_projection(current))
        self.assertNotEqual(saved["payload"]["survey_publication_fingerprint"], current["survey_publication_fingerprint"])
        self.request("GET", BASE + f"/{saved['id']}/export?format=csv", self.admin, status=403)

    def test_partial_day_report_labels_canonical_monthly_survey_coverage(self):
        self.populate()
        body = self.report_body()
        body.update(start="2026-10-15T12:00:00Z", end="2026-10-16T00:00:00Z")
        report = self.request("POST", BASE + "/preview", self.admin, body).json()
        self.assertEqual(datetime.fromisoformat(report["period"]["start"]), datetime.fromisoformat(body["start"].replace("Z", "+00:00")))
        self.assertTrue(report["survey"]["period"]["start"].startswith("2026-10-01"))
        self.assertTrue(report["survey"]["period"]["end"].startswith("2026-11-01"))
        self.assertEqual(report["survey"]["respondents"], 5)

    def test_unknown_port_is_separate_and_never_reuses_all_ports_survey(self):
        self.populate()
        self.call.port_id = None
        self.call.port_name = None
        self.db.commit()
        report = self.preview()
        self.assertEqual(report["summary"]["recorded_ports"], 0)
        self.assertEqual(report["summary"]["recorded_calls"], 1)
        self.assertEqual(report["summary"]["calls_without_port"], 1)
        row = report["port_comparison"][0]
        self.assertEqual(row["name"], "Port not recorded")
        self.assertIsNone(row["respondents"])
        self.assertIn("separately from named ports", report["findings"][0])

    def test_no_response_csv_and_safe_saved_snapshot_remain_available(self):
        saved = self.request("POST", BASE, self.admin, self.report_body(), 201).json()
        self.assertEqual(saved["payload"]["survey"]["status"], "no_responses")
        preview = self.request("GET", BASE + f"/{saved['id']}/preview.pdf", self.admin)
        download = self.request("GET", BASE + f"/{saved['id']}/export?format=pdf", self.admin)
        self.assertEqual(preview.content, download.content)
        csv = self.request("GET", BASE + f"/{saved['id']}/export?format=csv", self.admin).text
        self.assertIn("No responses", csv)
        self.assertNotIn("Crew survey,Withheld", csv)

    def test_barriers_rank_positive_counts_before_alphabetical_zero_categories(self):
        self.populate(barriers=["transport_unavailable"])
        result = self.dashboard()
        self.assertEqual(result["barriers"][0]["code"], "transport_unavailable")
        self.assertEqual(result["barriers"][0]["respondents"], 5)
        self.assertTrue(all(row["respondents"] > 0 for row in result["barriers"]))

    def test_exact_call_cannot_split_a_canonical_cell_into_five_plus_one(self):
        self.populate()
        user, _ = self.person(5)
        older = VesselCall(vessel_id=self.vessel.id, agency_id=self.agency.id,
                           port_id=self.port.id, vessel_name=self.vessel.name,
                           port_name=self.port.name, started_at=fixture.NOW-timedelta(days=3),
                           ended_at=fixture.NOW-timedelta(days=1))
        self.db.add(older); self.db.flush()
        crew = self.db.query(CrewProfile).filter_by(user_id=user.id).one()
        assignment = CrewAssignment(vessel_call_id=older.id, crew_profile_id=crew.id,
                                   crew_name="Synthetic", passport_number=crew.passport_number,
                                   nationality="IN", source="MANIFEST", started_at=older.started_at)
        self.db.add(assignment); self.db.flush()
        answer = self.db.query(WelfareFeedback).first()
        self.db.add(WelfareFeedback(user_id=user.id, assignment_id=assignment.id,
                                   vessel_call_id=older.id, vessel_id=self.vessel.id,
                                   port_id=self.port.id, experience_at=fixture.NOW-timedelta(days=2),
                                   answers=answer.answers, message="Good service"))
        self.db.commit()
        self.assertEqual(self.dashboard()["respondents"], 6)
        body = self.report_body(); body.update(report_type="CALL", call_id=self.call.id)
        report = self.request("POST", BASE+"/preview", self.admin, body).json()
        self.assertTrue(report["survey"]["suppressed"])
        self.assertIsNone(report["survey"]["respondents"])

    def test_exact_call_with_complete_canonical_cells_keeps_safe_figures(self):
        self.populate()
        body = self.report_body(); body.update(report_type="CALL", call_id=self.call.id)
        report = self.request("POST", BASE+"/preview", self.admin, body).json()
        self.assertEqual(report["survey"]["respondents"], 5)
        self.assertFalse(report["survey"]["suppressed"])

    def test_unanswered_question_csv_stays_distinct_from_small_answer_sample(self):
        self.populate()
        saved = self.request("POST", BASE, self.admin, self.report_body(), 201).json()
        csv = self.request("GET", BASE + f"/{saved['id']}/export?format=csv", self.admin).text
        self.assertIn("Satisfaction,No responses", csv)
