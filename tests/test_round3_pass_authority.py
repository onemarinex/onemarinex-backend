"""Exact-call authorization, renewal and stale ORM/race regressions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from fastapi import HTTPException
from sqlalchemy import update

import test_shore_pass_scoping as scoped
import test_company_operations_http as fixture
from app.api.v1.routes_agents import (get_shore_pass_requests, get_shore_pass_request,
    approve_shore_pass, reject_shore_pass, renew_shore_pass, ShorePassActionIn)
from app.api.v1.routes_crew import verify_shorepass, _require_booking_entitlement
from app.api.v1.routes_vessels import (update_vessel, VesselIn, update_crew_member,
    CrewMemberUpdate, set_vessel_shore_pass_validity, ShorePassValidityIn)
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.shore_pass import ShorePass
from app.db.models.vessel_crew import VesselCrew
from app.db.models.user import User
from app.db.session import SessionLocal
from app.services.shore_leave_eligibility import shore_leave_decision, lock_shore_leave_context

UTC = timezone.utc


class ExactPassAuthority(unittest.TestCase):
    setUp = scoped.ShorePassScopingTests.setUp
    tearDown = scoped.ShorePassScopingTests.tearDown
    make_agency_with_request = scoped.ShorePassScopingTests.make_agency_with_request
    PORT = scoped.ShorePassScopingTests.PORT

    def assignment(self, shore_pass=None):
        return self.db.get(CrewAssignment, (shore_pass or self.pass_a).crew_assignment_id)

    def expired(self):
        assignment = self.assignment()
        old = self.pass_a
        old.status = "approved"; old.is_verified = True
        old.expires_at = datetime.now(UTC)-timedelta(hours=1)
        old.out_time = old.expires_at-timedelta(hours=2)
        old.in_time = old.expires_at
        assignment.vessel_crew.shore_pass_valid_upto = old.expires_at
        self.db.flush()
        return old, assignment

    def test_same_person_on_two_agencies_does_not_cross_scope(self):
        other = self.assignment(self.pass_b)
        other.crew_profile_id = self.pass_a.crew_profile_id
        self.pass_b.crew_profile_id = self.pass_a.crew_profile_id
        self.db.flush()
        self.assertEqual([r.id for r in get_shore_pass_requests(db=self.db, current_user=self.agent_a)], [self.pass_a.id])
        for operation in (get_shore_pass_request, approve_shore_pass, renew_shore_pass):
            with self.assertRaises(HTTPException) as denied:
                operation(self.pass_b.id, db=self.db, current_user=self.agent_a)
            self.assertEqual(denied.exception.status_code, 404)
        with self.assertRaises(HTTPException) as denied:
            reject_shore_pass(self.pass_b.id, ShorePassActionIn(rejection_reason="No"), db=self.db, current_user=self.agent_a)
        self.assertEqual(denied.exception.status_code, 404)

    def test_unresolved_and_mismatched_legacy_context_fails_closed(self):
        self.pass_a.vessel_call_id = self.pass_b.vessel_call_id
        self.db.flush()
        self.assertEqual(get_shore_pass_requests(db=self.db, current_user=self.agent_a), [])
        with self.assertRaises(HTTPException):
            get_shore_pass_request(self.pass_a.id, db=self.db, current_user=self.agent_a)
        self.pass_a.vessel_call_id = self.pass_a.crew_assignment_id = None
        self.db.flush()
        with self.assertRaises(HTTPException):
            approve_shore_pass(self.pass_a.id, db=self.db, current_user=self.agent_a)

    def test_role_allowlist_precedes_vessel_lookup(self):
        vessel = self.assignment().vessel_call.vessel
        body = VesselIn(name=vessel.name, imo_number=vessel.imo_number, vessel_type=vessel.vessel_type)
        for role in ("crew", "aggregator", "shipping_company", "driver", "unknown"):
            for vessel_id in (vessel.id, 10**9):
                with self.assertRaises(HTTPException) as denied:
                    update_vessel(vessel_id, body, current_user=SimpleNamespace(role=role, id=10**9), db=self.db)
                self.assertEqual(denied.exception.status_code, 403)
        with self.assertRaises(HTTPException):
            update_vessel(vessel.id, body, current_user=self.agent_b, db=self.db)
        self.assertEqual(update_vessel(vessel.id, body, current_user=self.agent_a, db=self.db).id, vessel.id)
        self.assertEqual(update_vessel(vessel.id, body, current_user=SimpleNamespace(role="superadmin", id=10**9), db=self.db).id, vessel.id)

    def test_explicit_individual_extension_can_issue_new_pass_preserving_history(self):
        old, assignment = self.expired()
        original = (old.id, old.status, old.out_time, old.in_time, old.expires_at)
        future = datetime.now(UTC)+timedelta(hours=5)
        update_crew_member(assignment.vessel_call.vessel_id, assignment.vessel_crew_id,
            CrewMemberUpdate(shore_pass_valid_upto=future), current_user=self.agent_a, db=self.db)
        self.assertEqual(shore_leave_decision(self.db, assignment).status, "EXPIRED")
        self.assertTrue(get_shore_pass_request(old.id, db=self.db, current_user=self.agent_a).can_renew)
        new = renew_shore_pass(old.id, db=self.db, current_user=self.agent_a)
        self.assertNotEqual(new.id, old.id)
        self.db.refresh(old)
        self.assertEqual((old.id, old.status, old.out_time, old.in_time, old.expires_at), original)
        self.assertEqual(new.expires_at, future)
        self.assertTrue(shore_leave_decision(self.db, assignment).eligible)
        self.assertFalse(get_shore_pass_request(old.id, db=self.db, current_user=self.agent_a).can_renew)
        with self.assertRaises(HTTPException):
            renew_shore_pass(old.id, db=self.db, current_user=self.agent_a)

    def test_master_extension_preserves_override_until_apply_all(self):
        old, assignment = self.expired()
        future = datetime.now(UTC)+timedelta(hours=5)
        set_vessel_shore_pass_validity(assignment.vessel_call.vessel_id,
            ShorePassValidityIn(shore_pass_valid_upto=future, apply_to_all=False), current_user=self.agent_a, db=self.db)
        with self.assertRaises(HTTPException):
            renew_shore_pass(old.id, db=self.db, current_user=self.agent_a)
        set_vessel_shore_pass_validity(assignment.vessel_call.vessel_id,
            ShorePassValidityIn(shore_pass_valid_upto=future, apply_to_all=True), current_user=self.agent_a, db=self.db)
        self.assertNotEqual(renew_shore_pass(old.id, db=self.db, current_user=self.agent_a).id, old.id)

    def test_rejected_and_historical_passes_cannot_renew(self):
        old, assignment = self.expired()
        assignment.vessel_crew.shore_pass_valid_upto = datetime.now(UTC)+timedelta(hours=2)
        old.status = "rejected"; self.db.flush()
        with self.assertRaises(HTTPException):
            renew_shore_pass(old.id, db=self.db, current_user=self.agent_a)
        old.status = "approved"; assignment.vessel_call.ended_at = datetime.now(UTC); self.db.flush()
        self.assertFalse(get_shore_pass_request(old.id, db=self.db, current_user=self.agent_a).can_renew)
        with self.assertRaises(HTTPException):
            renew_shore_pass(old.id, db=self.db, current_user=self.agent_a)


class PassDecisionRace(unittest.TestCase):
    tearDown = fixture.OperationsHTTP.tearDown

    def setUp(self):
        fixture.OperationsHTTP.setUp(self)
        user = User(email="pass-race@example.com", hashed_password="x", role="crew")
        self.db.add(user); self.db.flush()
        self.profile = CrewProfile(user_id=user.id, full_name="Fixture crew", rank="able_seaman", nationality="IN")
        self.db.add(self.profile); self.db.flush()
        manifest = VesselCrew(vessel_id=self.vessel.id, name="Fixture crew", rank="able_seaman",
            shore_pass_eligible=True, shore_pass_valid_upto=datetime.now(UTC)+timedelta(hours=2))
        self.db.add(manifest); self.db.flush()
        self.assignment.crew_profile_id = self.profile.id
        self.assignment.vessel_crew_id = manifest.id
        self.assignment.shore_pass_eligible = True
        self.sp = ShorePass(crew_profile_id=self.profile.id, crew_assignment_id=self.assignment.id,
            vessel_call_id=self.call.id, shore_pass_id="SP-RACE", status="approved",
            expires_at=datetime.now(UTC)+timedelta(hours=1), is_verified=True)
        self.db.add(self.sp); self.db.commit()
        self.crew_actor = SimpleNamespace(id=user.id, role="crew")
        self.agent_actor = SimpleNamespace(id=self.agent.id, role="agent")

    def test_preloaded_pass_refresh_rejects_concurrent_agent_revocation(self):
        sp_id, assignment_id = self.sp.id, self.assignment.id
        prelock, rejected = Event(), Event()
        original = lock_shore_leave_context
        def delayed(db, selected_assignment):
            if db.info.get("crew_verify"):
                prelock.set()
                self.assertTrue(rejected.wait(10))
            return original(db, selected_assignment)
        def crew_verify():
            with SessionLocal() as db:
                db.info["crew_verify"] = True
                stale = db.get(ShorePass, sp_id)
                self.assertEqual(stale.status, "approved")
                with self.assertRaises(HTTPException) as denied:
                    verify_shorepass(sp_id, db=db, current_user=self.crew_actor)
                return denied.exception.status_code
        def revoke():
            self.assertTrue(prelock.wait(10))
            with SessionLocal() as db:
                reject_shore_pass(sp_id, ShorePassActionIn(rejection_reason="Revoked"), db=db, current_user=self.agent_actor)
            rejected.set()
        with patch("app.services.shore_leave_eligibility.lock_shore_leave_context", side_effect=delayed):
            with ThreadPoolExecutor(max_workers=2) as pool:
                crew_result = pool.submit(crew_verify)
                agent_result = pool.submit(revoke)
                self.assertEqual(crew_result.result(timeout=20), 409)
                agent_result.result(timeout=20)
        # This session still has its originally approved instance loaded.
        self.assertEqual(self.sp.status, "approved")
        with self.assertRaises(HTTPException):
            _require_booking_entitlement(self.db, self.assignment, refresh=True)
        self.db.refresh(self.sp)
        self.assertEqual(self.sp.status, "rejected")

    def test_preloaded_pass_expiry_update_is_not_hidden_by_identity_map(self):
        self.assertTrue(shore_leave_decision(self.db, self.assignment).eligible)
        with SessionLocal() as other:
            other.execute(update(ShorePass).where(ShorePass.id == self.sp.id).values(expires_at=datetime.now(UTC)-timedelta(seconds=1)))
            other.commit()
        with self.assertRaises(HTTPException):
            _require_booking_entitlement(self.db, self.assignment, refresh=True)
