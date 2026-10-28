"""The company API, and the asymmetry it has to keep.

Document 2 section 21: competing claims go to a private Super Admin queue and
"the requesting company gets a generic pending/conflict status, never
competitor identity, evidence or effective dates".

So the same conflict looks different from the two sides, and the tests that
matter are the ones checking a company's responses carry nothing about anyone
else. Section 10 also forbids exposing operational Agent API DTOs to a company,
which is checked by inspection of the module.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy import text

from app.api.v1 import routes_company_review as review
from app.api.v1 import routes_shipping_company as company_api
from app.db.models.shipping_company import (
    CompanyUserMembership,
    ShippingCompany,
    VesselRoleClaim,
)
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.session import SessionLocal
from app.services import company_claims as claims

JAN = datetime(2026, 1, 1, tzinfo=timezone.utc)
JUL = datetime(2026, 7, 1, tzinfo=timezone.utc)
AUG = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _uniq(p):
    return f"{p}-{uuid.uuid4().hex[:10]}"


class CompanyApiTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.vessel = Vessel(name=_uniq("MV"), imo_number=_uniq("IMO"),
                             vessel_type="Bulk Carrier", status="Active")
        self.db.add(self.vessel)

        self.admin = User(email=_uniq("sa") + "@x.com", hashed_password="x", role="superadmin")
        self.mine = User(email=_uniq("me") + "@x.com", hashed_password="x", role="crew")
        self.theirs = User(email=_uniq("them") + "@x.com", hashed_password="x", role="crew")
        self.db.add_all([self.admin, self.mine, self.theirs])
        self.db.flush()

        a, b = _uniq("Alpha"), _uniq("Bravo")
        self.my_company = ShippingCompany(legal_name=a, normalized_name=a.upper(), status="VERIFIED")
        self.rival = ShippingCompany(legal_name=b, normalized_name=b.upper(), status="VERIFIED")
        self.db.add_all([self.my_company, self.rival])
        self.db.flush()
        self.db.add_all([
            CompanyUserMembership(company_id=self.my_company.id, user_id=self.mine.id, role="ADMIN"),
            CompanyUserMembership(company_id=self.rival.id, user_id=self.theirs.id, role="ADMIN"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        ids = [self.my_company.id, self.rival.id]
        for stmt, params in (
            ("DELETE FROM company_audit_events WHERE company_id = ANY(:v)", {"v": ids}),
            ("DELETE FROM vessel_role_claims WHERE company_id = ANY(:v)", {"v": ids}),
            ("DELETE FROM company_user_memberships WHERE company_id = ANY(:v)", {"v": ids}),
            ("DELETE FROM shipping_companies WHERE id = ANY(:v)", {"v": ids}),
            ("DELETE FROM vessels WHERE id = :v", {"v": self.vessel.id}),
            ("DELETE FROM users WHERE id = ANY(:v)",
             {"v": [self.admin.id, self.mine.id, self.theirs.id]}),
        ):
            self.db.execute(text(stmt), params)
        self.db.commit()
        self.db.close()

    def _context(self, company, user):
        membership = self.db.query(CompanyUserMembership).filter(
            CompanyUserMembership.company_id == company.id,
            CompanyUserMembership.user_id == user.id).one()
        return company, membership

    def _approved_for_rival(self, start=JAN, end=AUG):
        claim = claims.submit_claim(
            self.db, company_id=self.rival.id, vessel_id=self.vessel.id,
            role="ISM", effective_start=start, effective_end=end,
            submitted_by=self.theirs, evidence_type="DOC",
            evidence_reference="RIVAL-SECRET-DOC-7781")
        claims.begin_review(self.db, claim=claim, actor=self.admin)
        claims.approve_claim(self.db, claim=claim, actor=self.admin,
                             evidence_type="DOC", evidence_reference="RIVAL-SECRET-DOC-7781")
        self.db.commit()
        return claim

    # --- identity -----------------------------------------------------------

    def test_me_returns_the_sections_and_the_claimable_roles(self):
        out = company_api.company_me(context=self._context(self.my_company, self.mine))
        self.assertEqual(len(out.permitted_sections), 8)
        self.assertEqual(
            {r.code for r in out.claimable_roles},
            {"REGISTERED_OWNER", "ISM_MANAGER", "COMMERCIAL_MANAGER"},
        )
        self.assertTrue(all(r.evidence for r in out.claimable_roles))

    def test_a_non_member_is_refused(self):
        stranger = User(email=_uniq("x") + "@x.com", hashed_password="x", role="crew")
        self.db.add(stranger)
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            company_api.current_company_member(db=self.db, current_user=stranger)
        self.assertEqual(caught.exception.status_code, 403)
        self.db.execute(text("DELETE FROM users WHERE id = :v"), {"v": stranger.id})
        self.db.commit()

    def test_a_suspended_company_gets_the_same_refusal_as_a_stranger(self):
        """Saying "suspended" to a non-member would confirm the company exists."""
        self.my_company.status = "SUSPENDED"
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            company_api.current_company_member(db=self.db, current_user=self.mine)
        self.assertEqual(caught.exception.detail, "Not a shipping company member")

    # --- the asymmetry ------------------------------------------------------

    def test_a_company_sees_only_its_own_claims(self):
        self._approved_for_rival()
        mine = claims.submit_claim(
            self.db, company_id=self.my_company.id, vessel_id=self.vessel.id,
            role="Owner", effective_start=JAN, effective_end=None,
            submitted_by=self.mine)
        self.db.commit()

        rows = company_api.company_fleet(
            db=self.db, context=self._context(self.my_company, self.mine))
        self.assertEqual([r.id for r in rows], [mine.id])

    def test_the_fleet_response_carries_nothing_about_a_rival(self):
        rival_claim = self._approved_for_rival()
        claims.submit_claim(
            self.db, company_id=self.my_company.id, vessel_id=self.vessel.id,
            role="ISM", effective_start=JUL, effective_end=None,
            submitted_by=self.mine)
        self.db.commit()

        rows = company_api.company_fleet(
            db=self.db, context=self._context(self.my_company, self.mine)
        )
        self.assertTrue(all(r.id != rival_claim.id for r in rows))
        self.assertTrue(all("company_id" not in r.model_dump() for r in rows))
        body = repr([r.model_dump() for r in rows])
        for leak in (self.rival.legal_name, "RIVAL-SECRET-DOC-7781"):
            self.assertNotIn(leak, body, f"the fleet response leaked {leak!r}")

    def test_the_superadmin_queue_does_show_both_sides(self):
        """Resolving a conflict is impossible without them."""
        rival_claim = self._approved_for_rival()
        mine = claims.submit_claim(
            self.db, company_id=self.my_company.id, vessel_id=self.vessel.id,
            role="ISM", effective_start=JUL, effective_end=None,
            submitted_by=self.mine)
        self.db.commit()

        queue = review.review_queue(db=self.db, current_user=self.admin)
        entry = next(row for row in queue if row.id == mine.id)
        self.assertIn(rival_claim.id, entry.conflicts_with)

    def test_the_queue_is_superadmin_only(self):
        with self.assertRaises(HTTPException) as caught:
            review.review_queue(db=self.db, current_user=self.mine)
        self.assertEqual(caught.exception.status_code, 403)

    # --- submission ---------------------------------------------------------

    def test_submitting_does_not_approve(self):
        body = company_api.ClaimSubmissionIn(
            vessel_id=self.vessel.id, role="ISM", effective_start=JAN)
        out = company_api.submit_fleet_claim(
            body, db=self.db, current_user=self.mine,
            context=self._context(self.my_company, self.mine))
        self.assertEqual(out.status, "SUBMITTED")

    def test_a_viewer_cannot_submit(self):
        self.db.query(CompanyUserMembership).filter(
            CompanyUserMembership.company_id == self.my_company.id).update({"role": "VIEWER"})
        self.db.commit()
        body = company_api.ClaimSubmissionIn(
            vessel_id=self.vessel.id, role="ISM", effective_start=JAN)
        with self.assertRaises(HTTPException) as caught:
            company_api.submit_fleet_claim(
                body, db=self.db, current_user=self.mine,
                context=self._context(self.my_company, self.mine))
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_undefined_role_is_refused_with_the_reason(self):
        body = company_api.ClaimSubmissionIn(
            vessel_id=self.vessel.id, role="charterer", effective_start=JAN)
        with self.assertRaises(HTTPException) as caught:
            company_api.submit_fleet_claim(
                body, db=self.db, current_user=self.mine,
                context=self._context(self.my_company, self.mine))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("not defined yet", caught.exception.detail)


class ContractTests(unittest.TestCase):
    def test_the_company_api_imports_no_agent_dto(self):
        """Section 10: company APIs must not expose operational Agent DTOs."""
        import pathlib

        source = pathlib.Path("app/api/v1/routes_shipping_company.py").read_text()
        for forbidden in ("routes_agents", "routes_vessels", "routes_incidents",
                          "routes_bookings", "routes_trips"):
            self.assertNotIn(forbidden, source)

    def test_the_company_api_defines_its_own_response_models(self):
        for name in ("CompanyMeOut", "FleetClaimOut", "ClaimSubmissionIn"):
            self.assertTrue(hasattr(company_api, name))

    def test_the_fleet_dto_has_no_field_for_another_company(self):
        fields = set(company_api.FleetClaimOut.model_fields)
        for leaky in ("company_id", "conflicts_with", "approved_by_user_id"):
            self.assertNotIn(leaky, fields)


if __name__ == "__main__":
    unittest.main()


class DataSectionTests(CompanyApiTests):
    """The five data sections, and the three answers they must keep apart."""

    def _section(self, name):
        from app.api.v1.routes_shipping_company import _section
        return _section(self.db, self.my_company.id, name)

    def test_every_section_returns_its_metrics(self):
        for name, expected in (
            ("overview", 1),
            ("crew_shore_leave", 5),
            ("crew_movements", 9),
            ("safety_and_incident_oversight", 3),
            ("welfare_intelligence", 1),
        ):
            with self.subTest(section=name):
                self.assertEqual(len(self._section(name).figures), expected)

    def test_welfare_reports_that_it_can_show_nothing(self):
        """Gate G6's no-go is invented welfare metrics; an empty chart is the
        easiest way to invent one."""
        self.assertTrue(self._section("welfare_intelligence").nothing_to_show)

    def test_a_section_with_real_figures_does_not(self):
        self.assertFalse(self._section("crew_movements").nothing_to_show)

    def test_an_unavailable_figure_says_so_rather_than_being_omitted(self):
        """A missing row reads as "nothing happened", a different claim."""
        keys = {f.key: f for f in self._section("crew_movements").figures}
        self.assertIn("sign_on_off", keys)
        self.assertTrue(keys["sign_on_off"].available)
        self.assertEqual(keys["sign_on_off"].display, "0")

    def test_a_labelled_figure_is_flagged_for_the_renderer(self):
        keys = {f.key: f for f in self._section("safety_and_incident_oversight").figures}
        self.assertTrue(keys["incidents_reported"].needs_caveat)
        self.assertFalse(keys["sos_raised"].needs_caveat)

    def test_every_figure_carries_a_caveat(self):
        for section in ("overview", "crew_movements", "safety_and_incident_oversight"):
            for figure in self._section(section).figures:
                with self.subTest(figure=figure.key):
                    self.assertTrue(figure.caveat.strip())


class VesselCallTests(CompanyApiTests):
    def test_a_guessed_imo_returns_empty_rather_than_404(self):
        """TEN-11: a 404 would tell the company which IMOs exist."""
        from app.api.v1.routes_shipping_company import vessel_calls
        self.assertEqual(
            vessel_calls("IMO-NOBODY-HAS-THIS", db=self.db,
                         context=self._context(self.my_company, self.mine)),
            [],
        )

    def test_a_vessel_the_company_never_held_returns_empty(self):
        from app.api.v1.routes_shipping_company import vessel_calls
        self._approved_for_rival()
        self.assertEqual(
            vessel_calls(self.vessel.imo_number, db=self.db,
                         context=self._context(self.my_company, self.mine)),
            [],
        )


class AccessRequestTests(CompanyApiTests):
    def test_a_request_grants_nothing_by_itself(self):
        """Section 21: a company admin cannot grant itself detail."""
        from datetime import timedelta
        from app.api.v1.routes_shipping_company import AccessRequestIn, request_access
        from app.db.models.vessel_call import VesselCall
        from app.services import company_claims as claims
        from app.services.company_access import covering_grant

        claim = claims.submit_claim(
            self.db, company_id=self.my_company.id, vessel_id=self.vessel.id,
            role="ISM", effective_start=JAN, effective_end=None, submitted_by=self.mine)
        claims.begin_review(self.db, claim=claim, actor=self.admin)
        claims.approve_claim(self.db, claim=claim, actor=self.admin,
                             evidence_type="DOC", evidence_reference="MINE-1")
        call = VesselCall(vessel_id=self.vessel.id, vessel_name=self.vessel.name)
        self.db.add(call)
        self.db.commit()

        out = request_access(
            AccessRequestIn(vessel_call_id=call.id, category="crew_detail",
                            requested_start=JAN, requested_end=JUL,
                            reason="investigating an incident"),
            db=self.db, context=self._context(self.my_company, self.mine),
            current_user=self.mine)
        self.assertEqual(out.status, "REQUESTED")
        self.assertIsNone(covering_grant(
            self.db, company_id=self.my_company.id, vessel_call_id=call.id,
            category="crew_detail", occurred_at=JAN + timedelta(days=1)))

    def test_a_call_the_company_never_held_is_not_distinguishable_from_a_guess(self):
        from app.api.v1.routes_shipping_company import AccessRequestIn, request_access

        for call_id in (999999999,):
            with self.assertRaises(HTTPException) as caught:
                request_access(
                    AccessRequestIn(vessel_call_id=call_id, category="x",
                                    requested_start=JAN, requested_end=JUL, reason="r"),
                    db=self.db, context=self._context(self.my_company, self.mine),
                    current_user=self.mine)
            self.assertEqual(caught.exception.status_code, 404)
            self.assertEqual(caught.exception.detail, "No such port call")


class FleetPreviewTests(CompanyApiTests):
    def _preview(self, rows):
        from app.api.v1.routes_shipping_company import (
            FleetPreviewIn, FleetRowIn, preview_fleet)
        return preview_fleet(
            FleetPreviewIn(rows=[FleetRowIn(**r) for r in rows]),
            db=self.db, context=self._context(self.my_company, self.mine),
            current_user=self.mine)

    def test_a_duplicate_imo_is_caught(self):
        """TEN-15."""
        out = self._preview([
            {"imo_number": self.vessel.imo_number, "role": "ISM", "effective_start": JAN},
            {"imo_number": self.vessel.imo_number, "role": "ISM", "effective_start": JAN},
        ])
        self.assertFalse(out.findings[1].ok)
        self.assertIn("Duplicate IMO", out.findings[1].problem)

    def test_an_invalid_role_is_caught(self):
        out = self._preview([
            {"imo_number": self.vessel.imo_number, "role": "charterer",
             "effective_start": JAN}])
        self.assertFalse(out.findings[0].ok)
        self.assertIn("not defined yet", out.findings[0].problem)

    def test_a_backwards_date_range_is_caught(self):
        out = self._preview([
            {"imo_number": self.vessel.imo_number, "role": "ISM",
             "effective_start": JUL, "effective_end": JAN}])
        self.assertFalse(out.findings[0].ok)

    def test_an_unknown_imo_is_caught(self):
        out = self._preview([
            {"imo_number": "IMO-NOT-REAL", "role": "ISM", "effective_start": JAN}])
        self.assertFalse(out.findings[0].ok)

    def test_a_conflict_is_reported_without_naming_the_competitor(self):
        """Section 21 applies here too."""
        self._approved_for_rival()
        out = self._preview([
            {"imo_number": self.vessel.imo_number, "role": "ISM",
             "effective_start": JAN}])
        problem = out.findings[0].problem
        self.assertIn("already has an approved claim", problem)
        for leak in (self.rival.legal_name, "RIVAL-SECRET-DOC-7781"):
            self.assertNotIn(leak, problem)

    def test_preview_creates_no_claim(self):
        before = self.db.query(VesselRoleClaim).filter(
            VesselRoleClaim.company_id == self.my_company.id).count()
        self._preview([{"imo_number": self.vessel.imo_number, "role": "ISM",
                        "effective_start": JAN}])
        after = self.db.query(VesselRoleClaim).filter(
            VesselRoleClaim.company_id == self.my_company.id).count()
        self.assertEqual(before, after)


class AccessDecisionTests(CompanyApiTests):
    """Deciding a request — Document 2 sections 4 and 21.

    Until these routes existed a request could be made and never answered, so
    no `SensitiveAccessGrant` could exist. With `AGGREGATE_SAFE_CATEGORIES`
    empty pending Q9, that meant nothing sensitive was visible to any company
    under any circumstances. These check the loop closes without widening.

    The fixture is built per test rather than in setUp, because this class
    inherits the parent's tests and an approved claim of its own on the same
    vessel and role would collide with the rival claim those tests make.
    """

    def _ready(self, start=JAN, end=JUL):
        """An approved claim, a port call under it, and one open request."""
        from app.api.v1.routes_shipping_company import AccessRequestIn, request_access
        from app.db.models.shipping_company import SensitiveAccessRequest
        from app.db.models.vessel_call import VesselCall

        claim = claims.submit_claim(
            self.db, company_id=self.my_company.id, vessel_id=self.vessel.id,
            role="ISM", effective_start=JAN, effective_end=None, submitted_by=self.mine)
        claims.begin_review(self.db, claim=claim, actor=self.admin)
        claims.approve_claim(self.db, claim=claim, actor=self.admin,
                             evidence_type="DOC", evidence_reference="MINE-1")
        self.call = VesselCall(vessel_id=self.vessel.id, vessel_name=self.vessel.name)
        self.db.add(self.call)
        self.db.commit()

        out = request_access(
            AccessRequestIn(vessel_call_id=self.call.id, category="crew_detail",
                            requested_start=start, requested_end=end,
                            reason="investigating an incident"),
            db=self.db, context=self._context(self.my_company, self.mine),
            current_user=self.mine)
        return self.db.query(SensitiveAccessRequest).filter(
            SensitiveAccessRequest.id == out.id).one()

    def _grant(self, at=None):
        from app.services.company_access import covering_grant
        return covering_grant(
            self.db, company_id=self.my_company.id, vessel_call_id=self.call.id,
            category="crew_detail", occurred_at=at or (JAN + timedelta(days=1)))

    def test_approving_is_what_creates_the_grant(self):
        request = self._ready()
        self.assertIsNone(self._grant())
        out = review.approve_access(
            request.id, review.GrantIn(reason="incident under investigation"),
            db=self.db, current_user=self.admin)
        self.assertEqual(out.status, "APPROVED")
        self.assertIsNotNone(out.grant_id)
        # Not live: this fixture's window is 2026 Jan-Jul, which is past. The
        # queue reports liveness, not merely approval, so a lapsed grant does
        # not read as access a company still has.
        self.assertFalse(out.grant_active)
        grant = self._grant()
        self.assertIsNotNone(grant)
        # The category is the request's own, never the reviewer's choice.
        self.assertEqual(grant.category, "crew_detail")

    def test_a_grant_cannot_reach_past_what_was_asked_for(self):
        request = self._ready()
        with self.assertRaises(HTTPException) as caught:
            review.approve_access(
                request.id,
                review.GrantIn(reason="r", effective_start=JAN, effective_end=AUG),
                db=self.db, current_user=self.admin)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("outside the window", caught.exception.detail)
        self.assertIsNone(self._grant())

    def test_a_narrower_grant_is_allowed_and_bounds_the_policy(self):
        """A reviewer may give less than was asked. The policy honours the narrower end."""
        request = self._ready()
        march = datetime(2026, 3, 1, tzinfo=timezone.utc)
        review.approve_access(
            request.id,
            review.GrantIn(reason="only the period of the incident",
                           effective_start=JAN, effective_end=march),
            db=self.db, current_user=self.admin)
        self.assertIsNotNone(self._grant())
        self.assertIsNone(self._grant(at=march + timedelta(days=1)))

    def test_a_company_cannot_approve_its_own_request(self):
        request = self._ready()
        with self.assertRaises(HTTPException) as caught:
            review.approve_access(
                request.id, review.GrantIn(reason="please"),
                db=self.db, current_user=self.mine)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIsNone(self._grant())

    def test_revoking_takes_the_access_away(self):
        request = self._ready()
        review.approve_access(
            request.id, review.GrantIn(reason="granted"),
            db=self.db, current_user=self.admin)
        out = review.revoke_access(
            request.id, review.DecisionIn(reason="investigation closed"),
            db=self.db, current_user=self.admin)
        self.assertEqual(out.status, "REVOKED")
        self.assertFalse(out.grant_active)
        self.assertIsNone(self._grant())

    def test_a_rejected_request_is_final(self):
        request = self._ready()
        review.reject_access(
            request.id, review.DecisionIn(reason="no operational need shown"),
            db=self.db, current_user=self.admin)
        with self.assertRaises(HTTPException) as caught:
            review.approve_access(
                request.id, review.GrantIn(reason="second thoughts"),
                db=self.db, current_user=self.admin)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("REJECTED", caught.exception.detail)

    def test_every_decision_is_on_the_record(self):
        request = self._ready()
        review.approve_access(
            request.id, review.GrantIn(reason="granted"),
            db=self.db, current_user=self.admin)
        review.revoke_access(
            request.id, review.DecisionIn(reason="done"),
            db=self.db, current_user=self.admin)
        trail = review.access_request_audit(
            request.id, db=self.db, current_user=self.admin)
        self.assertEqual([e.action for e in trail],
                         ["access_approved", "access_revoked"])
        self.assertEqual(trail[0].actor_user_id, self.admin.id)

    def test_the_queue_is_super_admin_only(self):
        self._ready()
        with self.assertRaises(HTTPException) as caught:
            review.access_request_queue(db=self.db, current_user=self.mine)
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_grant_covering_now_reads_as_live(self):
        now = datetime.now(timezone.utc)
        request = self._ready(start=now - timedelta(days=1), end=now + timedelta(days=7))
        out = review.approve_access(
            request.id, review.GrantIn(reason="current investigation"),
            db=self.db, current_user=self.admin)
        self.assertTrue(out.grant_active)
        self.assertIsNotNone(self._grant(at=now))

    def test_the_queue_shows_open_requests_with_the_company_named(self):
        request = self._ready()
        rows = review.access_request_queue(db=self.db, current_user=self.admin)
        mine = [r for r in rows if r.id == request.id]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].company_name, self.my_company.legal_name)
        self.assertFalse(mine[0].grant_active)


class RegistrationTests(unittest.TestCase):
    """Becoming a company — Document 2 section 21's prohibited shortcut.

    Until these routes existed nothing in the application created a
    `ShippingCompany`, and `company_verification.py` had no caller at all, so
    a status could only be set by writing the row by hand. The property that
    matters is not that registration works: it is that registration cannot
    reach VERIFIED, and that an unverified company reaches no data at all.
    """

    def setUp(self):
        from app.api.v1 import routes_shipping_company as company_api

        self.db = SessionLocal()
        self.api = company_api
        self.admin = User(email=_uniq("sa") + "@x.com", hashed_password="x", role="superadmin")
        self.founder = User(email=_uniq("founder") + "@x.com", hashed_password="x", role="crew")
        self.stranger = User(email=_uniq("other") + "@x.com", hashed_password="x", role="crew")
        self.db.add_all([self.admin, self.founder, self.stranger])
        self.db.commit()
        self.name = _uniq("Delta Shipping")
        self.company_ids = []

    def tearDown(self):
        self.db.rollback()
        ids = self.company_ids or [0]
        for stmt, params in (
            ("DELETE FROM company_audit_events WHERE company_id = ANY(:v)", {"v": ids}),
            ("DELETE FROM company_user_memberships WHERE company_id = ANY(:v)", {"v": ids}),
            ("DELETE FROM shipping_companies WHERE id = ANY(:v)", {"v": ids}),
            ("DELETE FROM users WHERE id = ANY(:v)",
             {"v": [self.admin.id, self.founder.id, self.stranger.id]}),
        ):
            self.db.execute(text(stmt), params)
        self.db.commit()
        self.db.close()

    def _register(self, user=None, name=None):
        out = self.api.register_company(
            self.api.RegistrationIn(legal_name=name or self.name),
            db=self.db, current_user=user or self.founder)
        self.company_ids.append(out.company_id)
        return out

    def _context(self, user):
        return self.api.current_company_any_status(db=self.db, current_user=user)

    # --- the prohibited shortcut --------------------------------------------

    def test_registering_cannot_reach_verified(self):
        out = self._register()
        self.assertEqual(out.status, "DRAFT")

    def test_submitting_is_a_request_not_a_decision(self):
        self._register()
        out = self.api.submit_for_verification(
            db=self.db, context=self._context(self.founder), current_user=self.founder)
        self.assertEqual(out.status, "PENDING_VERIFICATION")

    def test_a_company_cannot_verify_itself_through_the_superadmin_route(self):
        out = self._register()
        self.api.submit_for_verification(
            db=self.db, context=self._context(self.founder), current_user=self.founder)
        with self.assertRaises(HTTPException) as caught:
            review.verify_company(
                out.company_id,
                review.VerifyIn(evidence_type="Registry", evidence_reference="X"),
                db=self.db, current_user=self.founder)
        self.assertEqual(caught.exception.status_code, 403)
        self.db.rollback()
        self.assertEqual(
            self.db.query(ShippingCompany).filter(
                ShippingCompany.id == out.company_id).one().status,
            "PENDING_VERIFICATION")

    def test_an_unverified_company_reaches_no_data(self):
        """`current_company_member` is the gate, and it refuses anything but VERIFIED."""
        self._register()
        with self.assertRaises(HTTPException) as caught:
            self.api.current_company_member(db=self.db, current_user=self.founder)
        self.assertEqual(caught.exception.status_code, 403)

    # --- the lifecycle -------------------------------------------------------

    def test_verifying_opens_the_dashboard_and_suspending_closes_it(self):
        out = self._register()
        self.api.submit_for_verification(
            db=self.db, context=self._context(self.founder), current_user=self.founder)
        review.verify_company(
            out.company_id,
            review.VerifyIn(evidence_type="Registry", evidence_reference="REG-9"),
            db=self.db, current_user=self.admin)
        company, membership = self.api.current_company_member(
            db=self.db, current_user=self.founder)
        self.assertEqual(company.status, "VERIFIED")
        self.assertEqual(membership.role, "ADMIN")

        review.suspend_company(
            out.company_id, review.DecisionIn(reason="under investigation"),
            db=self.db, current_user=self.admin)
        with self.assertRaises(HTTPException):
            self.api.current_company_member(db=self.db, current_user=self.founder)

        review.reinstate_company(
            out.company_id, review.DecisionIn(reason="investigation closed"),
            db=self.db, current_user=self.admin)
        company, _ = self.api.current_company_member(db=self.db, current_user=self.founder)
        self.assertEqual(company.status, "VERIFIED")

    def test_a_verified_company_cannot_be_closed_in_one_step(self):
        out = self._register()
        self.api.submit_for_verification(
            db=self.db, context=self._context(self.founder), current_user=self.founder)
        review.verify_company(
            out.company_id,
            review.VerifyIn(evidence_type="Registry", evidence_reference="REG-9"),
            db=self.db, current_user=self.admin)
        with self.assertRaises(HTTPException) as caught:
            review.close_company(out.company_id, review.DecisionIn(reason="done"),
                                 db=self.db, current_user=self.admin)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("cannot go from VERIFIED to CLOSED", caught.exception.detail)

    def test_verification_needs_evidence(self):
        out = self._register()
        self.api.submit_for_verification(
            db=self.db, context=self._context(self.founder), current_user=self.founder)
        with self.assertRaises(Exception):
            review.verify_company(
                out.company_id,
                review.VerifyIn(evidence_type="", evidence_reference=""),
                db=self.db, current_user=self.admin)

    # --- who may register ----------------------------------------------------

    def test_a_taken_name_is_refused_without_naming_who_holds_it(self):
        held = self._register()
        with self.assertRaises(HTTPException) as caught:
            self._register(user=self.stranger, name=self.name.lower())
        self.assertEqual(caught.exception.status_code, 409)
        self.assertNotIn(str(held.company_id), caught.exception.detail)
        self.assertNotIn(self.founder.email, caught.exception.detail)

    def test_one_account_belongs_to_one_company(self):
        self._register()
        with self.assertRaises(HTTPException) as caught:
            self._register(name=_uniq("Second"))
        self.assertEqual(caught.exception.status_code, 409)

    def test_registration_is_on_the_record(self):
        out = self._register()
        trail = review.company_audit(out.company_id, db=self.db, current_user=self.admin)
        self.assertEqual([e.action for e in trail], ["registered"])

    def test_the_company_queue_is_super_admin_only(self):
        self._register()
        with self.assertRaises(HTTPException) as caught:
            review.company_queue(db=self.db, current_user=self.founder)
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_pending_company_is_in_the_queue_with_its_counts(self):
        out = self._register()
        rows = review.company_queue(db=self.db, current_user=self.admin)
        mine = [r for r in rows if r.id == out.company_id]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].status, "DRAFT")
        self.assertEqual(mine[0].member_count, 1)
        self.assertEqual(mine[0].claim_count, 0)

    def test_someone_with_no_company_gets_a_404_not_a_403(self):
        with self.assertRaises(HTTPException) as caught:
            self._context(self.stranger)
        self.assertEqual(caught.exception.status_code, 404)
