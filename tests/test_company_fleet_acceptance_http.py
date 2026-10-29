"""The decided aggregate-only boundary, real JWT/HTTP/PostgreSQL and bulk files."""

import csv
import io
import unittest
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from app.db.models.vessel import Vessel
from app.db.models.shipping_company import (
    VesselRoleClaim,
    ShippingCompany,
    CompanyUserMembership,
)
from app.services.company_access import covering_claim
from app.services.company_aggregates import tenure_scoped
from app.db.models.cab_booking import CabBooking
from app.api.v1.routes_company_fleet_workspace import LABELS
import test_company_portal_v13_http as portal_fixture
import test_company_operations_http as fixture

BASE = "/api/v1/shipping-company/fleet"


def number(i):
    stem = str(900000 + i)
    return stem + str(sum(int(stem[n]) * (7 - n) for n in range(6)) % 10)


class FleetAcceptanceHTTP(unittest.TestCase):
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request
    booking = fixture.OperationsHTTP.booking
    period = portal_fixture.PortalAcceptanceHTTP.period

    def test_voided_role_combination_stays_one_historical_row(self):
        body = self.declaration()
        body.update(commercial_manager=True)
        entry = self.add(body)
        self.request(
            "POST",
            BASE + f"/entries/{entry['id']}/change",
            self.admin,
            {
                "action": "VOID",
                "revision": entry["revision"],
                "reason": "Both role declarations were added by mistake",
            },
        )
        history = self.request(
            "GET", BASE + "/entries?view=historical", self.admin
        ).json()["items"]
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0]["owner"])
        self.assertTrue(history[0]["commercial_manager"])

    def test_separately_added_roles_share_one_row_and_edit_as_one_combination(self):
        first = self.add()
        second_body = self.declaration()
        second_body.update(owner=False, commercial_manager=True)
        self.add(second_body)
        rows = self.request("GET", BASE + "/entries", self.admin).json()["items"]
        combined = next(r for r in rows if r["id"] == first["id"])
        self.assertEqual(
            len(rows), 2
        )  # The original fixture's differently timed legacy period stays separate.
        self.assertTrue(combined["owner"])
        self.assertTrue(combined["commercial_manager"])
        self.assertFalse(combined["ism_manager"])
        self.assertNotEqual(combined["revision"], first["revision"])
        result = self.request(
            "POST",
            BASE + f"/entries/{first['id']}/change",
            self.admin,
            {
                "action": "ROLES",
                "revision": combined["revision"],
                "effective_date": "2026-07-01",
                "owner": False,
                "commercial_manager": True,
                "ism_manager": False,
                "reason": "Owner ends and commercial management continues at handover",
            },
        ).json()
        self.assertEqual(set(result["entry"]["accepted_roles"]), {"COMMERCIAL_MANAGER"})
        historical = self.request(
            "GET", BASE + "/entries?view=historical", self.admin
        ).json()["items"]
        self.assertEqual(len(historical), 1)
        self.assertTrue(historical[0]["owner"])
        self.assertTrue(historical[0]["commercial_manager"])

    def setUp(self):
        portal_fixture.PortalAcceptanceHTTP.setUp(self)
        self.vessel.imo_number = number(0)
        self.db.commit()

    def vessel_row(self, i):
        vessel = Vessel(
            name=f"Bulk MV {i}",
            imo_number=number(i),
            vessel_type="Bulk Carrier",
            status="Active",
            agent_id=self.agent.id,
        )
        self.db.add(vessel)
        return vessel

    def declaration(self, **extra):
        return dict(
            imo_number=self.vessel.imo_number,
            vessel_name=self.vessel.name,
            owner=True,
            commercial_manager=False,
            ism_manager=False,
            start_date="2026-01-01",
            end_date=None,
            **extra,
        )

    def add(self, body=None, actor=None, status=201):
        return self.request(
            "POST",
            BASE + "/entries",
            actor or self.admin,
            body or self.declaration(),
            status,
        ).json()

    def upload(self, rows, actor=None):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(LABELS)
        writer.writerows(rows)
        r = self.client.post(
            BASE + "/imports",
            headers=self.headers(actor or self.admin),
            files={"file": ("private.csv", out.getvalue().encode(), "text/csv")},
        )
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def preview(self, draft):
        return self.request(
            "POST",
            BASE + f"/imports/{draft['id']}/preview",
            self.admin,
            {"mapping": draft["mapping"]},
        ).json()

    def confirm(self, draft):
        return self.request(
            "POST", BASE + f"/imports/{draft['id']}/confirm", self.admin
        ).json()

    def test_500_uncontested_rows_do_not_create_500_review_tasks(self):
        for i in range(1, 501):
            self.vessel_row(i)
        self.db.commit()
        draft = self.upload(
            [
                [number(i), f"Bulk MV {i}", "Yes", "No", "No", "2026-01-01", ""]
                for i in range(1, 501)
            ]
        )
        self.assertEqual(self.preview(draft)["counts"]["accepted"], 500)
        result = self.confirm(draft)
        self.assertEqual(result["counts"]["accepted"], 500)
        self.assertEqual(self.confirm(draft), result)
        self.db.expire_all()
        self.assertEqual(
            self.db.query(VesselRoleClaim).filter_by(status="AUTO_ACCEPTED").count(),
            500,
        )
        self.assertEqual(
            self.request("GET", "/api/v1/superadmin/company-claims", self.sa).json(), []
        )
        page = self.request(
            "GET", BASE + "/entries?limit=100&offset=400", self.admin
        ).json()
        self.assertEqual(page["current_vessels"], 501)
        self.assertEqual(len(page["items"]), 100)
        self.assertEqual(len(page["items"][0]["accepted_roles"]), 1)

    def test_300_rows_only_ten_same_role_conflicts_enter_review(self):
        other = ShippingCompany(
            legal_name="Private competitor",
            normalized_name="PRIVATE COMPETITOR",
            status="VERIFIED",
        )
        self.db.add(other)
        self.db.flush()
        for i in range(1, 301):
            v = self.vessel_row(i)
            self.db.flush()
            if i <= 10:
                self.db.add(
                    VesselRoleClaim(
                        company_id=other.id,
                        vessel_id=v.id,
                        role="REGISTERED_OWNER",
                        effective_start=fixture.JAN,
                        status="AUTO_ACCEPTED",
                        evidence_reference="SECRET COMPETITOR PROOF",
                    )
                )
        self.db.commit()
        draft = self.upload(
            [
                [number(i), f"Bulk MV {i}", "Yes", "No", "No", "2026-01-01", ""]
                for i in range(1, 301)
            ]
        )
        preview = self.preview(draft)
        self.assertEqual(preview["counts"]["conflict"], 10)
        result = self.confirm(draft)
        self.assertEqual(result["counts"]["accepted"], 290)
        self.assertEqual(result["counts"]["conflict"], 10)
        self.assertNotIn("Private competitor", str(result))
        self.assertNotIn("SECRET COMPETITOR", str(result))
        queue = self.request("GET", "/api/v1/superadmin/company-claims", self.sa).json()
        self.assertEqual(len(queue), 10)
        self.assertEqual(queue[0]["conflict_count"], 1)

    def test_independent_roles_count_once_and_automatic_is_not_private_authority(self):
        self.db.query(VesselRoleClaim).delete()
        self.db.commit()
        body = self.declaration()
        body.update(commercial_manager=True, ism_manager=True)
        entry = self.add(body)
        self.booking()
        self.db.commit()
        self.db.expire_all()
        self.assertEqual(entry["acceptance"], "Accepted for aggregates")
        self.assertEqual(len(entry["accepted_roles"]), 3)
        self.assertIsNone(
            covering_claim(
                self.db,
                company_id=self.company.id,
                vessel_id=self.vessel.id,
                occurred_at=fixture.NOW,
            )
        )
        self.assertIsNotNone(
            covering_claim(
                self.db,
                company_id=self.company.id,
                vessel_id=self.vessel.id,
                occurred_at=fixture.NOW,
                include_declared=True,
            )
        )
        private = tenure_scoped(
            self.db,
            self.db.query(CabBooking),
            company_id=self.company.id,
            vessel_column=CabBooking.vessel_id,
            occurred_column=CabBooking.created_at,
        )
        self.assertEqual(private.count(), 0)
        overview = self.request(
            "GET", "/api/v1/shipping-company/intelligence/overview", self.admin
        ).json()
        self.assertEqual(overview["metrics"]["onboarded_vessels"], 1)
        detail = self.request(
            "GET", "/api/v1/shipping-company/intelligence/calls/1", self.admin
        ).json()
        self.assertEqual(detail["metrics"]["total_booked_rides"], 1)
        feed = self.request(
            "GET",
            "/api/v1/shipping-company/intelligence/calls/1/activities",
            self.admin,
        ).json()
        self.assertEqual(feed["items"], [])
        self.assertIn("aggregate", feed["coverage"])
        self.assertNotIn("Synthetic crew", str(detail))
        report = self.request(
            "POST",
            "/api/v1/shipping-company/report-centre/preview",
            self.admin,
            {
                "report_type": "FLEET",
                "start": fixture.JAN.isoformat(),
                "end": (fixture.NOW + timedelta(days=1)).isoformat(),
            },
        ).json()
        self.assertEqual(report["coverage"]["recorded_calls"], 1)

    def test_role_changes_close_old_interval_and_keep_continuing_roles(self):
        first = self.add()
        body = {
            "action": "ROLES",
            "revision": first["revision"],
            "effective_date": "2026-07-01",
            "owner": True,
            "commercial_manager": True,
            "ism_manager": False,
            "reason": "Management role changes at handover",
        }
        result = self.request(
            "POST", BASE + f"/entries/{first['id']}/change", self.admin, body
        ).json()
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(
            set(result["entry"]["accepted_roles"]),
            {"REGISTERED_OWNER", "COMMERCIAL_MANAGER"},
        )
        self.db.expire_all()
        old = self.db.get(VesselRoleClaim, first["id"])
        self.assertEqual(old.effective_end.isoformat(), "2026-06-30T18:30:00+00:00")
        self.request(
            "POST", BASE + f"/entries/{first['id']}/change", self.admin, body, 409
        )
        history = self.request(
            "GET", BASE + "/entries?view=historical", self.admin
        ).json()
        self.assertEqual(history["total"], 1)

    def test_void_retains_audit_and_stops_aggregate_access(self):
        self.db.query(VesselRoleClaim).delete()
        self.db.commit()
        entry = self.add()
        self.request(
            "POST",
            BASE + f"/entries/{entry['id']}/change",
            self.admin,
            {
                "action": "VOID",
                "revision": entry["revision"],
                "reason": "Added the wrong vessel in error",
            },
        )
        self.db.expire_all()
        self.assertEqual(self.db.get(VesselRoleClaim, entry["id"]).status, "REVOKED")
        self.assertEqual(
            self.request("GET", BASE + "/entries", self.admin).json()[
                "current_vessels"
            ],
            0,
        )
        self.request(
            "GET",
            "/api/v1/shipping-company/intelligence/calls/1",
            self.admin,
            status=404,
        )

    def test_invalid_unknown_duplicate_and_formula_rows_can_be_downloaded(self):
        draft = self.upload(
            [
                [number(0), self.vessel.name, "Yes", "No", "No", "2026-01-01", ""],
                [number(0), self.vessel.name, "Yes", "No", "No", "2026-01-01", ""],
                [number(888), "Unknown", "Yes", "No", "No", "2026-01-01", ""],
                ["1234568", "Invalid", "Yes", "No", "No", "2026-01-01", ""],
                [number(1), "=EVIL()", "Yes", "No", "No", "2026-01-01", ""],
            ]
        )
        result = self.preview(draft)
        self.assertEqual(
            result["counts"],
            dict(accepted=2, duplicate=1, unknown=0, invalid=2, conflict=0, identity_review=0),
        )
        error = self.request(
            "GET", BASE + f"/imports/{draft['id']}/errors.csv", self.admin
        )
        self.assertIn("'=EVIL()", error.text)
        self.assertEqual(self.confirm(draft)["counts"]["accepted"], 2)

    def test_mapping_requires_seven_distinct_columns_and_confirm_requires_review(self):
        draft = self.upload(
            [[number(0), self.vessel.name, "Yes", "No", "No", "2026-01-01", ""]]
        )
        self.request(
            "POST", BASE + f"/imports/{draft['id']}/confirm", self.admin, status=409
        )
        self.request(
            "POST",
            BASE + f"/imports/{draft['id']}/preview",
            self.admin,
            {"mapping": {f: 0 for f in draft["mapping"]}},
            422,
        )

    def test_admin_and_tenant_checks_cover_read_resume_and_write(self):
        draft = self.upload(
            [[number(0), self.vessel.name, "Yes", "No", "No", "2026-01-01", ""]]
        )
        self.request("GET", BASE + f"/imports/{draft['id']}", self.agent, status=403)
        member = (
            self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).one()
        )
        member.role = "VIEWER"
        self.db.commit()
        self.add(status=403)
        self.request("GET", BASE + f"/imports/{draft['id']}", self.admin, status=403)
        other = ShippingCompany(
            legal_name="Separate", normalized_name="SEPARATE", status="VERIFIED"
        )
        self.db.add(other)
        self.db.flush()
        member.company_id = other.id
        member.role = "ADMIN"
        self.db.commit()
        self.request("GET", BASE + f"/imports/{draft['id']}", self.admin, status=404)

    def test_missing_database_exclusion_fails_closed(self):
        from unittest.mock import patch
        from fastapi import HTTPException

        with patch(
            "app.services.company_fleet_declarations.require_fleet_constraint",
            side_effect=HTTPException(503, "Guard missing"),
        ):
            self.add(status=503)

    def test_concurrent_different_company_same_role_has_one_accepted_one_pending(self):
        other = ShippingCompany(
            legal_name="Other fleet", normalized_name="OTHER FLEET", status="VERIFIED"
        )
        self.db.add(other)
        self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin2.id).update(
            {"company_id": other.id}
        )
        self.db.commit()
        headers = [self.headers(actor) for actor in (self.admin, self.admin2)]
        body = self.declaration()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda auth: self.client.post(
                        BASE + "/entries", headers=auth, json=body
                    ),
                    headers,
                )
            )
        self.assertEqual([r.status_code for r in results], [201, 201])
        self.assertEqual(
            sorted(r.json()["statuses"][0] for r in results),
            ["AUTO_ACCEPTED", "SUBMITTED"],
        )

    def test_name_and_imo_corrections_need_review_and_never_move_calls(self):
        entry = self.add()
        request = self.request(
            "POST",
            BASE + f"/entries/{entry['id']}/change",
            self.admin,
            {
                "action": "RENAME",
                "revision": entry["revision"],
                "corrected_name": "Corrected master name",
                "reason": "The registered vessel name changed",
            },
        ).json()
        path = f"/api/v1/superadmin/fleet-changes/{request['change_id']}/decision"
        self.request(
            "POST",
            path,
            self.sa,
            {"approve": True, "reason": "Checked the independent registry record"},
            422,
        )
        self.request(
            "POST",
            path,
            self.sa,
            {
                "approve": True,
                "reason": "Checked the independent registry record",
                "evidence_reference": "Registry reference fixture 123",
            },
        )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(Vessel, self.vessel.id).name, "Corrected master name"
        )
        self.assertEqual(self.call.vessel_name, "Fixture MV")
        row = self.request("GET", BASE + "/entries?view=all", self.admin).json()[
            "items"
        ]
        self.assertEqual(
            next(r for r in row if r["id"] == entry["id"])["vessel_name"],
            "Corrected master name",
        )

    def test_automatic_relationship_cannot_approve_restricted_fields(self):
        self.db.query(VesselRoleClaim).delete()
        self.db.commit()
        entry = self.add()
        self.booking()
        self.db.commit()
        requested = self.request(
            "POST",
            "/api/v1/shipping-company/disclosure-requests",
            self.admin,
            {
                "source_kind": "CALL",
                "source_id": self.call.id,
                "fields": ["rides.pickup"],
                "reason": "Need the pickup records for this call",
                "event_start": fixture.JAN.isoformat(),
                "event_end": (fixture.NOW + timedelta(days=1)).isoformat(),
            },
            201,
        ).json()
        review = {
            "action": "APPROVE",
            "fields": ["rides.pickup"],
            "reason": "Reviewed field request for this call",
        }
        path = f"/api/v1/superadmin/disclosure-requests/{requested['id']}/preview"
        denied = self.request("POST", path, self.sa, review, 422)
        self.assertIn("Independently review", denied.text)
        self.request(
            "POST", f"/api/v1/superadmin/company-claims/{entry['id']}/review", self.sa
        )
        self.request(
            "POST",
            f"/api/v1/superadmin/company-claims/{entry['id']}/approve",
            self.sa,
            {
                "evidence_type": "registry",
                "evidence_reference": "Independent registry proof",
                "reason": "Checked company authority",
            },
        )
        self.request("POST", path, self.sa, review)
        # Independent relationship review alone does not release a requested field.
        self.request(
            "GET",
            f"/api/v1/shipping-company/disclosure-requests/{requested['id']}/data",
            self.admin,
            status=403,
        )

    def test_xlsx_mapping_and_dates_and_repeated_nonoverlapping_periods(self):
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.append(list(reversed(LABELS)))
        for start, end in (("2025-01-01", "2025-07-01"), ("2025-07-01", "2026-01-01")):
            sheet.append(
                list(
                    reversed(
                        [number(0), self.vessel.name, "Yes", "No", "No", start, end]
                    )
                )
            )
        out = io.BytesIO()
        book.save(out)
        book.close()
        r = self.client.post(
            BASE + "/imports",
            headers=self.headers(self.admin),
            files={
                "file": (
                    "fleet.xlsx",
                    out.getvalue(),
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(self.preview(r.json())["counts"]["accepted"], 2)
        self.assertEqual(self.confirm(r.json())["counts"]["accepted"], 2)
        self.db.expire_all()
        rows = (
            self.db.query(VesselRoleClaim)
            .filter_by(status="AUTO_ACCEPTED")
            .order_by(VesselRoleClaim.effective_start)
            .all()
        )
        self.assertEqual(rows[0].effective_end, rows[1].effective_start)
        self.assertEqual(rows[0].effective_start.hour, 18)

    def test_conflicts_are_rechecked_after_preview_and_other_roles_can_coexist(self):
        draft = self.upload(
            [[number(0), self.vessel.name, "Yes", "No", "No", "2026-01-01", ""]]
        )
        self.assertEqual(self.preview(draft)["counts"]["accepted"], 1)
        other = ShippingCompany(
            legal_name="New competitor",
            normalized_name="NEW COMPETITOR",
            status="VERIFIED",
        )
        self.db.add(other)
        self.db.flush()
        self.db.add(
            VesselRoleClaim(
                company_id=other.id,
                vessel_id=self.vessel.id,
                role="REGISTERED_OWNER",
                status="AUTO_ACCEPTED",
                effective_start=fixture.JAN,
            )
        )
        self.db.commit()
        self.assertEqual(self.confirm(draft)["counts"]["conflict"], 1)
        body = self.declaration()
        body.update(owner=False, commercial_manager=True)
        self.assertEqual(self.add(body)["statuses"], ["AUTO_ACCEPTED"])

    def test_end_date_shortens_access_and_invalidates_a_broader_saved_report(self):
        self.db.query(VesselRoleClaim).delete()
        self.db.commit()
        entry = self.add()
        self.booking()
        self.db.commit()
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
        self.request(
            "POST",
            BASE + f"/entries/{entry['id']}/change",
            self.admin,
            {
                "action": "END",
                "revision": entry["revision"],
                "effective_date": "2026-07-01",
                "reason": "The vessel relationship ended at handover",
            },
        )
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{saved['id']}",
            self.admin,
            status=403,
        )
        self.assertEqual(
            self.request("GET", BASE + "/entries", self.admin).json()[
                "current_vessels"
            ],
            0,
        )

    def test_imo_correction_keeps_original_operations_and_requires_known_target(self):
        entry = self.add()
        target = self.vessel_row(1)
        self.db.commit()
        request = self.request(
            "POST",
            BASE + f"/entries/{entry['id']}/change",
            self.admin,
            {
                "action": "IMO",
                "revision": entry["revision"],
                "corrected_imo": target.imo_number,
                "reason": "The first declaration used the wrong IMO",
            },
        ).json()
        self.request(
            "POST",
            f"/api/v1/superadmin/fleet-changes/{request['change_id']}/decision",
            self.sa,
            {
                "approve": True,
                "reason": "Independently checked the correct vessel identity",
                "evidence_reference": "Registry evidence synthetic 456",
            },
        )
        self.db.expire_all()
        self.assertEqual(self.call.vessel_id, self.vessel.id)
        self.assertEqual(self.db.get(VesselRoleClaim, entry["id"]).status, "CORRECTED")
        self.assertEqual(
            self.db.query(VesselRoleClaim)
            .filter_by(vessel_id=target.id, status="AUTO_ACCEPTED")
            .count(),
            1,
        )

    def test_guard_and_frozen_migration_are_repeatable_and_keep_legacy_rows(self):
        import importlib.util
        from unittest.mock import patch
        from app.db.fleet_schema import ensure_fleet_schema
        from app.db.session import engine
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "fleet_revision", "alembic/versions/k7f8a9b0c1d2_fleet_declarations.py"
        )
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        old = self.db.query(VesselRoleClaim).one()
        original = (old.id, old.status, old.effective_start)
        self.db.commit()
        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            with patch.object(revision, "op", operations):
                revision.upgrade()
                revision.upgrade()
            ensure_fleet_schema(connection)
        self.db.expire_all()
        old = self.db.get(VesselRoleClaim, original[0])
        self.assertEqual((old.id, old.status, old.effective_start), original)

    def test_database_guard_protects_automatic_against_independent_approval(self):
        from sqlalchemy.exc import IntegrityError

        entry = self.add()
        other = ShippingCompany(
            legal_name="Conflicting approved",
            normalized_name="CONFLICTING APPROVED",
            status="VERIFIED",
        )
        self.db.add(other)
        self.db.commit()
        self.db.add(
            VesselRoleClaim(
                company_id=other.id,
                vessel_id=self.vessel.id,
                role="REGISTERED_OWNER",
                effective_start=fixture.JAN,
                status="APPROVED",
            )
        )
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()
        self.assertEqual(
            self.db.get(VesselRoleClaim, entry["id"]).status, "AUTO_ACCEPTED"
        )

    def test_sa_can_amend_conflicting_dates_without_certifying_the_declaration(self):
        other = ShippingCompany(
            legal_name="Historic owner",
            normalized_name="HISTORIC OWNER",
            status="VERIFIED",
        )
        self.db.add(other)
        self.db.flush()
        self.db.add(
            VesselRoleClaim(
                company_id=other.id,
                vessel_id=self.vessel.id,
                role="REGISTERED_OWNER",
                effective_start=fixture.JAN,
                effective_end=fixture.JUL,
                status="AUTO_ACCEPTED",
            )
        )
        self.db.commit()
        entry = self.add()
        self.assertEqual(entry["statuses"], ["SUBMITTED"])
        amended = self.request(
            "POST",
            f"/api/v1/superadmin/company-claims/{entry['id']}/amend",
            self.sa,
            {
                "effective_start": fixture.JUL.isoformat(),
                "reason": "Checked the exact legal handover date",
                "evidence_reference": "Handover document synthetic 789",
            },
        ).json()
        self.assertEqual(amended["status"], "AUTO_ACCEPTED")
        self.assertEqual(amended["conflict_count"], 0)
        self.request(
            "POST",
            f"/api/v1/superadmin/company-claims/{amended['id']}/correct",
            self.sa,
            {"reason": "A date correction cannot certify this declaration"},
            400,
        )
