"""The one predicate every company endpoint must go through.

Document 2 section 7. The cases that matter are the ones where a company sees
something it did not hold: an event on the wrong side of a handover, a vessel
it no longer manages, a category nobody granted, or a legacy row whose origin
was never reconciled.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.db.models.shipping_company import (
    CompanyUserMembership,
    SensitiveAccessGrant,
    ShippingCompany,
    VesselRoleClaim,
)
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.session import engine
from app.services import company_access as policy
from app.services.company_access import Decision

JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
JUL = datetime(2026, 7, 1, tzinfo=timezone.utc)
OCT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.user = User(email=_uniq("viewer") + "@example.com", hashed_password="x", role="crew")
        self.db.add(self.user)
        self.db.flush()

        self.vessel = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        self.db.flush()

        self.company = self._company("VERIFIED")
        self._member(self.company, self.user)
        self.claim = self._claim(self.company, JAN, JUL, status="APPROVED")
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    # --- fixtures -----------------------------------------------------------

    def _company(self, status, name=None):
        name = name or _uniq("Company")
        company = ShippingCompany(
            legal_name=name, normalized_name=name.upper(), status=status,
        )
        self.db.add(company)
        self.db.flush()
        return company

    def _member(self, company, user, enabled=True):
        membership = CompanyUserMembership(
            company_id=company.id, user_id=user.id, role="VIEWER", enabled=enabled,
        )
        self.db.add(membership)
        self.db.flush()
        return membership

    def _claim(self, company, start, end, *, status="APPROVED", role="ISM", vessel=None):
        claim = VesselRoleClaim(
            company_id=company.id, vessel_id=(vessel or self.vessel).id,
            role=role, effective_start=start, effective_end=end, status=status,
        )
        self.db.add(claim)
        self.db.flush()
        return claim

    def _allow(self, **kwargs):
        params = dict(
            company_id=self.company.id, viewer_user_id=self.user.id,
            vessel_id=self.vessel.id, occurred_at=JAN + timedelta(days=30),
            category="shore_leave_count",
        )
        params.update(kwargs)
        return policy.allow(self.db, **params)

    # --- the handover, which is the whole point -----------------------------

    def test_an_event_inside_tenure_is_allowed_once_a_grant_exists(self):
        grant = self._grant(JAN, JUL)
        result = self._allow(vessel_call_id=grant.vessel_call_id)
        self.assertTrue(result, result.reason)
        self.assertEqual(result.claim_id, self.claim.id)
        self.assertEqual(result.grant_id, grant.id)

    def test_an_event_after_the_period_ends_is_refused(self):
        result = self._allow(occurred_at=JUL + timedelta(days=1))
        self.assertFalse(result)
        self.assertEqual(result.reason, Decision.OUTSIDE_TENURE)

    def test_an_event_before_the_period_starts_is_refused(self):
        result = self._allow(occurred_at=JAN - timedelta(seconds=1))
        self.assertFalse(result)
        self.assertEqual(result.reason, Decision.OUTSIDE_TENURE)

    def test_the_start_instant_is_inside_the_period(self):
        self.assertIsNotNone(policy.covering_claim(
            self.db, company_id=self.company.id,
            vessel_id=self.vessel.id, occurred_at=JAN,
        ))

    def test_the_end_instant_belongs_to_the_next_company_not_this_one(self):
        """Half-open [start, end): the boundary is the incoming company's."""
        self.assertIsNone(policy.covering_claim(
            self.db, company_id=self.company.id,
            vessel_id=self.vessel.id, occurred_at=JUL,
        ))

    def test_a_handover_gives_one_event_to_exactly_one_company(self):
        incoming = self._company("VERIFIED")
        self._member(incoming, self.user)
        self._claim(incoming, JUL, None)
        at_boundary = dict(
            viewer_user_id=self.user.id, vessel_id=self.vessel.id,
            occurred_at=JUL, category="x",
        )
        outgoing_sees = policy.allow(self.db, company_id=self.company.id, **at_boundary)
        incoming_sees = policy.covering_claim(
            self.db, company_id=incoming.id,
            vessel_id=self.vessel.id, occurred_at=JUL,
        )
        self.assertFalse(outgoing_sees)
        self.assertIsNotNone(incoming_sees)

    def test_an_ongoing_claim_has_no_end(self):
        ongoing = self._company("VERIFIED")
        self._member(ongoing, self.user)
        self._claim(ongoing, JAN, None, role="OWNER")
        self.assertIsNotNone(policy.covering_claim(
            self.db, company_id=ongoing.id,
            vessel_id=self.vessel.id, occurred_at=OCT,
        ))

    # --- the other conjuncts ------------------------------------------------

    def test_an_unverified_company_sees_nothing(self):
        for status in ("DRAFT", "PENDING_VERIFICATION", "SUSPENDED", "REJECTED", "CLOSED"):
            with self.subTest(status=status):
                self.company.status = status
                self.db.flush()
                result = self._allow()
                self.assertFalse(result)
                self.assertEqual(result.reason, Decision.COMPANY_NOT_VERIFIED)

    def test_a_disabled_member_sees_nothing(self):
        self.db.query(CompanyUserMembership).filter(
            CompanyUserMembership.company_id == self.company.id
        ).update({"enabled": False})
        self.db.flush()
        self.assertEqual(self._allow().reason, Decision.VIEWER_NOT_A_MEMBER)

    def test_someone_from_another_company_sees_nothing(self):
        stranger = User(email=_uniq("x") + "@example.com", hashed_password="x", role="crew")
        self.db.add(stranger)
        self.db.flush()
        self.assertEqual(
            self._allow(viewer_user_id=stranger.id).reason,
            Decision.VIEWER_NOT_A_MEMBER,
        )

    def test_a_claim_that_is_not_approved_grants_nothing(self):
        for status in ("DRAFT", "SUBMITTED", "UNDER_REVIEW", "REJECTED", "REVOKED"):
            with self.subTest(status=status):
                self.claim.status = status
                self.db.flush()
                self.assertEqual(self._allow().reason, Decision.OUTSIDE_TENURE)

    def test_a_revoked_claim_grants_nothing(self):
        self.claim.revoked_at = datetime.now(timezone.utc)
        self.db.flush()
        self.assertEqual(self._allow().reason, Decision.OUTSIDE_TENURE)

    def test_another_vessel_is_not_covered_by_this_claim(self):
        other = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(other)
        self.db.flush()
        self.assertEqual(
            self._allow(vessel_id=other.id).reason, Decision.OUTSIDE_TENURE
        )

    def test_an_unresolved_legacy_row_is_refused_not_quietly_included(self):
        self.assertEqual(
            self._allow(origin_resolved=False).reason, Decision.ORIGIN_UNRESOLVED
        )

    def test_an_event_with_no_timestamp_is_refused(self):
        self.assertEqual(self._allow(occurred_at=None).reason, Decision.NO_TIMESTAMP)

    def test_an_event_with_no_canonical_vessel_is_refused(self):
        self.assertEqual(self._allow(vessel_id=None).reason, Decision.NO_VESSEL)

    # --- categories and grants ----------------------------------------------

    def _grant(self, start, end, category="shore_leave_count", call_id=None):
        grant = SensitiveAccessGrant(
            company_id=self.company.id,
            vessel_call_id=call_id if call_id is not None else self._call_id(),
            category=category, effective_start=start, effective_end=end,
        )
        self.db.add(grant)
        self.db.flush()
        self._last_call_id = grant.vessel_call_id
        return grant

    def _call_id(self, ended=False):
        """A vessel call. Only the first may be open: `uq_vessel_calls_active_vessel`
        allows one open call per vessel, which is the production invariant."""
        from app.db.models.vessel_call import VesselCall

        call = VesselCall(
            vessel_id=self.vessel.id, vessel_name=self.vessel.name,
            ended_at=(JAN + timedelta(days=1)) if ended else None,
        )
        self.db.add(call)
        self.db.flush()
        return call.id

    def test_with_no_agreed_safe_categories_everything_needs_a_grant(self):
        """Q9 is undecided, so the policy fails closed rather than open."""
        self.assertEqual(policy.AGGREGATE_SAFE_CATEGORIES, frozenset())
        self.assertEqual(self._allow().reason, Decision.CATEGORY_NEEDS_GRANT)

    def test_a_grant_for_a_different_category_does_not_help(self):
        self._grant(JAN, JUL, category="something_else")
        result = self._allow(category="shore_leave_count",
                             vessel_call_id=self._last_call_id)
        self.assertEqual(result.reason, Decision.CATEGORY_NEEDS_GRANT)

    def test_a_grant_for_a_different_call_does_not_help(self):
        self._grant(JAN, JUL)
        other_call = self._call_id(ended=True)
        self.assertEqual(
            self._allow(vessel_call_id=other_call).reason,
            Decision.CATEGORY_NEEDS_GRANT,
        )

    def test_an_expired_grant_does_not_help(self):
        self._grant(JAN, JAN + timedelta(days=1))
        self.assertEqual(
            self._allow(occurred_at=JAN + timedelta(days=30),
                        vessel_call_id=self._last_call_id).reason,
            Decision.CATEGORY_NEEDS_GRANT,
        )

    def test_a_revoked_grant_does_not_help(self):
        grant = self._grant(JAN, JUL)
        grant.revoked_at = datetime.now(timezone.utc)
        self.db.flush()
        self.assertEqual(
            self._allow(vessel_call_id=grant.vessel_call_id).reason,
            Decision.CATEGORY_NEEDS_GRANT,
        )

    def test_a_grant_never_widens_the_tenure_it_sits_inside(self):
        """The document is explicit. A grant covering a period the company did
        not hold must still be refused."""
        grant = self._grant(JAN, OCT)
        result = self._allow(
            occurred_at=JUL + timedelta(days=10), vessel_call_id=grant.vessel_call_id,
        )
        self.assertFalse(result)
        self.assertEqual(result.reason, Decision.OUTSIDE_TENURE)

    # --- filtering before GROUP BY -------------------------------------------

    def test_visible_vessels_is_scoped_to_the_instant(self):
        self.assertEqual(
            policy.visible_vessel_ids(
                self.db, company_id=self.company.id, occurred_at=JAN + timedelta(days=5),
            ),
            [self.vessel.id],
        )
        self.assertEqual(
            policy.visible_vessel_ids(
                self.db, company_id=self.company.id, occurred_at=OCT,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()


class OverlapInvariantTests(unittest.TestCase):
    """Two approvals of the same role on one vessel must not both succeed.

    Document 2 section 6 requires this in the database rather than the ORM,
    because an ORM check reads before it writes and two writers both read
    nothing. Committed transactions on separate connections, so they clean up
    after themselves rather than rolling back.
    """

    def setUp(self):
        from app.db.session import SessionLocal

        self.db = SessionLocal()
        name = _uniq("Overlap")
        self.vessel = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.company_a = ShippingCompany(
            legal_name=name + "-A", normalized_name=(name + "-A").upper(), status="VERIFIED",
        )
        self.company_b = ShippingCompany(
            legal_name=name + "-B", normalized_name=(name + "-B").upper(), status="VERIFIED",
        )
        self.db.add_all([self.vessel, self.company_a, self.company_b])
        self.db.commit()

    def tearDown(self):
        from sqlalchemy import text

        self.db.execute(text("DELETE FROM vessel_role_claims WHERE vessel_id = :v"),
                        {"v": self.vessel.id})
        self.db.execute(text("DELETE FROM shipping_companies WHERE id IN (:a, :b)"),
                        {"a": self.company_a.id, "b": self.company_b.id})
        self.db.execute(text("DELETE FROM vessels WHERE id = :v"), {"v": self.vessel.id})
        self.db.commit()
        self.db.close()

    def _approve(self, company, start, end, role="ISM"):
        claim = VesselRoleClaim(
            company_id=company.id, vessel_id=self.vessel.id, role=role,
            effective_start=start, effective_end=end, status="APPROVED",
        )
        self.db.add(claim)
        self.db.commit()
        return claim

    def test_a_clean_handover_is_allowed(self):
        """A ISM [Jan, Jul); B ISM [Jul, open) — both approved, no overlap."""
        self._approve(self.company_a, JAN, JUL)
        self._approve(self.company_b, JUL, None)  # must not raise

    def test_an_overlapping_same_role_approval_is_refused_by_the_database(self):
        """A ISM [Jan, Aug); B ISM [Jul, open) — the second must fail."""
        from sqlalchemy.exc import IntegrityError

        self._approve(self.company_a, JAN, datetime(2026, 8, 1, tzinfo=timezone.utc))
        with self.assertRaises(IntegrityError):
            self._approve(self.company_b, JUL, None)
        self.db.rollback()

    def test_two_companies_holding_different_roles_is_allowed(self):
        """A Owner and B ISM on one vessel over the same dates is legitimate."""
        self._approve(self.company_a, JAN, None, role="OWNER")
        self._approve(self.company_b, JAN, None, role="ISM")  # must not raise

    def test_one_company_holding_two_roles_is_allowed(self):
        self._approve(self.company_a, JAN, None, role="OWNER")
        self._approve(self.company_a, JAN, None, role="COMMERCIAL")  # must not raise

    def test_a_pending_claim_does_not_block_a_live_one(self):
        pending = VesselRoleClaim(
            company_id=self.company_a.id, vessel_id=self.vessel.id, role="ISM",
            effective_start=JAN, effective_end=None, status="SUBMITTED",
        )
        self.db.add(pending)
        self.db.commit()
        self._approve(self.company_b, JAN, None)  # must not raise

    def test_a_revoked_claim_does_not_block_its_replacement(self):
        revoked = self._approve(self.company_a, JAN, None)
        revoked.revoked_at = datetime.now(timezone.utc)
        self.db.commit()
        self._approve(self.company_b, JAN, None)  # must not raise


class EventFamilyTests(PolicyTests):
    """A family this schema cannot attribute must be refused by the policy,
    not left to each caller to remember."""

    def test_a_family_with_no_usable_time_is_refused(self):
        for family in ("shore_pass_approved",):
            with self.subTest(family=family):
                result = self._allow(event_family=family)
                self.assertFalse(result)
                self.assertEqual(result.reason, Decision.FAMILY_HAS_NO_USABLE_TIME)

    def test_an_unlisted_family_is_refused_rather_than_assumed_fine(self):
        result = self._allow(event_family="something_nobody_inspected")
        self.assertEqual(result.reason, Decision.FAMILY_HAS_NO_USABLE_TIME)

    def test_the_family_check_runs_before_anything_else(self):
        """So an unattributable family cannot be allowed by a generous grant."""
        self.company.status = "VERIFIED"
        grant = self._grant(JAN, OCT)
        self.db.flush()
        result = self._allow(
            event_family="shore_pass_approved", vessel_call_id=grant.vessel_call_id,
        )
        self.assertEqual(result.reason, Decision.FAMILY_HAS_NO_USABLE_TIME)

    def test_an_attributable_family_still_goes_through_the_full_predicate(self):
        grant = self._grant(JAN, JUL)
        result = self._allow(
            event_family="sos_raised", vessel_call_id=grant.vessel_call_id,
        )
        self.assertTrue(result, result.reason)

    def test_omitting_the_family_leaves_the_caller_responsible(self):
        """Backwards compatible: the check is opt-in per call site."""
        grant = self._grant(JAN, JUL)
        self.assertTrue(self._allow(vessel_call_id=grant.vessel_call_id))
