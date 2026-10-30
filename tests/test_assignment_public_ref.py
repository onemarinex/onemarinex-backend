"""The opaque per-call public reference.

The legacy HPID is HP-{passport}-{NAT}-{PORT}: it carries a passport number to
everyone who receives it, and it is the same value for the same person on every
voyage, so it links their port calls together. The replacement must be the
opposite on both counts.
"""
import unittest
import uuid

import app.db.base  # noqa: F401
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.assignment_ref import (
    PREFIX, generate_assignment_ref, issue_assignment_ref,
    looks_like_assignment_ref, resolve_crew_identifier,
)
from app.services.historical_context import assignment_for_manifest


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class GeneratorTests(unittest.TestCase):
    def test_it_reveals_nothing_about_the_person(self):
        passport = "P1234567"
        refs = [generate_assignment_ref() for _ in range(200)]
        for ref in refs:
            self.assertNotIn(passport, ref)
            self.assertNotIn("IND", ref.replace(PREFIX, ""))

    def test_the_same_inputs_never_give_the_same_output(self):
        """Unlike generate_hpid, which is a pure function of the person."""
        self.assertEqual(len({generate_assignment_ref() for _ in range(2000)}), 2000)

    def test_it_avoids_characters_that_are_read_wrongly(self):
        body = generate_assignment_ref().split("-", 1)[1]
        for confusable in "ILOU":
            self.assertNotIn(confusable, body)

    def test_it_carries_the_prefix_the_specification_names(self):
        self.assertTrue(generate_assignment_ref().startswith("HP-"))

    def test_a_legacy_hpid_is_not_mistaken_for_one(self):
        """Even sharing the prefix, which the owner chose on 26 Oct 2026.

        The shape does the work, not the prefix: a legacy identifier is
        HP-{passport}-{NAT}-{PORT}, so its body always carries further
        hyphens, and a hyphen is not in the alphabet. Length alone would not
        be enough, hence the long-passport case below.
        """
        self.assertFalse(looks_like_assignment_ref("HP-P1234567-IND-MUM"))
        self.assertFalse(looks_like_assignment_ref("HP-AB1234567890123456-IN-VIS"))
        self.assertFalse(looks_like_assignment_ref(""))
        self.assertFalse(looks_like_assignment_ref("HP-too-short"))
        self.assertTrue(looks_like_assignment_ref(generate_assignment_ref()))



class IssuedOnAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent = User(email=_uniq("agent") + "@e.com", hashed_password="x", role="agent")
        crew_user = User(email=_uniq("crew") + "@e.com", hashed_password="x", role="crew")
        self.db.add_all([agent, crew_user])
        self.db.flush()
        self.profile = CrewProfile(
            user_id=crew_user.id, full_name="Repeat Sailor", rank="able_seaman",
            nationality="IN", hpid=_uniq("HP"),
        )
        self.vessel = Vessel(
            agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier",
        )
        self.db.add_all([self.profile, self.vessel])
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def _end_the_current_call(self):
        """Finish the open call so the next assignment belongs to a new one."""
        from datetime import datetime, timezone
        call = (
            self.db.query(VesselCall)
            .filter(VesselCall.vessel_id == self.vessel.id,
                    VesselCall.ended_at.is_(None))
            .one()
        )
        call.ended_at = datetime.now(timezone.utc)
        self.db.flush()

    def _manifest(self):
        manifest = VesselCrew(
            vessel_id=self.vessel.id, name="Repeat Sailor", rank="able_seaman",
            hp_id=self.profile.hpid, passport_number="P7654321",
        )
        self.db.add(manifest)
        self.db.flush()
        return manifest

    def test_a_new_assignment_is_issued_one(self):
        assignment = assignment_for_manifest(
            self.db, self.vessel, self._manifest(), create=True,
        )
        self.assertTrue(looks_like_assignment_ref(assignment.public_ref))

    def test_it_does_not_contain_the_passport_the_legacy_hpid_carries(self):
        manifest = self._manifest()
        manifest.hp_id = "HP-P7654321-IND-VIZ"
        self.db.flush()
        assignment = assignment_for_manifest(
            self.db, self.vessel, manifest, create=True,
        )
        # The legacy identifier it is issued alongside does carry the passport.
        self.assertIn("P7654321", assignment.hpid)
        self.assertNotIn("P7654321", assignment.public_ref)

    def test_the_same_person_on_a_later_call_gets_a_different_one(self):
        """The property the legacy identifier could never have."""
        # One manifest row, two calls: the manifest survives a port call, which
        # is exactly why the legacy identifier on it is the same both times.
        manifest = self._manifest()
        first = assignment_for_manifest(
            self.db, self.vessel, manifest, create=True,
        )
        first_ref, first_hpid = first.public_ref, first.hpid
        self._end_the_current_call()
        from app.services.historical_context import active_vessel_call
        active_vessel_call(self.db, self.vessel, allow_new_call=True)
        second = assignment_for_manifest(
            self.db, self.vessel, manifest, create=True,
        )

        self.assertNotEqual(first.id, second.id, "expected a second assignment")

        self.assertNotEqual(first_ref, second.public_ref)
        # ...while the legacy identifier is the same on both, which is the
        # linkage this replaces.
        self.assertEqual(first_hpid, second.hpid)

    def test_asking_twice_for_one_assignment_does_not_reissue(self):
        manifest = self._manifest()
        first = assignment_for_manifest(
            self.db, self.vessel, manifest, create=True,
        )
        again = assignment_for_manifest(
            self.db, self.vessel, manifest, create=True,
        )
        self.assertEqual(first.id, again.id)
        self.assertEqual(first.public_ref, again.public_ref)

    def test_the_database_refuses_a_duplicate(self):
        taken = issue_assignment_ref(self.db)
        call = VesselCall(vessel_id=self.vessel.id, vessel_name=self.vessel.name)
        self.db.add(call)
        self.db.flush()
        for _ in range(2):
            self.db.add(CrewAssignment(
                vessel_call_id=call.id, crew_name="Clash", rank="cook",
                public_ref=taken,
            ))
        with self.assertRaises(IntegrityError):
            self.db.flush()




