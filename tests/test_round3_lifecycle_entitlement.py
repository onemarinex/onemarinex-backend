"""Lifecycle/booking regressions; run only in the isolated local fixture DB."""
from datetime import timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import ExitStack
from app.db.models.cab_booking import CabBooking
import unittest

from fastapi import HTTPException
from sqlalchemy.orm import Session

import test_shore_leave_status_card as fixture
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel_crew import VesselCrew
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.session import engine
from app.services.shore_leave_eligibility import shore_leave_decision
from app.services.historical_context import active_vessel_call, eligible_assignments_for_profile
from app.api.v1.routes_crew import _require_booking_entitlement, book_cab, CabBookingCreateIn, verify_shorepass


class LifecycleEntitlement(unittest.TestCase):
    setUp = fixture.ShoreLeaveStatusTests.setUp
    tearDown = fixture.ShoreLeaveStatusTests.tearDown
    vessel = fixture.ShoreLeaveStatusTests.vessel
    assign = fixture.ShoreLeaveStatusTests.assign
    card = fixture.ShoreLeaveStatusTests.card

    def member(self):
        vessel = self.vessel(agency_name=self.agency.agency_name)
        call = self.assign(vessel, eligible=True)
        assignment = self.db.query(CrewAssignment).filter_by(vessel_call_id=call.id).one()
        manifest = VesselCrew(vessel_id=vessel.id, name="Crew", rank="able_seaman",
            shore_pass_eligible=True, shore_pass_valid_upto=fixture.NOW + timedelta(hours=3))
        self.db.add(manifest); self.db.flush()
        assignment.vessel_crew = manifest
        self.db.flush()
        return vessel, call, assignment, manifest

    def body(self):
        return CabBookingCreateIn(pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0, vehicle_type="ac", vehicle_name="Sedan",
            estimated_price=100, distance_km=1, ride_type="flexible_ride")

    def test_valid_assignment_display_and_booking_boundary_agree(self):
        _, _, assignment, _ = self.member()
        self.assertEqual(self.card().shore_leave_status, "ELIGIBLE")
        self.assertTrue(_require_booking_entitlement(self.db, assignment).eligible)

    def test_false_boolean_denies_submission(self):
        _, _, assignment, _ = self.member(); assignment.shore_pass_eligible = False
        with self.assertRaises(HTTPException) as error:
            book_cab(self.body(), db=self.db, current_user=self.crew)
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("not authorized", error.exception.detail)

    def test_missing_validity_is_unknown_not_eligible(self):
        vessel, _, assignment, manifest = self.member()
        manifest.shore_pass_valid_upto = None; vessel.shore_pass_valid_upto = None
        self.assertEqual(shore_leave_decision(self.db, assignment).status, "VALIDITY_UNKNOWN")
        self.assertTrue(self.card().cab_booking_locked)

    def test_expired_managed_assignment_does_not_fall_back_to_selected_other_vessel(self):
        _, _, assignment, manifest = self.member()
        manifest.shore_pass_valid_upto = fixture.NOW-timedelta(days=1)
        other = self.vessel(agency_name="Other")
        self.profile.selected_vessel_id = other.id
        with self.assertRaises(HTTPException):
            book_cab(self.body(), db=self.db, current_user=self.crew)
        self.assertEqual(self.card().shore_leave_status, "EXPIRED")

    def test_individual_override_wins_over_master(self):
        vessel, _, assignment, manifest = self.member()
        vessel.shore_pass_valid_upto = fixture.NOW - timedelta(days=1)
        self.assertTrue(shore_leave_decision(self.db, assignment).eligible)
        vessel.shore_pass_valid_upto = fixture.NOW + timedelta(days=3)
        manifest.shore_pass_valid_upto = fixture.NOW - timedelta(seconds=1)
        self.assertEqual(shore_leave_decision(self.db, assignment).status, "EXPIRED")

    def test_pass_expiry_clamps_manifest_and_verify_cannot_extend(self):
        _, call, assignment, _ = self.member()
        shore_pass = ShorePass(crew_profile_id=self.profile.id, crew_assignment_id=assignment.id,
            vessel_call_id=call.id, shore_pass_id=fixture._uniq("SP"),
            expires_at=fixture.NOW - timedelta(seconds=1), status="approved")
        self.db.add(shore_pass); self.db.flush()
        self.assertEqual(shore_leave_decision(self.db, assignment).status, "EXPIRED")
        with self.assertRaises(HTTPException):
            verify_shorepass(shore_pass.id, db=self.db, current_user=self.crew)

    def test_stale_active_past_etd_denies_and_does_not_create_call(self):
        vessel, call, assignment, _ = self.member()
        vessel.etd = call.etd = fixture.NOW - timedelta(hours=1)
        self.assertIsNone(active_vessel_call(self.db, vessel))
        self.assertEqual(eligible_assignments_for_profile(self.db, self.profile), [])
        self.assertEqual(self.card().shore_leave_status, "NO_ACTIVE_CALL")
        self.assertEqual(self.card().vessel_context, "last_assigned")
        self.assertIsNone(assignment.signed_off_at)
        with self.assertRaises(HTTPException):
            book_cab(self.body(), db=self.db, current_user=self.crew)

    def test_ended_call_denies_even_with_stale_eligible_assignment(self):
        _, call, assignment, _ = self.member(); call.ended_at = fixture.NOW
        self.assertEqual(shore_leave_decision(self.db, assignment).status, "NO_ACTIVE_CALL")

    def test_exact_ist_midnight_and_naive_utc_boundary(self):
        _, call, assignment, manifest = self.member()
        expiry = fixture.NOW.astimezone(timezone(timedelta(hours=5, minutes=30))).replace(hour=0, minute=0, second=0, microsecond=0)
        call.started_at = assignment.started_at = expiry - timedelta(days=1)
        manifest.shore_pass_valid_upto = expiry
        self.assertTrue(shore_leave_decision(self.db, assignment, now=expiry-timedelta(microseconds=1)).eligible)
        self.assertEqual(shore_leave_decision(self.db, assignment, now=expiry).status, "EXPIRED")
        manifest.shore_pass_valid_upto = expiry.astimezone(timezone.utc).replace(tzinfo=None)
        self.assertEqual(shore_leave_decision(self.db, assignment, now=expiry).status, "EXPIRED")

    def test_submission_revalidates_after_expiry_transition(self):
        _, _, assignment, manifest = self.member()
        self.assertTrue(_require_booking_entitlement(self.db, assignment).eligible)
        manifest.shore_pass_valid_upto = fixture.NOW - timedelta(seconds=1)
        self.db.flush()
        with self.assertRaises(HTTPException):
            _require_booking_entitlement(self.db, assignment, refresh=True)

    def test_unmanaged_source_requires_unmanaged_effective_call(self):
        vessel, call, assignment, _ = self.member()
        assignment.source = "SELF_SELECTED_UNMANAGED"
        self.assertFalse(shore_leave_decision(self.db, assignment).eligible)

        call.agency_name = vessel.agency_name = "Other"
        self.assertEqual(shore_leave_decision(self.db, assignment).status, "NOT_REQUIRED_UNMANAGED")
        call.ended_at = fixture.NOW
        self.assertFalse(shore_leave_decision(self.db, assignment).eligible)

    def test_ended_stale_active_without_etd_cannot_reopen_from_report(self):
        vessel, call, _, _ = self.member()
        call.ended_at = fixture.NOW; call.status = "DEPARTED"
        vessel.etd = None; call.etd = None
        self.db.flush()
        self.assertIsNone(active_vessel_call(self.db, vessel))
        from app.api.v1.routes_agents import _resolve_report_call
        agent = SimpleNamespace(id=self.agent_user.id, role="agent")
        with self.assertRaises(HTTPException):
            _resolve_report_call(self.db, agent, vessel.id, None)
        from app.api.v1.routes_vessels import vessel_out
        self.assertEqual(vessel_out(vessel).status, "Departed")

    def test_explicit_return_resets_validity_and_does_not_inherit_roster(self):
        vessel, old_call, old_assignment, _ = self.member()
        vessel.etd = old_call.etd = fixture.NOW - timedelta(days=1)
        vessel.shore_pass_valid_upto = fixture.NOW + timedelta(days=10)
        self.db.flush()
        from app.api.v1.routes_vessels import _start_return_call, VesselIn
        body = VesselIn(name=vessel.name, imo_number=vessel.imo_number,
            vessel_type=vessel.vessel_type, eta=fixture.NOW-timedelta(hours=1),
            etd=fixture.NOW+timedelta(days=2))
        _start_return_call(self.db, vessel, body, agent_id=self.agent_user.id, agency_name=self.agency.agency_name)
        fresh = active_vessel_call(self.db, vessel, create=False)
        self.assertNotEqual(fresh.id, old_call.id)
        self.assertIsNone(vessel.shore_pass_valid_upto)
        self.assertEqual(self.db.query(CrewAssignment).filter_by(vessel_call_id=fresh.id).count(), 0)
        self.assertIsNone(old_assignment.signed_off_at)

    def test_agency_mapping_keeps_historical_call_after_reassignment(self):
        vessel, old_call, _, _ = self.member()
        old_call.ended_at = fixture.NOW
        old_call.agency_name = self.agency.agency_name
        other = User(email=fixture._uniq("otheragent")+"@example.com", hashed_password="x", role="agent")
        self.db.add(other); self.db.flush()
        vessel.agent_id = other.id
        self.db.flush()
        from app.api.v1.routes_superadmin import list_agent_vessels_superadmin
        rows = list_agent_vessels_superadmin(self.agent_user.id, db=self.db,
            current_user=SimpleNamespace(role="superadmin"))
        mapped = next(row for row in rows if row.id == vessel.id)
        self.assertEqual(mapped.agency_mapping_context, "historical")
        self.assertEqual(mapped.agency_mapping_call_id, old_call.id)
        self.assertEqual(mapped.status, "Departed")

    def booking_stubs(self, fare=None):
        stack = ExitStack()
        stack.enter_context(patch("app.api.v1.routes_crew._pickup_availability", return_value={"available": True, "pickup_at": fixture.NOW}))
        stack.enter_context(patch("app.services.booking_service.is_ride_type_available", return_value=True))
        stack.enter_context(patch("app.api.v1.routes_crew.get_eligible_providers_for_ride", return_value=[SimpleNamespace(id=1, user=None)]))
        stack.enter_context(patch("app.api.v1.routes_crew._resolve_server_side_fare", side_effect=fare or (lambda *a, **kw: (100, 1))))
        for function in ("notify_crew_package_trip_state", "notify_crew_coordinated_transfer_trip_state", "notify_aggregator_trip_request", "notify_aggregator_coordinated_transfer_trip_request"):
            stack.enter_context(patch("app.services.whatsapp." + function))
        self.crew.mobile_number = None
        return stack

    def test_real_booking_route_valid_control_and_idempotent_retry(self):
        _, call, assignment, manifest = self.member()
        body = self.body(); body.idempotency_key = "round3-valid-booking"
        with self.booking_stubs():
            booked = book_cab(body, db=self.db, current_user=self.crew)
            saved = self.db.query(CabBooking).filter_by(booking_id=booked.booking_id).one()
            self.assertEqual(saved.crew_assignment_id, assignment.id)
            self.assertEqual(saved.vessel_call_id, call.id)
            manifest.shore_pass_valid_upto = fixture.NOW-timedelta(days=1); self.db.flush()
            retry = book_cab(body, db=self.db, current_user=self.crew)
            self.assertEqual(retry.booking_id, booked.booking_id)

    def test_real_booking_route_rejects_expiry_during_provider_lookup(self):
        _, _, _, manifest = self.member()
        def expire(*args, **kwargs):
            manifest.shore_pass_valid_upto = fixture.NOW-timedelta(seconds=1)
            self.db.flush()
            return 100, 1
        before = self.db.query(CabBooking).count()
        with self.booking_stubs(fare=expire):
            with self.assertRaises(HTTPException) as denied:
                book_cab(self.body(), db=self.db, current_user=self.crew)
        self.assertEqual(denied.exception.status_code, 409)
        self.assertEqual(self.db.query(CabBooking).count(), before)
