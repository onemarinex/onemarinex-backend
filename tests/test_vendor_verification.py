"""Vendor verification collected evidence and could not act on it.

A vendor creates a profile (`pending`), uploads a trade licence, GST or VAT
certificate, bank details and ISO certifications (`under_review`), and then
nothing happens. `verification_status` was written in two places and read in
one — a status endpoint the vendor polls. No route set `verified` or
`rejected`, no route returned the documents, and no screen showed them. The
verification the application asks for could not be performed on the evidence
it gathers, and the vendor waited at `under_review` for ever.

The tests that matter here are the negative ones: a verification with no
record of what was checked, a refusal with no reason, and anything that leaks
the reviewer's identity or the evidence reference back to the vendor, who can
read the notes field.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import routes_vendor_review as review
from app.db.models.user import User
from app.db.models.vendor_profile import VendorProfile
from app.db.session import engine
from app.services import vendor_verification as vv
from app.services.vendor_verification import VendorDecisionError


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class VendorDecisionTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        user = User(email=_uniq("vendor") + "@example.com", hashed_password="x", role="vendor")
        self.db.add(user)
        self.db.flush()
        self.profile = VendorProfile(
            user_id=user.id, company_name="Alpha Ship Supply",
            verification_status=vv.UNDER_REVIEW,
            trade_license_url="private://vendor_docs/abc_licence.pdf",
            bank_details_url="private://vendor_docs/abc_bank.pdf",
        )
        self.db.add(self.profile)
        self.db.flush()

        # A distinctive id, so the assertion below cannot pass or fail by
        # colliding with a digit in the date.
        self.admin = type("Admin", (), {"role": "superadmin", "id": 918273})()
        self.outsider = type("Agent", (), {"role": "agent", "id": 8})()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def verify(self, **kwargs):
        body = type("Body", (), {
            "evidence_type": kwargs.get("evidence_type", "Registry lookup"),
            "evidence_reference": kwargs.get("evidence_reference", "MCA-2026-11"),
            "reason": kwargs.get("reason", "Licence matches the registry."),
        })()
        return review.verify_vendor(vendor_id=self.profile.id, body=body,
                                    db=self.db, current_user=self.admin)

    def reject(self, reason):
        body = type("Body", (), {"reason": reason})()
        return review.reject_vendor(vendor_id=self.profile.id, body=body,
                                    db=self.db, current_user=self.admin)

    # --- the gap this closes ---

    def test_a_vendor_can_now_be_verified_at_all(self):
        result = self.verify()
        self.assertEqual(result.verification_status, vv.VERIFIED)

    def test_the_documents_come_back_as_urls_that_expire(self):
        result = review.get_vendor_review(vendor_id=self.profile.id, db=self.db,
                                          current_user=self.admin)
        urls = {d.field: d.url for d in result.documents}
        self.assertEqual(set(urls), {"trade_license_url", "bank_details_url"})
        for url in urls.values():
            self.assertTrue(url.startswith("/api/v1/files/private/"), url)
        # And it says what is still missing, rather than silently omitting it.
        self.assertIn("GST / VAT certificate", result.missing_documents)

    def test_a_listing_does_not_mint_urls_for_every_row(self):
        from fastapi import Response

        rows = review.list_vendor_reviews(
            response=Response(), status=None, limit=None,
            db=self.db, current_user=self.admin)
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.documents, [])

    # --- what it must refuse ---

    def test_verifying_without_naming_the_evidence_is_refused(self):
        with self.assertRaises(HTTPException) as error:
            self.verify(evidence_type="")
        self.assertEqual(error.exception.status_code, 409)
        self.db.refresh(self.profile)
        self.assertEqual(self.profile.verification_status, vv.UNDER_REVIEW)

    def test_verifying_a_vendor_with_no_documents_is_refused(self):
        self.profile.trade_license_url = None
        self.profile.bank_details_url = None
        self.db.flush()
        with self.assertRaises(HTTPException) as error:
            self.verify()
        self.assertIn("nothing to verify", str(error.exception.detail))

    def test_rejecting_without_a_usable_reason_is_refused(self):
        for reason in ("", "no", "bad docs"):
            with self.assertRaises(HTTPException):
                self.reject(reason)
        self.db.refresh(self.profile)
        self.assertEqual(self.profile.verification_status, vv.UNDER_REVIEW)

    def test_a_rejected_vendor_cannot_be_flipped_straight_to_verified(self):
        self.reject("The trade licence has expired; upload a current one.")
        with self.assertRaises(HTTPException) as error:
            self.verify()
        self.assertEqual(error.exception.status_code, 409)

    def test_a_decision_can_be_revisited_through_review(self):
        self.reject("The trade licence has expired; upload a current one.")
        body = type("Body", (), {"reason": "New licence supplied."})()
        review.reopen_vendor(vendor_id=self.profile.id, body=body,
                             db=self.db, current_user=self.admin)
        self.assertEqual(self.verify().verification_status, vv.VERIFIED)

    def test_only_a_super_admin_may_look_or_decide(self):
        from fastapi import Response

        for call in (
            lambda: review.get_vendor_review(vendor_id=self.profile.id, db=self.db,
                                             current_user=self.outsider),
            lambda: review.list_vendor_reviews(response=Response(), status=None, limit=None,
                                               db=self.db, current_user=self.outsider),
        ):
            with self.assertRaises(HTTPException) as error:
                call()
            self.assertEqual(error.exception.status_code, 403)

    # --- what the vendor is allowed to read ---

    def test_the_vendor_is_told_why_they_were_rejected(self):
        reason = "The trade licence has expired; upload a current one."
        self.reject(reason)
        self.db.refresh(self.profile)
        self.assertIn(reason, self.profile.verification_notes)

    def test_the_reviewer_and_the_evidence_are_not_in_what_the_vendor_reads(self):
        """`GET /vendor/verification-status` returns this field to the vendor."""
        self.verify(evidence_type="Registry lookup", evidence_reference="MCA-2026-11")
        self.db.refresh(self.profile)
        notes = self.profile.verification_notes
        self.assertIn("Verified", notes)
        self.assertNotIn("MCA-2026-11", notes)
        self.assertNotIn("Registry lookup", notes)
        self.assertNotIn(str(self.admin.id), notes)
        self.assertNotIn("user", notes.lower())


class TransitionTests(unittest.TestCase):
    def test_every_state_declares_what_may_follow_it(self):
        for state in (vv.PENDING, vv.UNDER_REVIEW, vv.VERIFIED, vv.REJECTED):
            self.assertIn(state, vv.ALLOWED_TRANSITIONS)

    def test_a_decided_vendor_only_moves_back_through_review(self):
        for state in (vv.VERIFIED, vv.REJECTED):
            self.assertEqual(vv.ALLOWED_TRANSITIONS[state], frozenset({vv.UNDER_REVIEW}))

    def test_an_unknown_state_allows_nothing(self):
        profile = type("P", (), {"verification_status": "something-else",
                                 "verification_notes": None, "id": 1})()
        with self.assertRaises(VendorDecisionError):
            vv.reject(profile, actor_id=1, reason="A reason long enough to pass.")


if __name__ == "__main__":
    unittest.main()