class DriverPayloadTests(unittest.TestCase):
    """What a trip-link holder receives.

    Step 1 removed the passport-bearing hp_id from this payload and put nothing
    back, because nothing safe existed yet. These pin what replaced it.
    """

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def _payload(self, *, with_assignment=True):
        from types import SimpleNamespace as NS
        from app.services.magic_link_service import (
            serialize_magic_link_public_payload,
        )

        assignment = (
            NS(public_ref="HP-9FKAS2QN1H98902EDE2RQ3A30P")
            if with_assignment else None
        )
        booking = NS(
            id=1, booking_id="SYN-1", status="on_trip",
            crew=NS(full_name="Synthetic Crew", hpid="HP-P1234567-IND-VIZ"),
            crew_assignment=assignment,
            vessel_call=NS(vessel_name="MV Synthetic"),
            estimated_price=None, distance_km=None, vehicle_type=None,
            vehicle_name=None, ride_type=None, num_passengers=1,
            aggregator_name="syn", port="syn", driver_name="d",
            driver_phone="p", driver_plate="v", pickup_address="a",
            pickup_lat=1, pickup_lng=2, drop_address="b", drop_lat=3, drop_lng=4,
        )
        link = NS(
            booking=booking, token="synthetic-token", otp_verified_at=None,
            itinerary_stops=[], reach_events=[],
            created_at=__import__("datetime").datetime(2026, 1, 1),
        )
        return serialize_magic_link_public_payload(link)

    def test_the_passport_bearing_identifier_is_absent(self):
        payload = self._payload()
        self.assertNotIn("hp_id", payload["crew"])
        self.assertNotIn("P1234567", repr(payload))

    def test_the_opaque_reference_is_what_the_driver_sees(self):
        payload = self._payload()
        self.assertEqual(
            payload["crew"]["reference"], "HP-9FKAS2QN1H98902EDE2RQ3A30P"
        )
        self.assertTrue(looks_like_assignment_ref(payload["crew"]["reference"]))

    def test_a_booking_with_no_assignment_simply_has_none(self):
        """An unmanaged-vessel booking has no assignment; it must not break."""
        payload = self._payload(with_assignment=False)
        self.assertIsNone(payload["crew"]["reference"])


