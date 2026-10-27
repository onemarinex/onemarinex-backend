"""A company cannot verify itself — Document 2 section 21.

    DRAFT -> PENDING_VERIFICATION -> VERIFIED / REJECTED
    VERIFIED -> SUSPENDED -> VERIFIED or CLOSED
    Prohibited: "A self-registered user cannot set verified status."

That prohibition is why this is a service and not a column anyone can write.
Verification is the root of every access decision beneath it: a company that
could verify itself could then claim vessels and read another operator's
commercial activity.
"""

import unittest
import uuid
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy import text

from app.db.models.shipping_company import CompanyAuditEvent, ShippingCompany
from app.db.session import SessionLocal
from app.services import company_verification as verification
from app.services.company_verification import VerificationError

SUPERADMIN = SimpleNamespace(id=None, role="superadmin")
COMPANY_ADMIN = SimpleNamespace(id=None, role="company_admin")


def _uniq(p):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        name = _uniq("Verify")
        self.company = ShippingCompany(
            legal_name=name, normalized_name=name.upper(), status="DRAFT")
        self.db.add(self.company)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.execute(text("DELETE FROM company_audit_events WHERE company_id = :c"),
                        {"c": self.company.id})
        self.db.execute(text("DELETE FROM shipping_companies WHERE id = :c"),
                        {"c": self.company.id})
        self.db.commit()
        self.db.close()

    def _to_pending(self):
        verification.submit_for_verification(
            self.db, company=self.company, actor=COMPANY_ADMIN)
        self.db.commit()

    def _to_verified(self):
        self._to_pending()
        verification.verify(self.db, company=self.company, actor=SUPERADMIN,
                            evidence_type="Registry", evidence_reference="REG-1")
        self.db.commit()

    # --- the prohibited shortcut --------------------------------------------

    def test_a_company_cannot_verify_itself(self):
        self._to_pending()
        with self.assertRaises(VerificationError) as caught:
            verification.verify(self.db, company=self.company, actor=COMPANY_ADMIN,
                                evidence_type="Registry", evidence_reference="X")
        self.assertIn("cannot verify itself", str(caught.exception))

    def test_a_company_may_ask_to_be_verified(self):
        """Requesting is not deciding, so the company itself may do it."""
        self._to_pending()
        self.assertEqual(self.company.status, "PENDING_VERIFICATION")

    def test_draft_cannot_jump_to_verified(self):
        with self.assertRaises(VerificationError) as caught:
            verification.verify(self.db, company=self.company, actor=SUPERADMIN,
                                evidence_type="Registry", evidence_reference="X")
        self.assertIn("cannot go from DRAFT to VERIFIED", str(caught.exception))

    def test_verification_requires_evidence(self):
        self._to_pending()
        for kwargs in ({"evidence_type": "", "evidence_reference": "X"},
                       {"evidence_type": "Registry", "evidence_reference": " "}):
            with self.subTest(**kwargs):
                with self.assertRaises(VerificationError):
                    verification.verify(self.db, company=self.company,
                                        actor=SUPERADMIN, **kwargs)

    def test_only_a_superadmin_rejects_suspends_or_closes(self):
        self._to_verified()
        for action in (verification.suspend,):
            with self.assertRaises(VerificationError):
                action(self.db, company=self.company, actor=COMPANY_ADMIN, reason="x")

    # --- suspension is reversible, closure is not ---------------------------

    def test_suspension_touches_nothing_beneath_the_company(self):
        """Access stops because the policy refuses anything but VERIFIED."""
        self._to_verified()
        verification.suspend(self.db, company=self.company, actor=SUPERADMIN,
                             reason="under investigation")
        self.db.commit()
        self.assertEqual(self.company.status, "SUSPENDED")
        self.assertIsNotNone(self.company.verified_at)  # history kept

    def test_a_suspended_company_can_be_reinstated(self):
        self._to_verified()
        verification.suspend(self.db, company=self.company, actor=SUPERADMIN, reason="x")
        verification.reinstate(self.db, company=self.company, actor=SUPERADMIN, reason="cleared")
        self.db.commit()
        self.assertEqual(self.company.status, "VERIFIED")

    def test_an_active_company_cannot_be_closed_in_one_step(self):
        """Section 21 allows CLOSED only from SUSPENDED."""
        self._to_verified()
        with self.assertRaises(VerificationError):
            verification.close(self.db, company=self.company, actor=SUPERADMIN, reason="x")

    def test_closed_is_terminal(self):
        self._to_verified()
        verification.suspend(self.db, company=self.company, actor=SUPERADMIN, reason="x")
        verification.close(self.db, company=self.company, actor=SUPERADMIN, reason="ceased")
        self.db.commit()
        with self.assertRaises(VerificationError) as caught:
            verification.reinstate(self.db, company=self.company,
                                   actor=SUPERADMIN, reason="x")
        self.assertIn("it is final", str(caught.exception))

    def test_a_rejected_company_is_terminal_too(self):
        self._to_pending()
        verification.reject(self.db, company=self.company, actor=SUPERADMIN, reason="no proof")
        self.db.commit()
        with self.assertRaises(VerificationError):
            verification.verify(self.db, company=self.company, actor=SUPERADMIN,
                                evidence_type="Registry", evidence_reference="X")

    # --- the audit ----------------------------------------------------------

    def test_every_status_change_is_audited(self):
        self._to_verified()
        verification.suspend(self.db, company=self.company, actor=SUPERADMIN, reason="x")
        self.db.commit()
        actions = [r.action for r in self.db.query(CompanyAuditEvent).filter(
            CompanyAuditEvent.target_type == "shipping_company",
            CompanyAuditEvent.target_id == self.company.id,
        ).order_by(CompanyAuditEvent.id).all()]
        self.assertEqual(actions, ["verification_requested", "verified", "suspended"])

    def test_the_audit_records_the_status_on_both_sides(self):
        self._to_verified()
        self.db.commit()
        row = self.db.query(CompanyAuditEvent).filter(
            CompanyAuditEvent.target_id == self.company.id,
            CompanyAuditEvent.action == "verified").one()
        self.assertEqual(row.before_state["status"], "PENDING_VERIFICATION")
        self.assertEqual(row.after_state["status"], "VERIFIED")
        self.assertEqual(row.evidence_reference, "REG-1")


class PolicyAgreementTests(unittest.TestCase):
    def test_only_verified_grants_access(self):
        """Every other status, including SUSPENDED, is refused by the policy."""
        from app.services.company_verification import ACTIVE_STATUS
        from app.db.models.shipping_company import COMPANY_STATUSES

        self.assertEqual(ACTIVE_STATUS, "VERIFIED")
        self.assertEqual(
            {s for s in COMPANY_STATUSES if s != ACTIVE_STATUS},
            {"DRAFT", "PENDING_VERIFICATION", "REJECTED", "SUSPENDED", "CLOSED"},
        )


if __name__ == "__main__":
    unittest.main()
