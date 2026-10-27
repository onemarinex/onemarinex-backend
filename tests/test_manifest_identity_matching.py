"""What links a manifest row to a registered person — HPID spec §4.1.

    "Normalized passport number + normalized nationality are the primary
    matching inputs... An HPID is never the identity-matching key."

Both halves were wrong before. The matcher joined its clauses with OR, so a
passport number alone was enough — and passport numbers are not unique across
issuing states. It also matched on the legacy HPID, which is derived from the
passport, making the public identifier an identity credential.

The tests that matter here are the ones where matching must *fail*. Refusing
to link costs a person a PENDING card; linking wrongly attaches them to a
stranger's voyage, and every eligibility and booking decision beneath it.
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy import text

from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import SessionLocal
from app.services.historical_context import crew_profile_for_manifest


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ManifestMatchingTests(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.passport = _uniq("P").replace("-", "").upper()[:12]
        self.user_ids = []
        self.profile_ids = []
        self.manifest_ids = []
        self.vessel = Vessel(
            name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(self.vessel)
        self.db.flush()

    def tearDown(self):
        self.db.rollback()
        for stmt, params in (
            ("DELETE FROM vessel_crew WHERE id = ANY(:v)", {"v": self.manifest_ids or [0]}),
            ("DELETE FROM vessels WHERE id = :v", {"v": self.vessel.id}),
            ("DELETE FROM crew_profiles WHERE id = ANY(:v)", {"v": self.profile_ids or [0]}),
            ("DELETE FROM users WHERE id = ANY(:v)", {"v": self.user_ids or [0]}),
        ):
            self.db.execute(text(stmt), params)
        self.db.commit()
        self.db.close()

    def _profile(self, *, passport, nationality, hpid=None):
        user = User(email=_uniq("crew") + "@x.com", hashed_password="x", role="crew")
        self.db.add(user)
        self.db.flush()
        self.user_ids.append(user.id)
        profile = CrewProfile(
            user_id=user.id, full_name="Crew", rank="able_seaman",
            nationality=nationality, passport_number=passport,
            hpid=hpid or _uniq("HP"),
        )
        self.db.add(profile)
        self.db.flush()
        self.profile_ids.append(profile.id)
        return profile

    def _manifest(self, *, passport, nationality, hp_id=None):
        row = VesselCrew(
            vessel_id=self.vessel.id, name="Crew", rank="able_seaman",
            passport_number=passport, nationality=nationality, hp_id=hp_id,
        )
        self.db.add(row)
        self.db.flush()
        self.manifest_ids.append(row.id)
        return row

    # --- it must link ---------------------------------------------------------

    def test_passport_and_nationality_together_link(self):
        profile = self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=self.passport, nationality="IN")
        self.assertEqual(crew_profile_for_manifest(self.db, row), profile)

    def test_the_two_sides_may_spell_the_nationality_differently(self):
        """A manifest says Indian, a profile says IN. One person."""
        profile = self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=self.passport, nationality="Indian")
        self.assertEqual(crew_profile_for_manifest(self.db, row), profile)

    def test_a_spaced_passport_still_links(self):
        # CrewProfile.nationality is VARCHAR(2): a profile already stores the
        # canonical code, and it is the manifest side that arrives free-text.
        profile = self._profile(passport="AB 123456", nationality="IN")
        row = self._manifest(passport="ab123456", nationality="IN")
        self.assertEqual(crew_profile_for_manifest(self.db, row), profile)

    # --- it must not ----------------------------------------------------------

    def test_the_same_passport_from_a_different_country_is_a_different_person(self):
        """The reason nationality was added. A number alone is not an identity."""
        self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=self.passport, nationality="PH")
        self.assertIsNone(crew_profile_for_manifest(self.db, row))

    def test_the_legacy_hpid_no_longer_matches_anything(self):
        """§4.1: the HPID is never the identity-matching key."""
        shared = _uniq("HP")
        self._profile(passport=self.passport, nationality="IN", hpid=shared)
        row = self._manifest(passport=None, nationality="IN", hp_id=shared)
        self.assertIsNone(crew_profile_for_manifest(self.db, row))

    def test_a_missing_passport_does_not_link(self):
        self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=None, nationality="IN")
        self.assertIsNone(crew_profile_for_manifest(self.db, row))

    def test_a_missing_nationality_does_not_link(self):
        self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=self.passport, nationality=None)
        self.assertIsNone(crew_profile_for_manifest(self.db, row))

    def test_an_unresolvable_nationality_is_refused_rather_than_guessed(self):
        """IND is both India and Indonesia. Two countries, so no answer."""
        self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=self.passport, nationality="IND")
        self.assertIsNone(crew_profile_for_manifest(self.db, row))

    def test_two_people_with_the_same_evidence_is_a_conflict_not_a_tie(self):
        self._profile(passport=self.passport, nationality="IN")
        self._profile(passport=self.passport, nationality="IN")
        row = self._manifest(passport=self.passport, nationality="IN")
        self.assertIsNone(crew_profile_for_manifest(self.db, row))