class ResolutionTests(unittest.TestCase):
    """Accepting the new identifier must not become a way around the old checks.

    resolve_crew_identifier only translates. Every caller keeps the scoping it
    already had, so presenting another agency's reference reaches that agency's
    filter and is refused there.
    """

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.mine = User(email=_uniq("mine") + "@e.com", hashed_password="x", role="agent")
        self.rival = User(email=_uniq("rival") + "@e.com", hashed_password="x", role="agent")
        self.db.add_all([self.mine, self.rival])
        self.db.flush()
        self.their_vessel = Vessel(
            agent_id=self.rival.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier",
        )
        self.db.add(self.their_vessel)
        self.db.flush()
        self.their_manifest = VesselCrew(
            vessel_id=self.their_vessel.id, name="Their Crew", rank="cook",
            hp_id=_uniq("HP"),
        )
        self.db.add(self.their_manifest)
        self.db.flush()
        call = VesselCall(
            vessel_id=self.their_vessel.id, vessel_name=self.their_vessel.name,
        )
        self.db.add(call)
        self.db.flush()
        self.their_assignment = CrewAssignment(
            vessel_call_id=call.id, vessel_crew_id=self.their_manifest.id,
            crew_name="Their Crew", rank="cook", hpid=self.their_manifest.hp_id,
            public_ref=issue_assignment_ref(self.db),
        )
        self.db.add(self.their_assignment)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def test_a_legacy_identifier_passes_through_untouched(self):
        from app.services.assignment_ref import resolve_crew_identifier
        self.assertEqual(
            resolve_crew_identifier(self.db, "HP-P1234567-IND-MUM"),
            "HP-P1234567-IND-MUM",
        )

    def test_a_reference_resolves_to_the_manifest_identifier(self):
        from app.services.assignment_ref import resolve_crew_identifier
        self.assertEqual(
            resolve_crew_identifier(self.db, self.their_assignment.public_ref),
            self.their_manifest.hp_id,
        )

    def test_an_unknown_reference_is_returned_unchanged(self):
        """So the caller's own not-found happens, rather than a different error
        that would confirm whether the reference exists."""
        from app.services.assignment_ref import resolve_crew_identifier
        unknown = generate_assignment_ref()
        self.assertEqual(resolve_crew_identifier(self.db, unknown), unknown)

    def test_another_agency_s_reference_still_gets_nothing(self):
        """The point of the whole class."""
        from fastapi import HTTPException
        from app.api.v1.routes_agents import get_agent_crew_detail

        with self.assertRaises(HTTPException) as refused:
            get_agent_crew_detail(
                hp_id=self.their_assignment.public_ref,
                current_user=self.mine,
                db=self.db,
            )
        self.assertEqual(refused.exception.status_code, 404)

    def test_the_owning_agent_can_use_it(self):
        from app.api.v1.routes_agents import get_agent_crew_detail
        detail = get_agent_crew_detail(
            hp_id=self.their_assignment.public_ref,
            current_user=self.rival,
            db=self.db,
        )
        self.assertEqual(detail.full_name, "Their Crew")
        self.assertEqual(detail.hpid, self.their_manifest.hp_id)


class NotYetBackfilledTests(unittest.TestCase):
    """An assignment that exists but has no reference yet.

    Until the migration was split, every row got a reference as the migration
    ran, so this state lasted microseconds. Now the migration writes nothing and
    `scripts/backfill_assignment_public_ref.py` is run separately, so a freshly
    migrated database has an assignment on every row and a reference on none —
    and stays that way until someone chooses to run it.

    It is also the state a stopped or partial backfill leaves behind. Every
    surface carrying the reference has to answer in it.
    """

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.vessel = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        self.db.flush()
        call = VesselCall(vessel_id=self.vessel.id, vessel_name=self.vessel.name)
        self.db.add(call)
        self.db.flush()
        self.assignment = CrewAssignment(
            vessel_call_id=call.id, crew_name="Unbackfilled Crew", rank="cook",
            hpid=_uniq("HP"), public_ref=None,
        )
        self.db.add(self.assignment)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def test_the_row_is_valid_with_no_reference(self):
        """The column is nullable and nothing requires it."""
        self.assertIsNone(self.assignment.public_ref)

    def test_the_unique_index_tolerates_a_whole_table_of_them(self):
        """Postgres treats NULLs as distinct, which is why the index can be
        created before the backfill runs."""
        for _ in range(5):
            self.db.add(CrewAssignment(
                vessel_call_id=self.assignment.vessel_call_id,
                crew_name="Another", rank="cook", public_ref=None,
            ))
        self.db.flush()   # must not raise

    def test_the_driver_payload_reports_no_reference_rather_than_breaking(self):
        from types import SimpleNamespace as NS
        from app.services.magic_link_service import (
            serialize_magic_link_public_payload,
        )

        booking = NS(
            id=1, booking_id="SYN-1", status="on_trip",
            crew=NS(full_name="Unbackfilled Crew", hpid="HP-P1234567-IND-VIZ"),
            crew_assignment=NS(public_ref=None),
            vessel_call=NS(vessel_name="MV Synthetic"),
            estimated_price=None, distance_km=None, vehicle_type=None,
            vehicle_name=None, ride_type=None, num_passengers=1,
            aggregator_name="syn", port="syn", driver_name="d",
            driver_phone="p", driver_plate="v", pickup_address="a",
            pickup_lat=1, pickup_lng=2, drop_address="b", drop_lat=3, drop_lng=4,
        )
        link = NS(
            booking=booking, token="synthetic-token", otp_verified_at=None,
            itinerary_stops=[], reach_events=[],
            created_at=__import__("datetime").datetime(2026, 1, 1),
        )

        payload = serialize_magic_link_public_payload(link)
        self.assertIsNone(payload["crew"]["reference"])
        # and still no passport, which was the point of removing hp_id
        self.assertNotIn("hp_id", payload["crew"])
        self.assertNotIn("P1234567", repr(payload))

    def test_resolution_falls_through_to_the_legacy_identifier(self):
        """A lookup must keep working before the backfill has run."""
        self.assertEqual(
            resolve_crew_identifier(self.db, self.assignment.hpid),
            self.assignment.hpid,
        )

if __name__ == "__main__":
    unittest.main()
