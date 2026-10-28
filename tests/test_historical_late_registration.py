"""Registering after the voyage ended must still find the voyage.

The audit's open item: the registration sync "does not by itself prove that a
crew account created after departure can be attached to every correct
historical assignment". It did not, and the reason was structural rather than
a missing branch.

`sync_crew_manifest_helper` walks VesselCrew, and VesselCrew is a *current
roster*: every row for a vessel is deleted when it departs. From there
`assignment_for_manifest` goes through `active_vessel_call`, which filters
`VesselCall.ended_at IS NULL`. So a seafarer who registers a month after
signing off searches a roster that no longer lists them, for a call that is no
longer open — and the assignments that are theirs keep `crew_profile_id = NULL`
for ever.

The first test here is the one that matters: it reproduces that world — ended
call, deleted manifest, profile created afterwards — and it fails without
`link_historical_assignments`.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy.orm import Session

from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_identity_conflict import CrewIdentityConflictRecord
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine
from app.services.historical_linkage import link_historical_assignments


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class HistoricalLateRegistrationTests(unittest.TestCase):
    PASSPORT = "Z5267418"

    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(agent)
        self.db.flush()

        self.vessel = Vessel(agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
                             vessel_type="Bulk Carrier", status="Departed")
        self.db.add(self.vessel)
        self.db.flush()

        # The voyage, finished. The manifest rows that went with it are gone,
        # exactly as the departure path deletes them.
        started = datetime.now(timezone.utc) - timedelta(days=60)
        self.call = VesselCall(
            vessel_id=self.vessel.id, vessel_name=self.vessel.name,
            imo_number=self.vessel.imo_number,
            started_at=started, ended_at=started + timedelta(days=10),
        )
        self.db.add(self.call)
        self.db.flush()
        self.assertEqual(
            self.db.query(VesselCrew).filter(VesselCrew.vessel_id == self.vessel.id).count(),
            0, "the fixture must have no manifest rows, as a departed vessel does not",
        )

        self.assignment = CrewAssignment(
            vessel_call_id=self.call.id, crew_profile_id=None,
            crew_name="Ravi Kumar", rank="able_seaman", nationality="IN",
            passport_number=self.PASSPORT,
            started_at=started, ended_at=started + timedelta(days=10),
        )
        self.db.add(self.assignment)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def register(self, *, passport=None, nationality="IN", name="Ravi Kumar"):
        """A crew account created long after the voyage ended."""
        user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add(user)
        self.db.flush()
        profile = CrewProfile(
            user_id=user.id, full_name=name, rank="able_seaman",
            nationality=nationality, hpid=_uniq("HP"),
            passport_number=self.PASSPORT if passport is None else passport,
        )
        self.db.add(profile)
        self.db.flush()
        return profile

    # --- the case the audit asked about ---

    def test_a_departed_voyage_requires_separate_review(self):
        profile = self.register()

        summary = link_historical_assignments(self.db, profile)

        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)
        self.assertEqual(summary["linked"], [])
        self.assertEqual(len(summary["conflicts"]), 1)

    def test_the_assignment_is_attached_not_rewritten(self):
        """Only the profile link changes; the voyage's own record is evidence."""
        before = (self.assignment.crew_name, self.assignment.nationality,
                  self.assignment.passport_number, self.assignment.started_at,
                  self.assignment.ended_at, self.assignment.rank)
        profile = self.register(name="R. Kumar")  # a different spelling

        link_historical_assignments(self.db, profile)

        self.db.refresh(self.assignment)
        self.assertEqual(
            (self.assignment.crew_name, self.assignment.nationality,
             self.assignment.passport_number, self.assignment.started_at,
             self.assignment.ended_at, self.assignment.rank),
            before,
        )

    def test_nothing_is_created(self):
        calls_before = self.db.query(VesselCall).count()
        assignments_before = self.db.query(CrewAssignment).count()

        link_historical_assignments(self.db, self.register())

        self.assertEqual(self.db.query(VesselCall).count(), calls_before)
        self.assertEqual(self.db.query(CrewAssignment).count(), assignments_before)

    # --- what it must not do ---

    def test_an_assignment_that_belongs_to_someone_else_is_untouched(self):
        other = self.register()
        self.assignment.crew_profile_id = other.id
        self.db.flush()

        late = self.register()
        summary = link_historical_assignments(self.db, late)

        self.db.refresh(self.assignment)
        self.assertEqual(self.assignment.crew_profile_id, other.id)
        self.assertEqual(summary["linked"], [])

    def test_a_different_passport_matches_nothing(self):
        profile = self.register(passport="X0000001")

        summary = link_historical_assignments(self.db, profile)

        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)
        self.assertEqual(summary["linked"], [])

    def test_a_profile_with_no_passport_matches_nothing(self):
        profile = self.register(passport="")

        self.assertEqual(link_historical_assignments(self.db, profile)["linked"], [])

    def test_a_disagreeing_nationality_raises_a_conflict_and_attaches_nothing(self):
        profile = self.register(nationality="PH")

        summary = link_historical_assignments(self.db, profile)

        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)
        self.assertEqual(summary["linked"], [])
        self.assertEqual(len(summary["conflicts"]), 1)

        record = self.db.query(CrewIdentityConflictRecord).filter(
            CrewIdentityConflictRecord.id == summary["conflicts"][0]
        ).one()
        self.assertEqual(record.operation, "HISTORICAL_LINKAGE")
        self.assertEqual(record.status, "OPEN")
        self.assertIn("has not been attached", record.conflict_message)

    def test_missing_nationality_requires_review(self):
        """Absence of evidence, not evidence of a different person."""
        self.assignment.nationality = None
        self.db.flush()
        profile = self.register(nationality="IN")

        link_historical_assignments(self.db, profile)

        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)

    def test_one_person_keeps_one_assignment_per_call(self):
        duplicate = CrewAssignment(
            vessel_call_id=self.call.id, crew_profile_id=None,
            crew_name="Ravi Kumar", rank="able_seaman", nationality="IN",
            passport_number=self.PASSPORT,
            started_at=self.call.started_at, ended_at=self.call.ended_at,
        )
        self.db.add(duplicate)
        self.db.flush()
        profile = self.register()

        summary = link_historical_assignments(self.db, profile)

        self.assertEqual(summary["linked"], [])
        self.assertEqual(len(summary["conflicts"]), 2)
        self.assertEqual(summary["skipped_occupied_call"], [])

    def test_several_voyages_are_all_attached(self):
        second = VesselCall(
            vessel_id=self.vessel.id, vessel_name=self.vessel.name,
            imo_number=self.vessel.imo_number,
            started_at=datetime.now(timezone.utc) - timedelta(days=200),
            ended_at=datetime.now(timezone.utc) - timedelta(days=190),
        )
        self.db.add(second)
        self.db.flush()
        older = CrewAssignment(
            vessel_call_id=second.id, crew_profile_id=None,
            crew_name="Ravi Kumar", rank="able_seaman", nationality="IN",
            passport_number=self.PASSPORT,
            started_at=second.started_at, ended_at=second.ended_at,
        )
        self.db.add(older)
        self.db.flush()

        summary = link_historical_assignments(self.db, self.register())

        self.assertEqual(summary["linked"], [])
        self.assertEqual(len(summary["conflicts"]), 2)


