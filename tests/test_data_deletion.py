"""Asking for your data to be deleted — the owner's "in this release" answer.

The answer's shape is unusual and the tests follow it: the thing being built
is a *request*, not an eraser. "It should initiate an authenticated request and
confirmation, not immediate destructive deletion", and "do not claim full
compliance merely because the button exists".

So the properties worth pinning are the negative ones. Asking erases nothing.
Approving erases nothing. A refusal cannot be given without naming the lawful
exception the requester is shown. And no module in this feature writes to any
table it describes — which the last test checks by inspection, because a
future edit that quietly added one would otherwise pass everything else.
"""

import unittest
import uuid

from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy import text

from app.api.v1 import routes_privacy as privacy
from app.db.models.data_deletion import DataDeletionRequest
from app.db.models.user import User
from app.db.session import SessionLocal
from app.services import data_deletion as deletion

SUPERADMIN_ROLE = "superadmin"


def _uniq(p):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class DeletionRequestTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.person = User(email=_uniq("crew") + "@x.com", hashed_password="x", role="crew")
        self.other = User(email=_uniq("other") + "@x.com", hashed_password="x", role="crew")
        self.admin = User(email=_uniq("sa") + "@x.com", hashed_password="x",
                          role=SUPERADMIN_ROLE)
        self.db.add_all([self.person, self.other, self.admin])
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        ids = [self.person.id, self.other.id, self.admin.id]
        self.db.execute(text("DELETE FROM data_deletion_requests WHERE user_id = ANY(:v)"),
                        {"v": ids})
        self.db.execute(text("DELETE FROM users WHERE id = ANY(:v)"), {"v": ids})
        self.db.commit()
        self.db.close()

    def _ask(self, user=None, reason="I have left the industry"):
        return privacy.request_deletion(
            privacy.DeletionRequestIn(reason=reason),
            db=self.db, current_user=user or self.person)

    # --- what it must not do -------------------------------------------------

    def test_asking_erases_nothing(self):
        out = self._ask()
        self.assertEqual(out.status, "REQUESTED")
        # The person is still there, which is the whole point of the answer.
        self.assertIsNotNone(
            self.db.query(User).filter(User.id == self.person.id).first())

    def test_approving_erases_nothing(self):
        out = self._ask()
        privacy.approve(out.id, privacy.AdminDecisionIn(reason="No lawful bar"),
                        db=self.db, current_user=self.admin)
        self.assertIsNotNone(
            self.db.query(User).filter(User.id == self.person.id).first())
        self.assertEqual(
            self.db.query(DataDeletionRequest).filter(
                DataDeletionRequest.id == out.id).one().status,
            "APPROVED")

    def test_no_module_in_this_feature_deletes_or_anonymises_anything(self):
        """By inspection, because the other tests cannot see a new write.

        The owner's answer forbids immediate destructive deletion and the
        standing instruction forbids a deliverable that rewrites existing
        production rows. Both are properties of the *code*, not of one run.
        """
        import inspect

        from app.services import data_deletion as service

        for module in (service, privacy):
            source = inspect.getsource(module)
            for forbidden in (".delete()", "db.delete(", "DELETE FROM"):
                self.assertNotIn(
                    forbidden, source,
                    f"{module.__name__} must not erase data; found {forbidden!r}")

    # --- the request itself --------------------------------------------------

    def test_a_refusal_must_name_the_retention_exception(self):
        out = self._ask()
        with self.assertRaises(HTTPException) as caught:
            privacy.refuse(out.id, privacy.RefusalIn(reason="Still required",
                                                     retention_note="   "),
                           db=self.db, current_user=self.admin)
        # Whitespace passes the schema's min_length and is caught by the
        # service, so this is a 400 rather than a validation error.
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("lawful retention exception", caught.exception.detail)

    def test_a_refusal_carries_its_reason_back_to_the_requester(self):
        out = self._ask()
        privacy.refuse(
            out.id,
            privacy.RefusalIn(reason="An open incident names you",
                              retention_note="Safety records are kept for the vessel."),
            db=self.db, current_user=self.admin)
        controls = privacy.data_controls(db=self.db, current_user=self.person)
        self.assertEqual(controls.request.status, "REFUSED")
        self.assertIn("Safety records", controls.request.retention_note)

    def test_only_the_requester_may_withdraw_it(self):
        out = self._ask()
        with self.assertRaises(HTTPException) as caught:
            privacy.cancel_deletion(out.id, db=self.db, current_user=self.other)
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_decided_request_cannot_be_withdrawn(self):
        out = self._ask()
        privacy.refuse(
            out.id,
            privacy.RefusalIn(reason="r", retention_note="Safety records."),
            db=self.db, current_user=self.admin)
        with self.assertRaises(HTTPException) as caught:
            privacy.cancel_deletion(out.id, db=self.db, current_user=self.person)
        self.assertEqual(caught.exception.status_code, 400)

    def test_one_outstanding_request_per_person(self):
        self._ask()
        with self.assertRaises(HTTPException) as caught:
            self._ask()
        self.assertEqual(caught.exception.status_code, 409)

    def test_a_withdrawn_request_frees_the_person_to_ask_again(self):
        out = self._ask()
        privacy.cancel_deletion(out.id, db=self.db, current_user=self.person)
        again = self._ask()
        self.assertEqual(again.status, "REQUESTED")

    def test_fulfilment_is_recorded_not_performed(self):
        out = self._ask()
        privacy.approve(out.id, privacy.AdminDecisionIn(reason="ok"),
                        db=self.db, current_user=self.admin)
        done = privacy.mark_fulfilled(
            out.id, privacy.FulfilmentIn(note="Profile and sign-in removed by hand"),
            db=self.db, current_user=self.admin)
        self.assertEqual(done.status, "FULFILLED")
        self.assertIsNotNone(done.fulfilled_at)
        self.assertIsNotNone(
            self.db.query(User).filter(User.id == self.person.id).first())

    def test_fulfilment_needs_an_approval_first(self):
        out = self._ask()
        with self.assertRaises(HTTPException) as caught:
            privacy.mark_fulfilled(out.id, privacy.FulfilmentIn(note="done"),
                                   db=self.db, current_user=self.admin)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("cannot go from REQUESTED to FULFILLED", caught.exception.detail)

    # --- who decides ---------------------------------------------------------

    def test_a_person_cannot_approve_their_own_request(self):
        out = self._ask()
        with self.assertRaises(HTTPException) as caught:
            privacy.approve(out.id, privacy.AdminDecisionIn(reason="please"),
                            db=self.db, current_user=self.person)
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_queue_is_restricted(self):
        self._ask()
        with self.assertRaises(HTTPException) as caught:
            privacy.deletion_queue(db=self.db, current_user=self.person)
        self.assertEqual(caught.exception.status_code, 403)

    def test_the_queue_shows_open_requests_with_the_person_named(self):
        out = self._ask()
        rows = privacy.deletion_queue(db=self.db, current_user=self.admin)
        mine = [r for r in rows if r.id == out.id]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].user_email, self.person.email)

    # --- what the requester is told ------------------------------------------

    def test_the_classification_is_shown_before_asking(self):
        controls = privacy.data_controls(db=self.db, current_user=self.person)
        self.assertIsNone(controls.request)
        self.assertTrue(controls.erasable)
        self.assertTrue(controls.retained)
        self.assertTrue(controls.third_party)
        self.assertIn("backup", controls.backups.lower())
        titles = " ".join(c.title for c in controls.retained).lower()
        # The four the owner's answer names, minus third-party which has its
        # own list: operational/safety, financial, and the audit trail.
        self.assertIn("safety", titles)
        self.assertIn("financial", titles)
        self.assertIn("audit", titles)
