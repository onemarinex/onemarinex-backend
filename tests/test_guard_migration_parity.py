"""Every startup guard should have a migration saying the same thing.

Alembic does not run on this deployment, so the `ensure_*` guards in
`app/main.py` are what actually applies DDL. The migrations are the record of
the same change for anywhere that does run them. When the two disagree, the
disagreement is invisible until someone builds a database the other way.

Three changes had shipped with a guard and no migration at all --
`crew_assignments.source`, the two movement timestamps, and
`shore_passes.crew_profile_id DROP NOT NULL` -- which is what this test now
prevents.

It reads both sides as text rather than running them. That is deliberate:
executing migrations against a real database is a rehearsal, not a unit test,
and the thing worth catching here is an *omission*, which text can see.
"""

import pathlib
import re
import unittest

BACKEND = pathlib.Path(__file__).resolve().parent.parent


def _collapse(source: str) -> str:
    """Join Python's implicit string concatenation, so split SQL reads as one."""
    return re.sub(r'"\s*\n\s*"', "", source)


def _guard_columns() -> set:
    """(table, column) pairs added by an `ensure_*` guard, where it says so."""
    source = _collapse((BACKEND / "app/main.py").read_text())
    found = set()
    for table, column in re.findall(
        r"ALTER TABLE (\w+)\s+ADD COLUMN\s+(?:IF NOT EXISTS\s+)?(\w+)", source
    ):
        if column != "IF":  # an f-string built the name; invisible to text
            found.add((table, column))
    return found


def _migration_columns() -> set:
    """(table, column) pairs any migration adds, by either API."""
    found = set()
    for path in (BACKEND / "alembic/versions").glob("*.py"):
        source = _collapse(path.read_text())
        for table, column in re.findall(
            r'op\.add_column\(\s*["\'](\w+)["\'],\s*sa\.Column\(\s*["\'](\w+)["\']',
            source,
        ):
            found.add((table, column))
        for table, column in re.findall(
            r"ALTER TABLE (\w+)\s+ADD COLUMN\s+(?:IF NOT EXISTS\s+)?(\w+)", source
        ):
            found.add((table, column))
    return found


class GuardMigrationParityTests(unittest.TestCase):
    # Guards whose columns predate this convention. They are in the deployed
    # database and a migration for them would be fiction, so they are exempt
    # by name rather than by silence.
    LEGACY_GUARDS = frozenset({
        ("agent_profiles", "agency_logo_url"),
        ("agent_profiles", "agency_rules"),
        ("agent_profiles", "support_number"),
        ("aggregator_profiles", "provider_type"),
        ("cab_bookings", "vessel_id"),
        ("crew_profiles", "selected_vessel_id"),
        ("notifications", "audience_type"),
        ("notifications", "target_vessel_ids"),
        ("port_rules", "timezone"),
        ("ports", "canonical_key"),
        ("vendors", "commission_percentage"),
        ("vessels", "shore_pass_valid_upto"),
    })

    def test_every_guarded_column_has_a_migration(self):
        unmigrated = _guard_columns() - _migration_columns() - self.LEGACY_GUARDS
        self.assertEqual(
            unmigrated,
            set(),
            "These columns are added by a startup guard and by no migration, so a "
            "database built from migrations would not have them: "
            f"{sorted(unmigrated)}. Write the migration, or add the pair to "
            "LEGACY_GUARDS with a reason.",
        )

    def test_the_three_that_were_missing_are_covered(self):
        """Named explicitly, so a regression says which change regressed."""
        migrated = _migration_columns()
        for pair in (("crew_assignments", "source"),
                     ("crew_assignments", "signed_on_at"),
                     ("crew_assignments", "signed_off_at")):
            with self.subTest(pair=pair):
                self.assertIn(pair, migrated)

    def test_the_shore_pass_nullability_change_has_a_migration(self):
        """Not a column addition, so the pair check above cannot see it.

        Looks for the *alteration* specifically. Three migrations mention
        shore_passes, crew_profile_id and nullability somewhere -- the table's
        own CREATE among them -- so a looser check passes whether or not the
        change exists, which is how the first version of this test was wrong.
        """
        pattern = re.compile(
            r'op\.alter_column\(\s*["\']shore_passes["\'],\s*["\']crew_profile_id["\']'
            # Not [^)]* -- existing_type=sa.Integer() has a bracket in it.
            r'.{0,200}?nullable\s*=\s*True',
            re.S,
        )
        matches = [
            path.name for path in (BACKEND / "alembic/versions").glob("*.py")
            if pattern.search(path.read_text())
            or "ALTER COLUMN crew_profile_id DROP NOT NULL" in path.read_text()
        ]
        self.assertTrue(
            matches,
            "app/main.py drops NOT NULL on shore_passes.crew_profile_id at "
            "startup and no migration does the same.",
        )


if __name__ == "__main__":
    unittest.main()
