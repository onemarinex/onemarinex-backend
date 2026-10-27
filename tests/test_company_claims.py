"""The claim lifecycle, and the shortcuts Document 2 section 21 prohibits.

Two of these are security properties rather than workflow rules:

* a company whose claim collides must not learn who it collided with, because
  that is a competitor's commercial position;
* a correction must not erase who previously held the role, because that is
  what a later dispute turns on.

Committed transactions on separate rows where the exclusion constraint is
involved, since an ORM-level check would not exercise it.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy import text

from app.db.models.shipping_company import (
    CompanyAuditEvent,
    ShippingCompany,
    VesselRoleClaim,
)
from app.db.models.vessel import Vessel
from app.db.session import SessionLocal
from app.services import company_claims as claims
from app.services.company_claims import ClaimConflict, ClaimError

JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
JUL = datetime(2026, 7, 1, tzinfo=timezone.utc)
AUG = datetime(2026, 8, 1, tzinfo=timezone.utc)

SUPERADMIN = SimpleNamespace(id=None, role="superadmin")
COMPANY_ADMIN = SimpleNamespace(id=None, role="company_admin")


def _uniq(p):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class ClaimLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.vessel = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        name_a, name_b = _uniq("A"), _uniq("B")
        self.company = ShippingCompany(
            legal_name=name_a, normalized_name=name_a.upper(), status="VERIFIED")
        self.other = ShippingCompany(
            legal_name=name_b, normalized_name=name_b.upper(), status="VERIFIED")
        self.db.add_all([self.company, self.other])
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        for table, column, value in (
            ("company_audit_events", "company_id", (self.company.id, self.other.id)),
            ("vessel_role_claims", "vessel_id", (self.vessel.id,)),
        ):
            self.db.execute(text(
                f"DELETE FROM {table} WHERE {column} = ANY(:v)"), {"v": list(value)})
        self.db.execute(text("DELETE FROM shipping_companies WHERE id = ANY(:v)"),
                        {"v": [self.company.id, self.other.id]})
        self.db.execute(text("DELETE FROM vessels WHERE id = :v"), {"v": self.vessel.id})
        self.db.commit()
        self.db.close()

    def _submit(self, company=None, start=JAN, end=JUL, role="ISM"):
        claim = claims.submit_claim(
            self.db, company_id=(company or self.company).id,
            vessel_id=self.vessel.id, role=role,
            effective_start=start, effective_end=end,
            submitted_by=COMPANY_ADMIN,
        )
        self.db.commit()
        return claim

    def _approve(self, claim, actor=SUPERADMIN):
        claims.begin_review(self.db, claim=claim, actor=actor)
        result = claims.approve_claim(
            self.db, claim=claim, actor=actor,
            evidence_type="DOC", evidence_reference="DOC-2026-11",
        )
        self.db.commit()
        return result

    # --- the happy path -----------------------------------------------------

    def test_a_submitted_claim_is_not_approved_by_submitting_it(self):
        claim = self._submit()
        self.assertEqual(claim.status, "SUBMITTED")

    def test_review_then_approval_reaches_approved(self):
        claim = self._submit()
        self.assertEqual(self._approve(claim).status, "APPROVED")

    def test_the_role_is_canonicalised_on_submission(self):
        """So a claim stored as "ISM" and a query for ISM_MANAGER still meet."""
        self.assertEqual(self._submit(role="ism").role, "ISM_MANAGER")

    # --- the prohibited shortcuts -------------------------------------------

    def test_a_claim_cannot_jump_straight_to_approved(self):
        claim = self._submit()
        with self.assertRaises(ClaimError) as caught:
            claims.approve_claim(
                self.db, claim=claim, actor=SUPERADMIN,
                evidence_type="DOC", evidence_reference="X")
        self.assertIn("cannot go from SUBMITTED to APPROVED", str(caught.exception))

    def test_only_a_superadmin_decides(self):
        claim = self._submit()
        for action in (claims.begin_review,):
            with self.assertRaises(ClaimError):
                action(self.db, claim=claim, actor=COMPANY_ADMIN)

    def test_an_approval_must_record_its_evidence(self):
        claim = self._submit()
        claims.begin_review(self.db, claim=claim, actor=SUPERADMIN)
        for kwargs in ({"evidence_type": "", "evidence_reference": "X"},
                       {"evidence_type": "DOC", "evidence_reference": "  "}):
            with self.subTest(**kwargs):
                with self.assertRaises(ClaimError):
                    claims.approve_claim(self.db, claim=claim, actor=SUPERADMIN, **kwargs)

    def test_an_unverified_company_cannot_claim(self):
        self.company.status = "PENDING_VERIFICATION"
        self.db.commit()
        with self.assertRaises(ClaimError):
            self._submit()

    def test_a_backwards_interval_is_refused(self):
        with self.assertRaises(ClaimError):
            self._submit(start=JUL, end=JAN)

    def test_a_terminal_claim_goes_nowhere(self):
        claim = self._submit()
        claims.begin_review(self.db, claim=claim, actor=SUPERADMIN)
        claims.reject_claim(self.db, claim=claim, actor=SUPERADMIN, reason="no evidence")
        self.db.commit()
        with self.assertRaises(ClaimError) as caught:
            claims.begin_review(self.db, claim=claim, actor=SUPERADMIN)
        self.assertIn("it is final", str(caught.exception))

    # --- the conflict, and what it must not say ------------------------------

    def test_an_overlapping_approval_is_refused(self):
        self._approve(self._submit(start=JAN, end=AUG))
        competing = self._submit(company=self.other, start=JUL, end=None)
        claims.begin_review(self.db, claim=competing, actor=SUPERADMIN)
        with self.assertRaises(ClaimConflict):
            claims.approve_claim(
                self.db, claim=competing, actor=SUPERADMIN,
                evidence_type="DOC", evidence_reference="Y")
        self.db.rollback()

    def test_the_conflict_names_no_competitor(self):
        """Section 21: never competitor identity, evidence or effective dates."""
        self._approve(self._submit(start=JAN, end=AUG))
        competing = self._submit(company=self.other, start=JUL, end=None)
        claims.begin_review(self.db, claim=competing, actor=SUPERADMIN)
        try:
            claims.approve_claim(
                self.db, claim=competing, actor=SUPERADMIN,
                evidence_type="DOC", evidence_reference="Y")
            self.fail("the overlap was accepted")
        except ClaimConflict as conflict:
            message = str(conflict)
        finally:
            self.db.rollback()

        for leak in (self.company.legal_name, "DOC-2026-11",
                     "2026-01", "2026-08", str(self.company.id)):
            self.assertNotIn(leak, message, f"the message leaked {leak!r}")

    def test_a_clean_handover_is_approved(self):
        self._approve(self._submit(start=JAN, end=JUL))
        self.assertEqual(
            self._approve(self._submit(company=self.other, start=JUL, end=None)).status,
            "APPROVED",
        )

    def test_a_different_role_does_not_conflict(self):
        self._approve(self._submit(start=JAN, end=None, role="ISM"))
        self.assertEqual(
            self._approve(
                self._submit(company=self.other, start=JAN, end=None, role="Owner")
            ).status,
            "APPROVED",
        )

    # --- history ------------------------------------------------------------

    def test_a_correction_keeps_the_claim_it_replaces(self):
        original = self._approve(self._submit(start=JAN, end=JUL))
        original_id = original.id
        replacement = claims.correct_claim(
            self.db, claim=original, actor=SUPERADMIN,
            reason="handover was a month later", effective_end=AUG)
        self.db.commit()

        kept = self.db.query(VesselRoleClaim).filter(
            VesselRoleClaim.id == original_id).one()
        self.assertEqual(kept.status, "CORRECTED")
        self.assertEqual(kept.effective_end, JUL)
        self.assertEqual(replacement.status, "APPROVED")
        self.assertEqual(replacement.effective_end, AUG)
        self.assertNotEqual(replacement.id, original_id)

    def test_every_decision_leaves_an_audit_row(self):
        claim = self._submit()
        self._approve(claim)
        claims.revoke_claim(self.db, claim=claim, actor=SUPERADMIN, reason="withdrawn")
        self.db.commit()

        actions = [
            row.action for row in self.db.query(CompanyAuditEvent)
            .filter(CompanyAuditEvent.target_id == claim.id)
            .order_by(CompanyAuditEvent.id).all()
        ]
        self.assertEqual(actions, ["submitted", "review_started", "approved", "revoked"])

    def test_the_audit_records_the_interval_on_both_sides(self):
        claim = self._submit()
        self._approve(claim)
        self.db.commit()
        row = (self.db.query(CompanyAuditEvent)
               .filter(CompanyAuditEvent.target_id == claim.id,
                       CompanyAuditEvent.action == "approved").one())
        self.assertEqual(row.before_state["status"], "UNDER_REVIEW")
        self.assertEqual(row.after_state["status"], "APPROVED")
        self.assertEqual(row.evidence_reference, "DOC-2026-11")

    def test_a_revoked_claim_stops_blocking_its_replacement(self):
        first = self._approve(self._submit(start=JAN, end=None))
        claims.revoke_claim(self.db, claim=first, actor=SUPERADMIN, reason="wrong company")
        self.db.commit()
        self.assertEqual(
            self._approve(self._submit(company=self.other, start=JAN, end=None)).status,
            "APPROVED",
        )


if __name__ == "__main__":
    unittest.main()