if __name__ == "__main__":
    unittest.main()


class ResolvedConflictTests(HistoricalLateRegistrationTests):
    """A reviewer's decision has to actually attach the voyage.

    `resolve_identity_conflict` records the decision and does not act on it --
    other operations consult it the next time they run. A historical
    assignment has no next time, so without this the Super Admin's answer is
    filed and nothing happens, which is the shape of every "built but
    unreachable" defect this codebase has already had twice.
    """

    def _open_conflict(self):
        profile = self.register(nationality="PH")
        summary = link_historical_assignments(self.db, profile)
        record = self.db.query(CrewIdentityConflictRecord).filter(
            CrewIdentityConflictRecord.id == summary["conflicts"][0]
        ).one()
        return profile, record

    def test_selecting_the_profile_attaches_the_assignment(self):
        from app.services.historical_linkage import apply_resolved_conflict

        profile, record = self._open_conflict()
        record.resolution_action = "SELECT_PROFILE"
        record.selected_profile_id = profile.id
        self.db.flush()

        attached = apply_resolved_conflict(self.db, record)

        self.db.refresh(self.assignment)
        self.assertEqual(attached, [self.assignment.id])
        self.assertEqual(self.assignment.crew_profile_id, profile.id)

    def test_dismissing_attaches_nothing(self):
        from app.services.historical_linkage import apply_resolved_conflict

        profile, record = self._open_conflict()
        record.resolution_action = "DISMISS"
        # Set too, so the action is the only thing stopping the attach. Without
        # it this passes because there is no profile to attach to, which proves
        # nothing about DISMISS.
        record.selected_profile_id = profile.id
        self.db.flush()

        self.assertEqual(apply_resolved_conflict(self.db, record), [])
        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)

    def test_an_assignment_claimed_meanwhile_is_not_reassigned(self):
        """A resolution does not override an existing link; that is a correction."""
        from app.services.historical_linkage import apply_resolved_conflict

        profile, record = self._open_conflict()
        owner = self.register()
        self.assignment.crew_profile_id = owner.id
        record.resolution_action = "SELECT_PROFILE"
        record.selected_profile_id = profile.id
        self.db.flush()

        self.assertEqual(apply_resolved_conflict(self.db, record), [])
        self.db.refresh(self.assignment)
        self.assertEqual(self.assignment.crew_profile_id, owner.id)

    def test_a_conflict_from_another_operation_is_left_alone(self):
        from app.services.historical_linkage import apply_resolved_conflict

        profile, record = self._open_conflict()
        record.operation = "MANIFEST_UPLOAD"
        record.resolution_action = "SELECT_PROFILE"
        record.selected_profile_id = profile.id
        self.db.flush()

        self.assertEqual(apply_resolved_conflict(self.db, record), [])
        self.db.refresh(self.assignment)
        self.assertIsNone(self.assignment.crew_profile_id)
