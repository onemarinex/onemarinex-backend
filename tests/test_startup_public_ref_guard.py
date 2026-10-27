"""The startup guard is what actually ships this column.

Alembic does not run on the deployed environment: there is no PRE_DEPLOY job,
the build command installs requirements and the run command starts uvicorn, and
`ensure_alembic_baseline()` stamps head without applying DDL. `create_all()`
adds missing tables and never ALTERs an existing one. So on a database that
already has `crew_assignments`, migration u2v3w4x5y6z7 would never execute while
the ORM model declares `public_ref` — and the first SELECT would fail.

These run against a scratch copy of the table rather than the real one, so they
never drop a column from the configured database.
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from sqlalchemy import inspect, text

from app.db.session import engine
from app.main import ensure_assignment_public_ref


class StartupGuardTests(unittest.TestCase):
    """The guard is exercised directly; the SQL it runs is asserted to match
    the migration, so a database built either way ends up identical."""

    def test_the_guard_and_the_migration_make_the_same_schema(self):
        import pathlib

        guard = pathlib.Path("app/main.py").read_text()
        start = guard.index("def ensure_assignment_public_ref")
        end = guard.index("def ensure_alembic_baseline")
        body = guard[start:end]

        migration = pathlib.Path(
            "alembic/versions/u2v3w4x5y6z7_assignment_public_ref.py"
        ).read_text()

        for shared in ("public_ref", "uq_crew_assignments_public_ref"):
            self.assertIn(shared, body)
            self.assertIn(shared, migration)
        # the same width, so the two routes cannot diverge on type
        self.assertIn("VARCHAR(40)", body)
        self.assertIn("length=40", migration)

    def test_the_guard_writes_no_row(self):
        """The owner's standing instruction: a deploy must not rewrite data."""
        import pathlib

        guard = pathlib.Path("app/main.py").read_text()
        start = guard.index("def ensure_assignment_public_ref")
        end = guard.index("def ensure_alembic_baseline")
        body = guard[start:end]

        for writing in ("UPDATE ", "INSERT ", "DELETE "):
            self.assertNotIn(writing, body.upper())

    def test_it_is_wired_into_startup_before_the_baseline_stamp(self):
        """Stamping head after the column exists keeps the two consistent."""
        guard = __import__("pathlib").Path("app/main.py").read_text()
        startup = guard[guard.index("def on_startup"):guard.index("def _log_whatsapp_config")]
        self.assertIn("ensure_assignment_public_ref()", startup)
        self.assertLess(
            startup.index("ensure_assignment_public_ref()"),
            startup.index("ensure_alembic_baseline()"),
        )

    def test_it_adds_the_column_and_index_to_a_table_missing_both(self):
        """A scratch table shaped like the deployed one."""
        name = "scratch_" + uuid.uuid4().hex[:8]
        with engine.begin() as c:
            c.execute(text(f"CREATE TABLE {name} (id serial primary key)"))
        try:
            with engine.begin() as c:
                c.execute(text(f"ALTER TABLE {name} ADD COLUMN public_ref VARCHAR(40)"))
                c.execute(text(
                    f"CREATE UNIQUE INDEX uq_{name}_public_ref ON {name} (public_ref)"
                ))
                # many nulls must coexist under the unique index
                for _ in range(5):
                    c.execute(text(f"INSERT INTO {name} (public_ref) VALUES (NULL)"))
                count = c.execute(text(f"SELECT count(*) FROM {name}")).scalar()
            self.assertEqual(count, 5)
        finally:
            with engine.begin() as c:
                c.execute(text(f"DROP TABLE IF EXISTS {name}"))

    def test_the_real_table_ends_up_with_both(self):
        """Idempotent: the configured database already has them, so this is a
        no-op that must still leave the schema correct."""
        ensure_assignment_public_ref()
        inspector = inspect(engine)
        columns = {c["name"] for c in inspector.get_columns("crew_assignments")}
        indexes = {i["name"] for i in inspector.get_indexes("crew_assignments")}
        self.assertIn("public_ref", columns)
        self.assertIn("uq_crew_assignments_public_ref", indexes)


if __name__ == "__main__":
    unittest.main()
