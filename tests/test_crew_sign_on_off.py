"""The agency transfer screen — HPID spec §8, scenarios H18 to H20.

    H18  Sign-on transfer before manifest → create call assignment + random
         HPID without account
    H19  Sign-off transfer → reuse existing call assignment and HPID
    H20  Sign-on crew later appears in manifest → resolve existing assignment;
         no duplicate HPID

H20 is the one worth the care. A person can reach a call from two directions
and both must land on one assignment with one reference, whichever order they
arrive in.
"""

from datetime import datetime, timedelta, timezone
import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services import assignment_source, crew_movement
from app.services.assignment_ref import looks_like_assignment_ref
from app.services.historical_context import assignment_for_manifest

NOW = datetime.now(timezone.utc)


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class SignOnOffTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent_user = User(email=_uniq("agent") + "@x.com", hashed_password="x",
                          role="agent")
        self.db.add(agent_user)
        self.db.flush()
        self.agent = agent_user
        self.db.add(AgentProfile(
            user_id=agent_user.id, agency_name=_uniq("Praveen Shipping"),
            location="Port", assigned_port="port_test"))
        self.vessel = Vessel(
            agent_id=agent_user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
            eta=NOW - timedelta(days=1), etd=NOW + timedelta(days=2))
        self.db.add(self.vessel)
        self.db.flush()
        self.passport = _uniq("P").replace("-", "").upper()[:12]

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def _sign_on(self, **overrides):
        kwargs = dict(
            vessel=self.vessel, crew_name="Joining Crew",
            passport_number=self.passport, nationality="Indian",
            rank="oiler", actor=self.agent,
        )
        kwargs.update(overrides)
        return crew_movement.sign_on(self.db, **kwargs)

    def _manifest(self, *, eligible=True):
        row = VesselCrew(
            vessel_id=self.vessel.id, name="Joining Crew", rank="oiler",
            hp_id=_uniq("HP"), passport_number=self.passport,
            nationality="IN", shore_pass_eligible=eligible,
        )
        self.db.add(row)
        self.db.flush()
        return row

    # --- H18 ------------------------------------------------------------------

    def test_h18_sign_on_needs_no_manifest_and_no_account(self):
        assignment = self._sign_on()
        self.assertIsNone(assignment.vessel_crew_id, "no manifest row")
        self.assertIsNone(assignment.crew_profile_id, "no account")
        self.assertEqual(assignment.source, assignment_source.SIGN_ON)
        self.assertTrue(looks_like_assignment_ref(assignment.public_ref))
        self.assertIsNotNone(assignment.signed_on_at)

    def test_sign_on_normalises_the_nationality_it_is_given(self):
        self.assertEqual(self._sign_on().nationality, "IN")

    def test_sign_on_without_a_passport_is_refused(self):
        with self.assertRaises(crew_movement.MovementError) as caught:
            self._sign_on(passport_number="   ")
        self.assertIn("passport number is required", str(caught.exception))

    def test_signing_the_same_person_on_twice_issues_one_reference(self):
        first = self._sign_on()
        second = self._sign_on()
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.public_ref, second.public_ref)

    # --- H19 ------------------------------------------------------------------

    def test_h19_sign_off_keeps_the_reference_and_ends_the_assignment(self):
        assignment = self._sign_on()
        reference = assignment.public_ref
        crew_movement.sign_off(self.db, assignment=assignment, actor=self.agent)
        self.assertEqual(assignment.public_ref, reference, "§5.1: history keeps it")
        self.assertIsNotNone(assignment.ended_at)
        self.assertIsNotNone(assignment.signed_off_at)

    def test_signing_off_twice_is_refused(self):
        assignment = self._sign_on()
        crew_movement.sign_off(self.db, assignment=assignment, actor=self.agent)
        with self.assertRaises(crew_movement.MovementError):
            crew_movement.sign_off(self.db, assignment=assignment, actor=self.agent)

    def test_signing_on_again_after_sign_off_starts_a_new_assignment(self):
        """The old one is closed, so this person is genuinely rejoining."""
        first = self._sign_on()
        crew_movement.sign_off(self.db, assignment=first, actor=self.agent)
        second = self._sign_on()
        self.assertNotEqual(first.id, second.id)
        self.assertNotEqual(first.public_ref, second.public_ref)

    # --- H20 ------------------------------------------------------------------

    def test_h20_a_later_manifest_row_adopts_the_sign_on_assignment(self):
        signed_on = self._sign_on()
        reference = signed_on.public_ref

        manifest = self._manifest()
        adopted = assignment_for_manifest(self.db, self.vessel, manifest)

        self.assertEqual(adopted.id, signed_on.id, "no duplicate assignment")
        self.assertEqual(adopted.public_ref, reference, "no duplicate reference")
        self.assertEqual(adopted.vessel_crew_id, manifest.id)
        # The manifest is the agent's word on eligibility; the sign-on had none.
        self.assertTrue(adopted.shore_pass_eligible)

    def test_h20_holds_in_the_other_order_too(self):
        """Manifest first, then a sign-on for the same person."""
        manifest = self._manifest()
        from_manifest = assignment_for_manifest(self.db, self.vessel, manifest)
        signed_on = self._sign_on()
        self.assertEqual(signed_on.id, from_manifest.id)
        self.assertEqual(signed_on.public_ref, from_manifest.public_ref)

    def test_one_person_on_one_call_has_exactly_one_assignment(self):
        self._sign_on()
        manifest = self._manifest()
        assignment_for_manifest(self.db, self.vessel, manifest)
        self._sign_on()
        call_id = self.db.query(CrewAssignment.vessel_call_id).filter(
            CrewAssignment.passport_number == self.passport).first()[0]
        self.assertEqual(
            self.db.query(CrewAssignment).filter(
                CrewAssignment.vessel_call_id == call_id,
                CrewAssignment.passport_number == self.passport,
            ).count(),
            1,
        )

    def test_a_different_persons_manifest_row_never_adopts_this_one(self):
        self._sign_on()
        other = VesselCrew(
            vessel_id=self.vessel.id, name="Someone Else", rank="cook",
            hp_id=_uniq("HP"), passport_number=_uniq("Q").replace("-", "")[:12],
            nationality="IN",
        )
        self.db.add(other)
        self.db.flush()
        theirs = assignment_for_manifest(self.db, self.vessel, other)
        self.assertNotEqual(theirs.passport_number, self.passport)

    # --- a trip is not a movement ---------------------------------------------

    def test_nothing_in_the_booking_flow_confirms_a_movement(self):
        """§8: "Trip booked/completed is NOT automatically confirmed sign-on/off."

        By inspection, because a booking that wrongly set these would still
        pass every other test here.
        """
        import inspect as _inspect

        from app.api.v1 import routes_crew

        source = _inspect.getsource(routes_crew)
        for field in ("signed_on_at", "signed_off_at"):
            self.assertNotIn(
                field, source,
                f"routes_crew must not set {field}: an agent confirms a "
                "movement, a cab ride does not",
            )
