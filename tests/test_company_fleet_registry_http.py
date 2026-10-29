"""Modern registry acceptance with the guarded disposable PostgreSQL fixture."""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.api.v1 import routes_company_fleet_registry as routes
from app.db.models.company_fleet_change import CompanyFleetChange
from app.db.models.shipping_company import CompanyAuditEvent, CompanyUserMembership, ShippingCompany, VesselRoleClaim
from app.db.models.vessel import Vessel
from app.services.company_access import covering_claim
from app.services.company_fleet_identity import canonical_imo
import test_company_fleet_acceptance_http as fleet_fixture
import test_company_operations_http as fixture

BASE = fleet_fixture.BASE


class RegistryHTTP(unittest.TestCase):
    setUpBase = fleet_fixture.FleetAcceptanceHTTP.setUp
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request
    add = fleet_fixture.FleetAcceptanceHTTP.add

    def declaration(self, **changes):
        body = fleet_fixture.FleetAcceptanceHTTP.declaration(self)
        body.update(changes)
        return body

    def setUp(self):
        self.setUpBase()
        self.client.app.include_router(routes.router, prefix=BASE)
        self.client.app.include_router(routes.admin_router, prefix="/api/v1/superadmin")

    def entry(self, entry_id=None):
        items = self.request("GET", BASE + "/registry-entries?view=all", self.admin).json()["items"]
        return next((r for r in items if r["id"] == entry_id), items[0])

    def update(self, entry, **changes):
        body = {"revision": entry["revision"], "action": "UPDATE", "change_kind": "CORRECTION"}
        for field in ("owner", "commercial_manager", "ism_manager"):
            body[field] = entry[field]
            body[field + "_start_date"] = entry[field + "_start_date"]
            body[field + "_end_date"] = entry[field + "_end_date"]
        body.update(changes)
        return body

    def edit(self, entry, body, status=200, actor=None):
        return self.request("PATCH", BASE + f"/registry-entries/{entry['id']}", actor or self.admin, body, status)

    def other_company(self):
        company = ShippingCompany(legal_name="Private competitor", normalized_name="PRIVATE COMPETITOR", status="VERIFIED")
        self.db.add(company)
        self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin2.id).update({"company_id": company.id})
        self.db.commit()
        return company

    def test_registry_combines_legacy_and_independent_role_periods_without_fake_vessels(self):
        self.add(self.declaration(commercial_manager=True, owner_start_date="2026-02-01", commercial_manager_start_date="2026-03-01"))
        data = self.request("GET", BASE + "/registry-entries?view=all", self.admin).json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["current_vessels"], 1)
        entry = data["items"][0]
        self.assertEqual({r["role"] for r in entry["role_periods"]}, {"REGISTERED_OWNER", "COMMERCIAL_MANAGER", "ISM_MANAGER"})
        self.assertNotEqual(entry["owner_start_date"], entry["commercial_manager_start_date"])
        self.assertEqual(self.db.query(Vessel).count(), 1)
        self.assertEqual(self.request("GET", BASE + "/registry-entries?search=Fixture&limit=1", self.admin).json()["total"], 1)

    def test_same_day_correction_applies_without_review_and_retains_complete_audit(self):
        added = self.add()
        entry = self.entry()
        body = self.update(entry, owner_end_date="2026-12-31")
        result = self.edit(entry, body).json()
        self.assertEqual(result["status"], "APPLIED")
        self.db.expire_all()
        self.assertEqual(self.db.get(VesselRoleClaim, added["id"]).status, "CORRECTED")
        self.assertEqual(self.db.query(CompanyFleetChange).count(), 0)
        event = self.db.query(CompanyAuditEvent).filter_by(action="registry_roles_updated").one()
        self.assertTrue(event.before_state["claims"])
        self.assertTrue(event.after_state["claims"])
        self.edit(entry, body, 409)

    def test_later_change_closes_old_roles_and_preserves_prior_periods(self):
        self.add()
        entry = self.entry()
        result = self.edit(entry, self.update(entry, change_kind="CHANGE", effective_date="2026-07-01",
                                             owner=False, commercial_manager=True, commercial_manager_start_date="2026-07-01",
                                             ism_manager_start_date="2026-07-01")).json()["entry"]
        self.assertFalse(result["owner"] and not result["owner_end_date"])
        self.assertTrue(result["commercial_manager"])
        self.db.expire_all()
        earlier = covering_claim(self.db, company_id=self.company.id, vessel_id=self.vessel.id,
                                 occurred_at=datetime(2026, 5, 1, tzinfo=timezone.utc), role="REGISTERED_OWNER", include_declared=True)
        later = covering_claim(self.db, company_id=self.company.id, vessel_id=self.vessel.id,
                               occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc), role="REGISTERED_OWNER", include_declared=True)
        self.assertIsNotNone(earlier)
        self.assertIsNone(later)

    def test_company_correction_preserves_only_previously_approved_intersection(self):
        claim = self.db.query(VesselRoleClaim).one()
        claim.effective_end = fixture.JUL
        self.db.commit()
        entry = self.entry()
        result = self.edit(entry, self.update(entry, ism_manager_start_date="2025-01-01", ism_manager_end_date="2026-12-31")).json()
        self.assertEqual(result["status"], "APPLIED")
        self.db.expire_all()
        for date, approved in ((datetime(2025, 6, 1, tzinfo=timezone.utc), False),
                               (datetime(2026, 5, 1, tzinfo=timezone.utc), True),
                               (datetime(2026, 8, 1, tzinfo=timezone.utc), False)):
            query = dict(company_id=self.company.id, vessel_id=self.vessel.id, occurred_at=date, role="ISM_MANAGER")
            self.assertEqual(covering_claim(self.db, **query) is not None, approved)
            self.assertIsNotNone(covering_claim(self.db, **query, include_declared=True))
        self.assertEqual(self.db.query(VesselRoleClaim).filter_by(status="APPROVED").count(), 1)

    def test_role_removal_needs_boundary_and_same_start_withdrawal_is_valid(self):
        self.add()
        entry = self.entry()
        self.edit(entry, self.update(entry, owner=False), 422)
        result = self.edit(entry, self.update(entry, owner=False, effective_date="2026-01-01")).json()["entry"]
        self.assertTrue(result["ism_manager"])
        self.assertFalse(result["owner"])

    def test_remove_closes_access_and_keeps_historical_registry(self):
        self.add()
        entry = self.entry()
        self.edit(entry, {"revision": entry["revision"], "action": "REMOVE", "effective_date": "2026-07-01"})
        self.assertEqual(self.request("GET", BASE + "/registry-entries", self.admin).json()["current_vessels"], 0)
        self.assertEqual(self.request("GET", BASE + "/registry-entries?view=historical", self.admin).json()["total"], 1)
        self.db.expire_all()
        self.assertIsNone(covering_claim(self.db, company_id=self.company.id, vessel_id=self.vessel.id,
                                        occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc), include_declared=True))

    def test_competing_period_change_keeps_original_access_and_enters_real_conflict_queue(self):
        self.add(self.declaration(end_date="2026-07-01"))
        other = self.other_company()
        self.db.add(VesselRoleClaim(company_id=other.id, vessel_id=self.vessel.id, imo_number=self.vessel.imo_number,
                                   role="REGISTERED_OWNER", status="AUTO_ACCEPTED", effective_start=fixture.JUL))
        self.db.commit()
        entry = self.entry()
        result = self.edit(entry, self.update(entry, owner_end_date=None, reason="The requested handover date needs review")).json()
        self.assertEqual(result["status"], "REQUESTED")
        self.assertEqual(result["entry"]["revision"], entry["revision"])
        self.db.expire_all()
        original = self.db.query(VesselRoleClaim).filter_by(company_id=self.company.id, role="REGISTERED_OWNER", status="AUTO_ACCEPTED").one()
        self.assertIsNotNone(original.effective_end)
        queue = self.request("GET", "/api/v1/superadmin/fleet-conflicts", self.sa).json()
        self.assertTrue(any(r["kind"] == "CHANGE" and r["id"] == result["change_id"] for r in queue["items"]))
        item = next(r for r in queue["items"] if r["kind"] == "CHANGE" and r["id"] == result["change_id"])
        self.assertEqual(item["competing_claims"][0]["company_name"], other.legal_name)
        self.request("GET", "/api/v1/superadmin/fleet-conflicts", self.admin, status=403)

    def test_material_identity_review_does_not_move_calls_or_keep_private_approval(self):
        legacy = self.db.query(VesselRoleClaim).one()
        legacy.imo_number = "IMO " + self.vessel.imo_number
        self.db.commit()
        entry = self.entry()
        body = {"revision": entry["revision"], "action": "CORRECT", "imo_number": fleet_fixture.number(889),
                "vessel_name": "Corrected independent vessel", "reason": "The original declaration used the wrong IMO",
                "evidence_reference": "Independent registry proof fixture"}
        result = self.edit(entry, body).json()
        self.assertEqual(result["status"], "REQUESTED")
        with patch("app.services.email.send_email", return_value=True) as send:
            self.request("POST", f"/api/v1/superadmin/fleet-conflicts/CHANGE/{result['change_id']}/decision", self.sa,
                         {"decision": "ACCEPT", "reason": "Checked the vessel identity and company records",
                          "evidence_reference": "Independent registry proof fixture"})
            self.assertEqual(send.call_count, 2)
        self.db.expire_all()
        self.assertEqual(self.call.vessel_id, self.vessel.id)
        self.assertEqual(self.db.query(Vessel).count(), 1)
        new = self.db.query(VesselRoleClaim).filter_by(imo_number=fleet_fixture.number(889), status="AUTO_ACCEPTED").one()
        self.assertIsNone(new.vessel_id)
        self.assertEqual(self.db.query(VesselRoleClaim).filter_by(status="APPROVED").count(), 0)

    def _legacy_identity_approval(self, *, prefixed, remove):
        claim = self.db.query(VesselRoleClaim).one()
        if prefixed:
            claim.imo_number = "IMO " + self.vessel.imo_number
        else:
            self.vessel.imo_number = "legacy-operational-identifier"
            claim.imo_number = "legacy-submitted-identifier"
        claim.effective_end = fixture.JUL
        self.db.commit()
        original_start = claim.effective_start
        entry = self.entry()
        if remove:
            body = {"action": "REMOVE", "revision": entry["revision"], "effective_date": "2026-05-01"}
        else:
            body = self.update(entry, ism_manager_start_date="2025-01-01", ism_manager_end_date="2026-12-31")
        self.assertEqual(self.edit(entry, body).json()["status"], "APPLIED")
        self.db.expire_all()
        retained = self.db.query(VesselRoleClaim).filter_by(status="APPROVED").one()
        self.assertEqual(retained.vessel_id, self.vessel.id)
        self.assertEqual(retained.effective_start, original_start)
        self.assertEqual(retained.effective_end.isoformat(), "2026-04-30T18:30:00+00:00" if remove else fixture.JUL.isoformat())
        for date, approved in ((datetime(2025, 6, 1, tzinfo=timezone.utc), False),
                               (datetime(2026, 4, 1, tzinfo=timezone.utc), True),
                               (datetime(2026, 8, 1, tzinfo=timezone.utc), False)):
            query = dict(company_id=self.company.id, vessel_id=self.vessel.id, occurred_at=date, role="ISM_MANAGER")
            self.assertEqual(covering_claim(self.db, **query) is not None, approved)
            if not remove:
                self.assertIsNotNone(covering_claim(self.db, **query, include_declared=True))

    def test_update_retains_approved_subset_for_legacy_invalid_imo(self):
        self._legacy_identity_approval(prefixed=False, remove=False)

    def test_update_retains_approved_subset_for_legacy_prefixed_imo(self):
        self._legacy_identity_approval(prefixed=True, remove=False)

    def test_removal_retains_prior_approval_for_legacy_invalid_imo(self):
        self._legacy_identity_approval(prefixed=False, remove=True)

    def test_removal_retains_prior_approval_for_legacy_prefixed_imo(self):
        self._legacy_identity_approval(prefixed=True, remove=True)

    def test_unknown_to_unknown_identity_correction_applies_with_no_fake_operational_row(self):
        unknown = self.add(self.declaration(imo_number=fleet_fixture.number(881), vessel_name="Independent fleet vessel"))
        entry = self.entry(unknown["id"])
        result = self.edit(entry, {"revision": entry["revision"], "action": "CORRECT", "imo_number": fleet_fixture.number(882),
                                   "vessel_name": "Independent corrected vessel", "reason": "Corrected an original entry mistake"}).json()
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["entry"]["imo_number"], fleet_fixture.number(882))
        self.assertEqual(self.db.query(Vessel).count(), 1)

    def test_activity_includes_automatic_additions_edits_and_search_with_tenant_scope(self):
        self.add()
        entry = self.entry()
        self.edit(entry, self.update(entry, owner_end_date="2026-12-31"))
        activity = self.request("GET", BASE + "/activity?search=Fixture&limit=1", self.admin).json()
        self.assertGreaterEqual(activity["total"], 2)
        self.assertEqual(len(activity["items"]), 1)
        self.assertEqual(activity["items"][0]["outcome"], "Applied")
        self.assertTrue(activity["items"][0]["actor_name"])
        self.assertTrue(activity["items"][0]["created_at"])
        self.other_company()
        self.assertEqual(self.request("GET", BASE + "/activity", self.admin2).json()["total"], 0)
        self.assertGreater(self.request("GET", "/api/v1/superadmin/fleet-activity", self.sa).json()["total"], 0)
        self.edit(entry, self.update(entry), 404, self.admin2)

    def test_legacy_pending_change_is_retained_and_blocks_silent_overwrite(self):
        claim = self.db.query(VesselRoleClaim).one()
        change = CompanyFleetChange(company_id=self.company.id, claim_id=claim.id, requested_by_user_id=self.admin.id,
                                    action="GROUP", payload={"legacy": "unchanged"}, reason="Pending legacy review")
        self.db.add(change)
        self.db.commit()
        entry = self.entry()
        self.edit(entry, self.update(entry, ism_manager_end_date="2026-12-31"), 409)
        self.db.expire_all()
        self.assertEqual(self.db.get(CompanyFleetChange, change.id).payload, {"legacy": "unchanged"})
        self.assertEqual(self.db.get(CompanyFleetChange, change.id).status, "REQUESTED")

    def test_viewer_cannot_edit_or_read_superadmin_activity(self):
        entry = self.entry()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update({"role": "VIEWER"})
        self.db.commit()
        self.edit(entry, self.update(entry), 403)
        self.request("GET", "/api/v1/superadmin/fleet-activity", self.admin, status=403)

    def test_conflict_decision_cannot_override_overlap_or_independently_approve(self):
        first = self.add()
        other = self.other_company()
        pending = self.add(actor=self.admin2)
        self.assertEqual(pending["statuses"], ["SUBMITTED"])
        body = {"decision": "ACCEPT", "reason": "Checked the company relationship and handover records",
                "evidence_reference": "Registry fixture independent evidence"}
        path = f"/api/v1/superadmin/fleet-conflicts/CLAIM/{pending['id']}/decision"
        with patch("app.services.email.send_email", return_value=True) as send:
            self.request("POST", path, self.sa, body, 409)
            send.assert_not_called()
        self.db.expire_all()
        self.assertEqual(self.db.get(VesselRoleClaim, pending["id"]).status, "SUBMITTED")
        original = self.db.get(VesselRoleClaim, first["id"])
        original.status = "REVOKED"
        original.revoked_at = datetime.now(timezone.utc)
        self.db.commit()
        with patch("app.services.email.send_email", return_value=True) as send:
            result = self.request("POST", path, self.sa, body).json()
            self.assertEqual(result["notification"], {"recipients": 1, "sent": 1})
            self.assertEqual(send.call_count, 1)
        self.db.expire_all()
        accepted = self.db.get(VesselRoleClaim, pending["id"])
        self.assertEqual(accepted.company_id, other.id)
        self.assertEqual(accepted.status, "AUTO_ACCEPTED")
        self.assertIsNone(accepted.approved_at)
        self.assertIsNone(accepted.approved_by_user_id)

    def test_routine_unknown_vessel_name_correction_has_optional_reason(self):
        added = self.add(self.declaration(imo_number=fleet_fixture.number(886), vessel_name="Declared vessel"))
        entry = self.entry(added["id"])
        result = self.edit(entry, {"action": "CORRECT", "revision": entry["revision"],
                                  "imo_number": entry["imo_number"], "vessel_name": "Corrected declared vessel"}).json()
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["entry"]["vessel_name"], "Corrected declared vessel")
        self.assertEqual(self.db.query(CompanyFleetChange).count(), 0)

    def test_role_update_cannot_bypass_unresolved_name_identity_review(self):
        declared = self.add(self.declaration(vessel_name="Conflicting declared name"))
        self.assertEqual(declared["statuses"], ["SUBMITTED"])
        self.db.get(VesselRoleClaim, declared["id"]).imo_number = "IMO " + self.vessel.imo_number
        self.db.commit()
        entry = self.entry()
        result = self.edit(entry, self.update(entry, owner_end_date="2026-12-31")).json()
        self.assertTrue(result["review_pending"])
        self.assertEqual(result["entry"]["acceptance"], "Pending Conflict Resolution")
        self.db.expire_all()
        self.assertEqual(self.db.query(VesselRoleClaim).filter_by(role="REGISTERED_OWNER", status="AUTO_ACCEPTED").count(), 0)
        pending = self.db.query(VesselRoleClaim).filter_by(role="REGISTERED_OWNER", status="SUBMITTED").one()
        self.assertIn("identity", pending.review_reason.lower())
        self.assertIsNone(covering_claim(self.db, company_id=self.company.id, vessel_id=self.vessel.id,
                                        role="REGISTERED_OWNER", occurred_at=fixture.NOW, include_declared=True))

    def test_role_update_and_removal_cannot_bypass_ambiguous_operational_identity(self):
        number = fleet_fixture.number(885)
        for suffix in ("A", "B"):
            self.db.add(Vessel(name="Ambiguous " + suffix, imo_number=number if suffix == "A" else "IMO " + number, vessel_type="Bulk Carrier", status="Active", agent_id=self.agent.id))
        self.db.commit()
        declared = self.add(self.declaration(imo_number=number, vessel_name="Ambiguous A"))
        self.assertEqual(declared["statuses"], ["SUBMITTED"])
        self.db.get(VesselRoleClaim, declared["id"]).imo_number = "IMO " + number
        self.db.commit()
        entry = self.entry(declared["id"])
        result = self.edit(entry, self.update(entry, owner_end_date="2026-12-31")).json()
        self.assertTrue(result["review_pending"])
        updated = result["entry"]
        self.edit(updated, {"action": "REMOVE", "revision": updated["revision"], "effective_date": "2026-07-01"})
        self.db.expire_all()
        scoped = self.db.query(VesselRoleClaim).filter(VesselRoleClaim.company_id == self.company.id,
                                                      canonical_imo(VesselRoleClaim.imo_number) == number)
        self.assertEqual(scoped.filter(VesselRoleClaim.status == "AUTO_ACCEPTED").count(), 0)
        remaining = scoped.filter(VesselRoleClaim.status == "SUBMITTED").one()
        self.assertIsNone(remaining.vessel_id)
        self.assertIn("identity", remaining.review_reason.lower())
