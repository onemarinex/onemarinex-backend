"""The HPID specification's own acceptance matrix, §14 — H01 to H30.

    "Use the 30 scenario tests as acceptance criteria."

So they are written out by name. The point of the matrix is the lifecycle
rule it keeps restating from three directions:

    SAME PERSON + SAME VESSEL CALL = SAME HPID
    SAME PERSON + NEW VESSEL CALL  = NEW HPID
    Logging in, logging out, refreshing or booking another cab NEVER creates
    a new HPID within the same call.

Scenarios covered elsewhere are named here with their home, so the matrix can
be read end to end without pretending this file is the only evidence:

    H06, H07  tests/test_assignment_public_ref.py   (manifest re-upload,
              and the partial unique indexes that make it concurrent-safe)
    H15       tests/test_manifest_identity_matching.py  (two matching
              profiles → no automatic merge)
    H18-H20   tests/test_crew_sign_on_off.py
    H21, H22  tests/test_unmanaged_vessel_bookings.py
    H23, H28  tests/test_assignment_public_ref.py   (driver payload)
    H26       tests/test_company_access_policy.py
    H27       tests/test_data_deletion.py
    H29       tests/test_shore_leave_status_card.py
    H30       tests/test_eligibility_and_ashore.py
"""

from datetime import datetime, timedelta, timezone
import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_context import (
    active_assignment_for_profile,
    active_vessel_call,
    assignment_for_manifest,
    finish_vessel_call,
)

NOW = datetime.now(timezone.utc)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class AcceptanceMatrixTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent_user = User(email=_uniq("agent") + "@x.com", hashed_password="x",
                          role="agent")
        crew_user = User(email=_uniq("crew") + "@x.com", hashed_password="x",
                         role="crew")
        self.db.add_all([agent_user, crew_user])
        self.db.flush()
        self.agent_user = agent_user
        self.db.add(AgentProfile(
            user_id=agent_user.id, agency_name=_uniq("Praveen Shipping"),
            location="Port", assigned_port="port_test"))

        self.passport = _uniq("P").replace("-", "").upper()[:12]
        self.profile = CrewProfile(
            user_id=crew_user.id, full_name="Ravi", rank="able_seaman",
            nationality="IN", hpid=_uniq("HP"), passport_number=self.passport)
        self.db.add(self.profile)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    # --- fixtures -------------------------------------------------------------

    def _vessel(self, name=None):
        vessel = Vessel(
            agent_id=self.agent_user.id, name=name or _uniq("MV"),
            imo_number=_uniq("IMO"), vessel_type="Bulk Carrier", status="Active",
            eta=NOW - timedelta(days=1), etd=NOW + timedelta(days=2))
        self.db.add(vessel)
        self.db.flush()
        return vessel

    def _manifest(self, vessel, *, eligible=True, passport=None, nationality="IN"):
        row = VesselCrew(
            vessel_id=vessel.id, name="Ravi", rank="able_seaman",
            hp_id=_uniq("HP"), passport_number=passport or self.passport,
            nationality=nationality, shore_pass_eligible=eligible)
        self.db.add(row)
        self.db.flush()
        return row

    def _join(self, vessel, **kwargs):
        """Put this crew member on the vessel's active call."""
        return assignment_for_manifest(
            self.db, vessel, self._manifest(vessel, **kwargs), profile=self.profile)

    def _reference_of(self, assignment_id):
        return self.db.query(CrewAssignment.public_ref).filter(
            CrewAssignment.id == assignment_id).one()[0]

    # --- H04, H05, H11 — nothing within one call rotates it -------------------

    def test_h04_logging_in_twenty_times_resolves_to_one_assignment(self):
        vessel = self._vessel()
        joined = self._join(vessel)
        seen = {
            active_assignment_for_profile(self.db, self.profile).public_ref
            for _ in range(20)
        }
        self.assertEqual(seen, {joined.public_ref})

    def test_h05_reinstalling_or_changing_phone_changes_nothing(self):
        """There is nothing device-side to change: the reference lives on the
        assignment, so an authenticated session resolves to the same row."""
        vessel = self._vessel()
        joined = self._join(vessel)
        reference = joined.public_ref
        self.db.expire_all()  # a fresh session, as a reinstall would be
        self.assertEqual(
            active_assignment_for_profile(self.db, self.profile).public_ref,
            reference)

    def test_h11_three_bookings_in_one_call_reference_one_assignment(self):
        vessel = self._vessel()
        joined = self._join(vessel)
        resolved = [
            active_assignment_for_profile(self.db, self.profile).id
            for _ in range(3)
        ]
        self.assertEqual(set(resolved), {joined.id})
        self.assertEqual(self._reference_of(joined.id), joined.public_ref)

    # --- H12 — eligibility moves, the reference does not ----------------------

    def test_h12_toggling_eligibility_keeps_the_reference(self):
        vessel = self._vessel()
        joined = self._join(vessel, eligible=True)
        reference = joined.public_ref
        self.assertTrue(joined.shore_pass_eligible)

        joined.shore_pass_eligible = False
        self.db.flush()
        self.assertFalse(joined.shore_pass_eligible)
        self.assertEqual(joined.public_ref, reference)

    # --- H13, H14 — identity corrections keep the call's reference ------------

    def test_h13_a_passport_spelling_correction_keeps_the_reference(self):
        vessel = self._vessel()
        joined = self._join(vessel)
        reference = joined.public_ref

        # Same number, respaced -- normalisation already treats these as one.
        self.profile.passport_number = f" {self.passport[:4]} {self.passport[4:]} "
        self.db.flush()
        self.assertEqual(self._reference_of(joined.id), reference)

    def test_h14_a_passport_renewal_keeps_the_reference_for_this_call(self):
        """§5: "Do not regenerate already-issued HPID" on renewal."""
        vessel = self._vessel()
        joined = self._join(vessel)
        reference = joined.public_ref

        self.profile.passport_number = _uniq("NEW").replace("-", "").upper()[:12]
        self.db.flush()
        self.assertEqual(self._reference_of(joined.id), reference)

    # --- H08, H09, H10 — a new call is a new reference ------------------------

    def _close_call(self, vessel):
        """The vessel departs: the call ends and its assignments end with it."""
        finish_vessel_call(self.db, vessel)
        self.db.flush()

    def test_h08_the_same_ship_returning_to_the_same_port_is_a_new_reference(self):
        vessel = self._vessel()
        first = self._join(vessel)
        first_reference = first.public_ref
        self._close_call(vessel)
        # Model an explicit authorized new call, not a manifest-triggered reopen.
        active_vessel_call(self.db, vessel, allow_new_call=True)

        second = self._join(vessel)
        self.assertNotEqual(second.id, first.id)
        self.assertNotEqual(second.public_ref, first_reference)
        # §5.1: the old one survives as history rather than being reissued.
        self.assertEqual(self._reference_of(first.id), first_reference)

    def test_h09_the_same_ship_at_a_different_port_is_a_new_reference(self):
        vessel = self._vessel()
        first = self._join(vessel)
        self._close_call(vessel)

        later = VesselCall(
            vessel_id=vessel.id, vessel_name=vessel.name,
            imo_number=vessel.imo_number, port_name="port_krishnapatnam",
            status="ACTIVE", started_at=NOW, ended_at=None)
        self.db.add(later)
        self.db.flush()
        second = self._join(vessel)

        self.assertNotEqual(second.public_ref, first.public_ref)
        self.assertNotEqual(second.vessel_call_id, first.vessel_call_id)

    def test_h10_the_same_person_on_another_vessel_is_a_new_reference(self):
        first = self._join(self._vessel())
        second = self._join(self._vessel())
        self.assertNotEqual(second.id, first.id)
        self.assertNotEqual(second.public_ref, first.public_ref)

    # --- H16 — names are not identity -----------------------------------------

    def test_h16_two_ships_with_the_same_name_stay_separate(self):
        shared = _uniq("MV DUPLICATE")
        one = self._join(self._vessel(name=shared))
        two = self._join(self._vessel(name=shared))
        self.assertNotEqual(one.vessel_call_id, two.vessel_call_id)
        self.assertNotEqual(one.public_ref, two.public_ref)

    # --- H24 — a closed call grants nothing -----------------------------------

    def test_h24_a_closed_assignment_is_history_and_not_a_booking_context(self):
        vessel = self._vessel()
        joined = self._join(vessel)
        reference = joined.public_ref
        self._close_call(vessel)
        self.db.refresh(joined)

        self.assertIsNotNone(joined.ended_at, "the call closed the assignment")
        self.assertIsNone(
            active_assignment_for_profile(self.db, self.profile),
            "a closed assignment is not an active booking context")
        self.assertEqual(self._reference_of(joined.id), reference,
                         "§5.1: the identifier stays as a historical reference")

    # --- H17 — ambiguity is refused, not resolved -----------------------------

    def test_h17_two_live_calls_need_an_explicit_choice(self):
        first = self._join(self._vessel())
        second = self._join(self._vessel())
        self.assertIsNone(
            active_assignment_for_profile(self.db, self.profile),
            "no implicit newest")
        for assignment in (first, second):
            self.assertEqual(
                active_assignment_for_profile(
                    self.db, self.profile,
                    vessel_call_id=assignment.vessel_call_id).id,
                assignment.id)


