"""Independent IMO declarations, later operations and immutable access scope."""

import importlib.util
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from alembic.migration import MigrationContext
from alembic.operations import Operations

import test_company_fleet_acceptance_http as fleet_fixture
import test_company_operations_http as fixture
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.cab_booking import CabBooking
from app.db.models.shipping_company import VesselRoleClaim, ShippingCompany, CompanyUserMembership
from app.db.session import engine
from app.services.company_access import covering_claim
from app.services.company_aggregates import tenure_scoped
from app.services.company_fleet_identity import canonical_imo

BASE = fleet_fixture.BASE
number = fleet_fixture.number


class IndependentFleetHTTP(unittest.TestCase):
    setUp = fleet_fixture.FleetAcceptanceHTTP.setUp
    tearDown = fleet_fixture.FleetAcceptanceHTTP.tearDown
    headers = fleet_fixture.FleetAcceptanceHTTP.headers
    request = fleet_fixture.FleetAcceptanceHTTP.request
    booking = fleet_fixture.FleetAcceptanceHTTP.booking
    period = fleet_fixture.FleetAcceptanceHTTP.period
    declaration = fleet_fixture.FleetAcceptanceHTTP.declaration
    add = fleet_fixture.FleetAcceptanceHTTP.add
    upload = fleet_fixture.FleetAcceptanceHTTP.upload
    preview = fleet_fixture.FleetAcceptanceHTTP.preview
    confirm = fleet_fixture.FleetAcceptanceHTTP.confirm

    def unknown(self, i=888, **extra):
        body = self.declaration()
        body.update(imo_number=number(i), vessel_name=f"Independent MV {i}", **extra)
        return body

    def another_company(self):
        other = ShippingCompany(legal_name="Separate Fleet", normalized_name="SEPARATE FLEET", status="VERIFIED")
        self.db.add(other)
        self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin2.id).update({"company_id": other.id})
        self.db.commit()
        return other

    def operational_vessel(self, body):
        vessel = Vessel(name=body["vessel_name"], imo_number=body["imo_number"], vessel_type="Bulk Carrier", agent_id=self.agent.id)
        self.db.add(vessel)
        self.db.flush()
        return vessel

    def test_twenty_unknown_imos_import_without_creating_operational_records(self):
        before_vessels = self.db.query(Vessel).count()
        before_calls = self.db.query(VesselCall).count()
        draft = self.upload([[number(800+i), f"Independent MV {i}", "Yes", "No", "No", "2026-01-01", ""] for i in range(20)])
        self.assertTrue(draft["mapping_ready"])
        self.assertEqual(self.preview(draft)["counts"]["accepted"], 20)
        self.assertEqual(self.confirm(draft)["counts"]["accepted"], 20)
        self.db.expire_all()
        self.assertEqual(self.db.query(Vessel).count(), before_vessels)
        self.assertEqual(self.db.query(VesselCall).count(), before_calls)
        self.assertEqual(self.db.query(VesselRoleClaim).filter_by(vessel_id=None, status="AUTO_ACCEPTED").count(), 20)
        page = self.request("GET", BASE + "/entries?view=all&search=Independent", self.admin).json()
        self.assertEqual(page["total"], 20)
        self.assertEqual(page["current_vessels"], 21)
        self.assertTrue(all(not row["operational_linked"] for row in page["items"]))

    def test_later_call_matches_imo_and_period_without_reassigning_any_record(self):
        self.another_company()
        body = self.unknown(end_date="2026-07-01")
        entry = self.add(body)
        self.request("GET", "/api/v1/shipping-company/intelligence/calls?" + self.period(), self.admin)
        vessel = self.operational_vessel(body)
        call = VesselCall(vessel_id=vessel.id, agency_id=self.agency.id, port_id=self.port.id,
                          vessel_name=vessel.name, imo_number=vessel.imo_number,
                          started_at=fixture.JAN, ended_at=fixture.JUL)
        self.db.add(call)
        self.db.flush()
        booking = self.booking(vessel_id=vessel.id, vessel_call_id=call.id, created_at=fixture.JAN + timedelta(days=1))
        self.db.commit()
        ids = (vessel.id, call.id, booking.id)
        detail = self.request("GET", f"/api/v1/shipping-company/intelligence/calls/{call.id}", self.admin).json()
        self.assertEqual(detail["metrics"]["total_booked_rides"], 1)
        self.request("GET", f"/api/v1/shipping-company/intelligence/calls/{call.id}", self.admin2, status=404)
        aggregate = tenure_scoped(self.db, self.db.query(CabBooking), company_id=self.company.id,
            vessel_column=CabBooking.vessel_id, occurred_column=CabBooking.created_at, include_declared=True)
        self.assertEqual(aggregate.filter(CabBooking.id == booking.id).count(), 1)
        self.assertIsNone(covering_claim(self.db, company_id=self.company.id, vessel_id=vessel.id, occurred_at=booking.created_at))
        self.assertIsNotNone(covering_claim(self.db, company_id=self.company.id, vessel_id=vessel.id, occurred_at=booking.created_at, include_declared=True))
        self.assertIsNone(covering_claim(self.db, company_id=self.company.id, vessel_id=vessel.id, occurred_at=fixture.JUL, include_declared=True))
        self.db.expire_all()
        self.assertIsNone(self.db.get(VesselRoleClaim, entry["id"]).vessel_id)
        self.assertEqual((vessel.id, call.id, booking.id), ids)
        saved = self.request("POST", "/api/v1/shipping-company/report-centre", self.admin,
            {"report_type":"FLEET", "start":fixture.JAN.isoformat(), "end":fixture.JUL.isoformat()}, 201).json()
        self.request("GET", f"/api/v1/shipping-company/report-centre/{saved['id']}", self.admin)
        self.request("POST", BASE + f"/entries/{entry['id']}/change", self.admin,
                     {"action":"VOID", "revision":entry["revision"], "reason":"Synthetic declaration has ended"})
        self.request("GET", f"/api/v1/shipping-company/report-centre/{saved['id']}", self.admin, status=404)

    def test_independent_role_dates_are_separate_and_unselected_dates_are_not_required(self):
        body = self.unknown()
        body.pop("start_date")
        body.update(commercial_manager=True, owner_start_date="2026-01-01", owner_end_date="2026-07-01",
                    commercial_manager_start_date="2026-04-01", commercial_manager_end_date=None)
        entry = self.add(body)
        rows = self.db.query(VesselRoleClaim).filter_by(imo_number=body["imo_number"]).order_by(VesselRoleClaim.id).all()
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].effective_start, rows[1].effective_start)
        self.assertIsNone(rows[1].effective_end)
        self.assertEqual(len(entry["role_periods"]), 2)
        bad = self.unknown(i=889, owner_start_date="2026-07-01", owner_end_date="2026-01-01")
        self.add(bad, status=422)

    def test_concurrent_unknown_imo_has_one_accepted_and_one_review(self):
        self.another_company()
        headers = [self.headers(actor) for actor in (self.admin, self.admin2)]
        body = self.unknown()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda auth: self.client.post(BASE+"/entries", headers=auth, json=body), headers))
        self.assertEqual([r.status_code for r in results], [201, 201])
        self.assertEqual(sorted(r.json()["statuses"][0] for r in results), ["AUTO_ACCEPTED", "SUBMITTED"])

    def test_different_roles_coexist_but_same_role_and_own_overlap_do_not(self):
        self.another_company()
        body = self.unknown()
        self.assertEqual(self.add(body)["statuses"], ["AUTO_ACCEPTED"])
        self.add(body, status=422)
        other = dict(body, owner=False, commercial_manager=True)
        self.assertEqual(self.add(other, actor=self.admin2)["statuses"], ["AUTO_ACCEPTED"])
        self.assertEqual(self.add(body, actor=self.admin2)["statuses"], ["SUBMITTED"])

    def test_direct_database_write_cannot_bypass_unknown_identity_guard(self):
        other = self.another_company()
        body = self.unknown()
        self.add(body)
        self.db.add(VesselRoleClaim(company_id=other.id, vessel_id=None, imo_number="IMO " + body["imo_number"],
                    role="REGISTERED_OWNER", effective_start=fixture.JAN, status="APPROVED"))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_missing_operational_identity_never_matches_every_unlinked_declaration(self):
        self.add(self.unknown())
        self.assertIsNone(covering_claim(self.db, company_id=self.company.id, vessel_id=None,
                                        occurred_at=fixture.JAN, include_declared=True))

    def test_new_template_maps_role_dates_and_keeps_blank_unselected_role_dates(self):
        from app.api.v1.routes_company_fleet_workspace import TEMPLATE_LABELS
        import csv, io

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(TEMPLATE_LABELS)
        writer.writerow([number(888), "New template MV", "Yes", "No", "Yes",
                         "2026-01-01", "2026-07-01", "", "", "2026-04-01", ""])
        draft = self.client.post(BASE+"/imports", headers=self.headers(self.admin),
                   files={"file":("new-template.csv", output.getvalue().encode(), "text/csv")})
        self.assertEqual(draft.status_code, 201, draft.text)
        self.assertTrue(draft.json()["mapping_ready"])
        self.assertEqual(self.preview(draft.json())["counts"]["accepted"], 1)
        self.assertEqual(self.confirm(draft.json())["counts"]["accepted"], 1)
        self.db.expire_all()
        claims = self.db.query(VesselRoleClaim).filter_by(imo_number=number(888)).order_by(VesselRoleClaim.id).all()
        self.assertEqual(len(claims), 2)
        self.assertIsNotNone(claims[0].effective_end)
        self.assertNotEqual(claims[0].effective_start, claims[1].effective_start)
        self.assertIsNone(claims[1].effective_end)

    def test_unknown_declaration_conflicts_with_legacy_claim_after_operations_arrive(self):
        body = self.unknown()
        self.add(body)
        vessel = self.operational_vessel(body)
        other = self.another_company()
        self.db.add(VesselRoleClaim(company_id=other.id, vessel_id=vessel.id, imo_number=None,
                    role="REGISTERED_OWNER", effective_start=fixture.JAN, status="APPROVED"))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_ambiguous_operational_identity_has_no_automatic_data_association(self):
        body = self.unknown()
        entry = self.add(body)
        vessel = self.operational_vessel(body)
        self.db.add(Vessel(name="Ambiguous raw-prefix variant", imo_number="IMO " + body["imo_number"], vessel_type="Bulk Carrier"))
        self.db.commit()
        self.assertIsNone(covering_claim(self.db, company_id=self.company.id, vessel_id=vessel.id,
                         occurred_at=fixture.JAN, include_declared=True))
        self.assertEqual(self.db.query(Vessel).filter(canonical_imo(Vessel.imo_number) == body["imo_number"]).count(), 2)
        self.assertIsNone(self.db.get(VesselRoleClaim, entry["id"]).vessel_id)

    def test_guard_upgrade_is_repeatable_and_does_not_backfill_legacy_claims(self):
        self.db.execute(text("ALTER TABLE vessel_role_claims DISABLE TRIGGER company_fleet_identity_guard"))
        self.db.execute(text("UPDATE vessel_role_claims SET fleet_identity_key=NULL"))
        self.db.execute(text("ALTER TABLE vessel_role_claims ENABLE TRIGGER company_fleet_identity_guard"))
        self.db.commit()
        before = self.db.execute(text("SELECT id,vessel_id,imo_number,status,effective_start,effective_end,fleet_identity_key FROM vessel_role_claims ORDER BY id")).all()
        calls = self.db.execute(text("SELECT id,vessel_id,vessel_name,imo_number FROM vessel_calls ORDER BY id")).all()
        self.db.commit()
        spec = importlib.util.spec_from_file_location("independent_fleet_revision", "alembic/versions/l8a9b0c1d2e3_independent_fleet_registry.py")
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        with engine.begin() as connection:
            with patch.object(revision, "op", Operations(MigrationContext.configure(connection))):
                revision.upgrade()
                revision.upgrade()
        self.assertEqual(self.db.execute(text("SELECT id,vessel_id,imo_number,status,effective_start,effective_end,fleet_identity_key FROM vessel_role_claims ORDER BY id")).all(), before)
        self.assertEqual(self.db.execute(text("SELECT id,vessel_id,vessel_name,imo_number FROM vessel_calls ORDER BY id")).all(), calls)
