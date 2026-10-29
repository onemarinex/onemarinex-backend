"""Independent-scope acceptance on the guarded disposable PostgreSQL database."""

import io
import unittest
from datetime import timedelta
from urllib.parse import urlencode
from openpyxl import Workbook
import test_company_operations_http as fixture
import test_welfare_feedback_http as welfare_fixture
from app.api.v1 import (
    routes_company_fleet as fleet,
    routes_company_intelligence as intelligence,
    routes_welfare as welfare,
    routes_company_reports as reports,
    routes_company_disclosure as disclosure,
)
from app.db.models.agency_transfer import AgencyTransfer
from app.db.models.booking_review import BookingReview
from app.db.models.shipping_company import (
    VesselRoleClaim,
    CompanyAuditEvent,
    CompanyUserMembership,
)
from app.db.models.company_fleet_change import CompanyFleetChange
from app.services.company_aggregates import Period


class PortalAcceptanceHTTP(unittest.TestCase):
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request
    booking = fixture.OperationsHTTP.booking
    person = welfare_fixture.WelfareHTTP.person

    def setUp(self):
        fixture.OperationsHTTP.setUp(self)
        for router, prefix in [
            (fleet.router, "/api/v1/shipping-company/fleet"),
            (fleet.admin_router, "/api/v1/superadmin"),
            (intelligence.router, "/api/v1/shipping-company/intelligence"),
            (welfare.crew_router, "/api/v1/crew"),
            (welfare.company_router, "/api/v1/shipping-company/welfare-intelligence"),
            (welfare.admin_router, "/api/v1/superadmin/welfare-intelligence"),
            (reports.router, "/api/v1/shipping-company/report-centre"),
            (disclosure.company_router, "/api/v1/shipping-company"),
            (disclosure.admin_router, "/api/v1/superadmin"),
        ]:
            self.client.app.include_router(router, prefix=prefix)

    def period(self, **extra):
        return urlencode(
            dict(
                start=(fixture.NOW - timedelta(days=1)).isoformat(),
                end=(fixture.NOW + timedelta(days=1)).isoformat(),
                **extra,
            )
        )

    def test_all_activity_types_are_paged_and_do_not_name_people(self):
        self.assignment.signed_on_at = fixture.NOW
        self.booking()
        self.db.add(
            AgencyTransfer(
                agency_id=self.agency.id,
                vessel_call_id=self.call.id,
                provider_id=self.provider.id,
                assignment_ids=[self.assignment.id],
                movement_type="SIGN_OFF",
                pickup="Private airport",
                destination="Private gate",
                travel_at=fixture.NOW,
                status="COMPLETED",
                created_by_user_id=self.agent.id,
            )
        )
        self.db.commit()
        base = "/api/v1/shipping-company/intelligence/calls/1/activities"
        data = self.request("GET", base + "?kind=ALL&limit=2", self.admin).json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["items"]), 2)
        rows = (
            data["items"]
            + self.request(
                "GET", base + "?kind=ALL&offset=2&limit=2", self.admin
            ).json()["items"]
        )
        self.assertEqual(
            {r["record_type"] for r in rows},
            {"transport", "confirmed_movement", "shore_leave"},
        )
        self.assertNotIn("Synthetic crew", str(rows))
        self.assertNotIn("Private airport", str(rows))
        self.assertEqual(
            self.request("GET", base + "?kind=SIGN_ON", self.admin).json()["total"], 1
        )
        self.assertEqual(
            self.request("GET", base + "?kind=SHORE_LEAVE", self.admin).json()["total"],
            1,
        )

    def test_historical_ride_snapshot_uses_milestones_not_current_status(self):
        self.booking(
            status="completed",
            trip_started_at=fixture.JAN + timedelta(hours=1),
            trip_completed_at=fixture.JAN + timedelta(hours=3),
            created_at=fixture.JAN,
        )
        metrics = intelligence.call_metrics(
            self.db,
            self.company.id,
            self.call,
            Period(fixture.JAN, fixture.JAN + timedelta(hours=2)),
        )
        self.assertEqual(metrics["active_rides"], 1)

    def test_country_mapping_is_admin_only_and_filters_all_sources(self):
        path = f"/api/v1/superadmin/welfare-intelligence/port-geography/{self.port.id}"
        body = {
            "country_code": "IN",
            "evidence_reference": "Reviewed fixture port record",
        }
        self.request("PUT", path, self.admin, body, 403)
        self.request("PUT", path, self.sa, body)
        scope = self.request(
            "GET", "/api/v1/shipping-company/welfare-intelligence/scope", self.admin
        ).json()
        self.assertEqual(scope["countries"], ["IN"])
        self.assertEqual(
            self.request(
                "GET",
                "/api/v1/shipping-company/welfare-intelligence/operations?"
                + self.period(country_code="US"),
                self.admin,
            ).json()["operations"]["recorded_calls"],
            0,
        )
        self.assertEqual(
            self.request(
                "GET",
                "/api/v1/shipping-company/welfare-intelligence/operations?"
                + self.period(country_code="IN"),
                self.admin,
            ).json()["operations"]["recorded_calls"],
            1,
        )
        self.assertEqual(
            self.db.query(CompanyAuditEvent)
            .filter_by(action="port_country_recorded")
            .count(),
            1,
        )

    def test_legacy_feedback_date_and_port_filters_do_not_reclassify_rows(self):
        from app.db.models.incident import Incident, IncidentType

        row = Incident(
            title="Crew Feedback",
            type=IncidentType.CREW,
            description="Retained original",
            created_at=fixture.JAN,
            port_id=self.port.id,
            vessel_id=self.vessel.id,
        )
        self.db.add(row)
        self.db.commit()
        data = self.request(
            "GET",
            "/api/v1/superadmin/welfare-intelligence/legacy-feedback?" + self.period(),
            self.sa,
        ).json()
        self.assertEqual(data["total"], 0)
        self.assertEqual(self.db.get(Incident, row.id).title, "Crew Feedback")

    def test_port_comparison_preserves_empty_activity_and_survey_privacy(self):
        data = self.request(
            "GET",
            "/api/v1/shipping-company/welfare-intelligence/comparisons?"
            + self.period(),
            self.admin,
        ).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["operations"]["crew_participation"], 0)
        self.assertTrue(data["items"][0]["survey"]["suppressed"])

    def test_general_feedback_cannot_smuggle_structured_answers_without_call(self):
        crew, _ = self.person(0)
        for field, value in [
            ("satisfaction", 5),
            ("nonparticipation_reason", "WORK"),
            ("currency", "USD"),
        ]:
            self.request(
                "POST",
                "/api/v1/crew/welfare-feedback",
                crew,
                {"message": "General message", field: value},
                422,
            )

    def test_registry_search_and_paging_are_tenant_scoped(self):
        path = "/api/v1/shipping-company/fleet/registry"
        self.assertEqual(
            self.request("GET", path + "?search=Fixture", self.admin).json()["total"], 1
        )
        self.assertEqual(
            self.request("GET", path + "?search=absent", self.admin).json()["total"], 0
        )
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update(
            {"enabled": False}
        )
        self.db.commit()
        self.request("GET", path, self.admin, status=403)

    def test_csv_and_xlsx_preview_report_bad_rows_and_grant_no_access(self):
        original = self.db.query(VesselRoleClaim).count()
        content = "IMO,role,start,end\nUNKNOWN,ISM_MANAGER,2026-01-01,\nIMO-FIXTURE,ISM_MANAGER,bad-date,\n"
        path = "/api/v1/shipping-company/fleet/import-preview"
        response = self.client.post(
            path,
            headers=self.headers(self.admin),
            files={"file": ("fleet.csv", content, "text/csv")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        self.assertFalse(any(f["ok"] for f in response.json()["findings"]))
        book = Workbook()
        book.active.append(["IMO", "role", "start", "end"])
        book.active.append(["=1+1", "ISM_MANAGER", "2026-01-01", None])
        out = io.BytesIO()
        book.save(out)
        response = self.client.post(
            path,
            headers=self.headers(self.admin),
            files={
                "file": (
                    "fleet.xlsx",
                    out.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["findings"][0]["ok"])
        self.assertEqual(self.db.query(VesselRoleClaim).count(), original)
        self.assertEqual(
            self.client.post(
                path,
                headers=self.headers(self.agent),
                files={"file": ("fleet.csv", content, "text/csv")},
            ).status_code,
            403,
        )

    def test_fleet_change_requires_review_and_retains_original(self):
        claim = self.db.query(VesselRoleClaim).one()
        body = {
            "action": "END",
            "effective_end": fixture.JUL.isoformat(),
            "reason": "Company reports management handover",
        }
        change = self.request(
            "POST",
            f"/api/v1/shipping-company/fleet/claims/{claim.id}/changes",
            self.admin,
            body,
            201,
        ).json()
        self.db.refresh(claim)
        self.assertIsNone(claim.effective_end)
        decision = {"approve": True, "reason": "Independent handover evidence checked"}
        self.request(
            "POST",
            f"/api/v1/superadmin/fleet-changes/{change['id']}/decision",
            self.admin,
            decision,
            403,
        )
        self.request(
            "POST",
            f"/api/v1/superadmin/fleet-changes/{change['id']}/decision",
            self.sa,
            decision,
        )
        self.db.refresh(claim)
        self.assertEqual(claim.status, "CORRECTED")
        new = self.db.query(VesselRoleClaim).filter_by(status="APPROVED").one()
        self.assertEqual(new.effective_end, fixture.JUL)
        self.assertEqual(self.db.query(CompanyFleetChange).one().status, "APPROVED")
        self.request(
            "POST",
            f"/api/v1/superadmin/fleet-changes/{change['id']}/decision",
            self.sa,
            decision,
            409,
        )

    def test_fleet_change_duplicate_and_non_admin_submission_are_denied(self):
        claim = self.db.query(VesselRoleClaim).one()
        body = {"action": "VOID", "reason": "Duplicate declaration requires withdrawal"}
        path = f"/api/v1/shipping-company/fleet/claims/{claim.id}/changes"
        self.request("POST", path, self.admin, body, 201)
        self.request("POST", path, self.admin, body, 409)
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update(
            {"role": "VIEWER"}
        )
        self.db.commit()
        self.request("POST", path, self.admin, body, 403)

    def test_report_templates_have_real_distinct_compositions_and_frozen_csv(self):
        self.booking()
        body = {
            "start": fixture.JAN.isoformat(),
            "end": (fixture.NOW + timedelta(days=1)).isoformat(),
            "report_type": "FLEET",
        }
        for kind in ["FLEET", "PORT", "CALL", "WELFARE"]:
            data = self.request(
                "POST",
                "/api/v1/shipping-company/report-centre/preview",
                self.admin,
                dict(
                    body,
                    report_type=kind,
                    call_id=self.call.id if kind == "CALL" else None,
                ),
            ).json()
            self.assertEqual(data["summary"]["booked_rides"], 1)
            if kind in ("FLEET", "PORT"):
                self.assertEqual(
                    data["comparison"]["dimension"],
                    "vessel" if kind == "FLEET" else "port",
                )
            if kind == "WELFARE":
                self.assertIn("survey", data)
                self.assertIn("operations", data)
        saved = self.request(
            "POST", "/api/v1/shipping-company/report-centre", self.admin, body, 201
        ).json()
        self.booking()
        csv = self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{saved['id']}/export?format=csv",
            self.admin,
        )
        self.assertIn("Booked rides,1", csv.text)
        self.assertEqual(csv.headers["cache-control"], "private, no-store")

    def test_source_review_release_is_item_scoped_and_edit_fails_closed(self):
        crew, assignment = self.person(0)
        b = self.booking(crew_id=assignment.crew_profile_id)
        review = BookingReview(
            booking_id=b.id,
            crew_id=assignment.crew_profile_id,
            review_type="driver",
            rating=4,
            review_text="Original Alice comment",
            created_at=fixture.NOW,
        )
        self.db.add(review)
        self.db.commit()
        body = {
            "source_kind": "CALL",
            "source_id": self.call.id,
            "fields": ["reviews.text", "reviews.rating"],
            "event_start": fixture.JAN.isoformat(),
            "event_end": (fixture.NOW + timedelta(hours=1)).isoformat(),
            "reason": "Review the exact original service comment",
        }
        rid = self.request(
            "POST",
            "/api/v1/shipping-company/disclosure-requests",
            self.admin,
            body,
            201,
        ).json()["id"]
        scope = {
            "action": "APPROVE",
            "fields": ["reviews.text"],
            "review_ids": [review.id],
            "redact_terms": ["Alice"],
            "reason": "Approve this reviewed comment only",
            "expires_at": (fixture.NOW + timedelta(hours=1)).isoformat(),
        }
        endpoint = f"/api/v1/superadmin/disclosure-requests/{rid}"
        self.request("POST", endpoint + "/decision", self.sa, scope, 409)
        preview = self.request("POST", endpoint + "/preview", self.sa, scope).json()
        self.request(
            "POST",
            endpoint + "/decision",
            self.sa,
            dict(scope, preview_sha256=preview["preview_sha256"]),
        )
        path = f"/api/v1/shipping-company/disclosure-requests/{rid}/data"
        response = self.request("GET", path, self.admin)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(
            response.json()["data"]["reviews"],
            [{"text": "Original [redacted] comment"}],
        )
        self.assertNotIn("rides", response.json()["data"])
        review.review_text = "New sensitive content"
        self.db.commit()
        self.assertEqual(
            self.request("GET", path, self.admin).json()["data"]["reviews"], []
        )

    def test_additive_migrations_create_fresh_tables_and_preserve_existing_rows(self):
        import importlib.util
        from pathlib import Path
        from unittest.mock import patch
        from sqlalchemy import text
        from app.db.session import engine

        company_id = self.company.id
        self.db.rollback()
        with engine.begin() as c:
            c.execute(text("SET LOCAL lock_timeout = '5s'"))
            before = c.execute(
                text("SELECT id,email,hashed_password FROM users ORDER BY id")
            ).all()
            c.execute(
                text(
                    "DROP TABLE company_fleet_changes, port_geography, company_management_reports, company_disclosure_requests, company_safety_escalations, welfare_feedback, company_onboarding_drafts, company_profiles"
                )
            )
            migrations = []
            for name in [
                "f2a3b4c5d6e7_company_profiles.py",
                "g3b4c5d6e7f8_welfare_feedback.py",
                "h4c5d6e7f8a9_company_disclosures_reports.py",
                "i5d6e7f8a9b0_port_geography.py",
                "j6e7f8a9b0c1_company_fleet_changes.py",
            ]:
                spec = importlib.util.spec_from_file_location(
                    "fixture_migration_" + name[:-3], Path("alembic/versions") / name
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                migrations.append(module)
                with patch.object(module.op, "execute", side_effect=c.execute):
                    module.upgrade()
            c.execute(
                text(
                    "INSERT INTO company_profiles (company_id,corporate_details) VALUES (:company,CAST(:data AS JSON))"
                ),
                {"company": company_id, "data": '{"retained":"sentinel"}'},
            )
            for module in migrations:
                with patch.object(module.op, "execute", side_effect=c.execute):
                    module.upgrade()
                    module.downgrade()
            after = c.execute(
                text("SELECT id,email,hashed_password FROM users ORDER BY id")
            ).all()
            self.assertEqual(before, after)
            self.assertEqual(
                c.execute(
                    text("SELECT corporate_details FROM company_profiles")
                ).scalar(),
                {"retained": "sentinel"},
            )

    def test_shortened_tenure_blocks_a_previously_saved_broader_report(self):
        saved = self.request(
            "POST",
            "/api/v1/shipping-company/report-centre",
            self.admin,
            {
                "report_type": "FLEET",
                "start": fixture.JAN.isoformat(),
                "end": (fixture.NOW + timedelta(days=1)).isoformat(),
            },
            201,
        ).json()
        claim = self.db.query(VesselRoleClaim).one()
        claim.effective_end = fixture.JUL
        self.db.commit()
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{saved['id']}",
            self.admin,
            status=403,
        )
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{saved['id']}/export?format=pdf",
            self.admin,
            status=403,
        )

    def test_saved_report_content_cannot_change_under_its_existing_hash(self):
        from app.db.models.company_disclosure import CompanyManagementReport

        saved = self.request(
            "POST",
            "/api/v1/shipping-company/report-centre",
            self.admin,
            {
                "report_type": "FLEET",
                "start": fixture.JAN.isoformat(),
                "end": (fixture.NOW + timedelta(days=1)).isoformat(),
            },
            201,
        ).json()
        report = self.db.get(CompanyManagementReport, saved["id"])
        report.payload = dict(report.payload, title="Unexpected edit")
        self.db.commit()
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{saved['id']}",
            self.admin,
            status=409,
        )

    def test_review_type_and_provider_filter_apply_to_the_same_source_rows(self):
        _, assignment = self.person(0)
        b = self.booking(
            crew_id=assignment.crew_profile_id, provider_id=self.provider.id
        )
        self.db.add(
            BookingReview(
                booking_id=b.id,
                crew_id=assignment.crew_profile_id,
                review_type="driver",
                rating=4,
                review_text="Original source",
                created_at=fixture.NOW,
            )
        )
        self.db.commit()
        base = "/api/v1/superadmin/welfare-intelligence/"
        self.assertEqual(
            self.request(
                "GET",
                base
                + "reviews?"
                + self.period(review_type="driver", provider_id=self.provider.id),
                self.sa,
            ).json()["total"],
            1,
        )
        self.assertEqual(
            self.request(
                "GET",
                base
                + "reviews?"
                + self.period(
                    review_type="facility_stop", provider_id=self.provider.id
                ),
                self.sa,
            ).json()["total"],
            0,
        )
        self.assertEqual(
            self.request(
                "GET",
                base
                + "operations?"
                + self.period(review_type="driver", provider_id=self.provider2.id),
                self.sa,
            ).json()["reviews"]["groups"],
            [],
        )
        choices = self.request(
            "GET",
            "/api/v1/shipping-company/welfare-intelligence/review-providers?"
            + self.period(),
            self.admin,
        ).json()
        self.assertEqual(
            choices["items"], [{"id": self.provider.id, "name": "Provider"}]
        )

    def test_port_cards_have_current_figures_search_and_active_service_scope(self):
        self.request(
            "PUT",
            f"/api/v1/superadmin/welfare-intelligence/port-geography/{self.port.id}",
            self.sa,
            {"country_code": "IN", "evidence_reference": "Reviewed fixture geography"},
        )
        path = "/api/v1/shipping-company/intelligence/ports/page"
        page = self.request("GET", path + "?search=Fixture&limit=25", self.admin).json()
        self.assertEqual(page["total"], 1)
        port = page["items"][0]
        self.assertEqual(
            (
                port["country_code"],
                port["active_vessels"],
                port["crew_ashore"],
                port["active_rides"],
            ),
            ("IN", 1, 0, 0),
        )
        self.assertEqual(
            self.request("GET", path + "?search=absent", self.admin).json()["total"], 0
        )
        self.db.query(VesselRoleClaim).update({"effective_end": fixture.JUL})
        self.db.commit()
        self.assertEqual(self.request("GET", path, self.admin).json()["total"], 0)
        history = self.request("GET", path + "?active=false", self.admin).json()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["active_vessels"], 0)
        self.port.is_active = False
        self.db.commit()
        self.assertEqual(
            self.request("GET", path + "?active=false", self.admin).json()["total"], 1
        )

    def test_registry_uses_the_canonical_vessel_imo(self):
        self.assertIsNone(self.db.query(VesselRoleClaim).one().imo_number)
        data = self.request(
            "GET", "/api/v1/shipping-company/fleet/registry", self.admin
        ).json()
        self.assertEqual(data["items"][0]["imo_number"], self.vessel.imo_number)

    def test_welfare_call_sql_pages_keep_full_period_totals(self):
        for index in range(26):
            if index == 0:
                vessel, call = self.vessel, self.call
            else:
                vessel = fixture.Vessel(
                    name=f"Page vessel {index}",
                    imo_number=f"IMO-PAGE-{index}",
                    vessel_type="Fixture",
                    agent_id=self.agent.id,
                )
                self.db.add(vessel)
                self.db.flush()
                call = fixture.VesselCall(
                    vessel_id=vessel.id,
                    agency_id=self.agency.id,
                    port_id=self.port.id,
                    vessel_name=vessel.name,
                    port_name=self.port.name,
                    started_at=fixture.JAN,
                )
                self.db.add(call)
                self.db.flush()
                self.db.add(
                    VesselRoleClaim(
                        company_id=self.company.id,
                        vessel_id=vessel.id,
                        role="ISM_MANAGER",
                        effective_start=fixture.JAN,
                        status="APPROVED",
                    )
                )
            self.booking(
                vessel_id=vessel.id, vessel_call_id=call.id, trip_started_at=fixture.NOW
            )
        self.db.commit()
        path = (
            "/api/v1/shipping-company/welfare-intelligence/operations?" + self.period()
        )
        first = self.request(
            "GET", path + "&call_offset=0&call_limit=25", self.admin
        ).json()["operations"]
        second = self.request(
            "GET", path + "&call_offset=25&call_limit=25", self.admin
        ).json()["operations"]
        self.assertEqual((len(first["calls"]), len(second["calls"])), (25, 1))
        self.assertEqual(first["calls_total"], 26)
        self.assertEqual(second["calls_total"], 26)
        self.assertEqual(first["crew_participation"], 78)
        self.assertEqual(second["crew_participation"], 78)
        self.assertFalse(
            {c["call_id"] for c in first["calls"]}
            & {c["call_id"] for c in second["calls"]}
        )