class DuplicateManifestRowTests(AcceptanceMatrixTests):
    """A manifest naming the same person twice — §10, found by H08.

    An agent who adds a crew member again without removing the old entry
    leaves two rows that resolve to one person. Opening a new call
    materialises an assignment for every manifest row, and the second one
    wanted the same (call, profile) as the first. The partial unique index
    refused it, the refusal aborted the surrounding transaction, and the
    vessel could not open a call at all: one stale row made the ship unusable.
    """

    def test_a_second_row_for_one_person_resolves_to_one_assignment(self):
        vessel = self._vessel()
        first = self._join(vessel)
        second = assignment_for_manifest(
            self.db, vessel, self._manifest(vessel), profile=self.profile)
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.public_ref, first.public_ref)

    def test_the_existing_row_keeps_the_manifest_entry_it_was_built_from(self):
        vessel = self._vessel()
        first = self._join(vessel)
        original = first.vessel_crew_id
        assignment_for_manifest(
            self.db, vessel, self._manifest(vessel), profile=self.profile)
        self.db.refresh(first)
        self.assertEqual(first.vessel_crew_id, original)

    def test_a_stale_duplicate_no_longer_stops_the_vessel_opening_a_call(self):
        """The crash this guards. Two rows, then the ship departs and returns."""
        vessel = self._vessel()
        self._join(vessel)
        self._manifest(vessel)  # the duplicate the agent left behind
        self._close_call(vessel)
        active_vessel_call(self.db, vessel, allow_new_call=True)

        reopened = self._join(vessel)
        self.assertIsNotNone(reopened)
        self.assertIsNone(reopened.ended_at)
