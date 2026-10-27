"""The deploy must fail rather than serve a broken application.

Every `ensure_*` guard in app.main catches its own exception and logs it, so a
guard that cannot take its ALTER TABLE lock leaves the application booting
normally with a column the ORM declares and the database lacks. Measured
against a database in exactly that state:

    ORM query FAILS: UndefinedColumn — crew_assignments.public_ref does not exist

...while the deployment reports success throughout. DigitalOcean aborts a
deployment whose process exits and keeps the previous version serving, so
raising converts a silent outage into a failed deploy.
"""

import unittest
from unittest import mock

from app.db import schema_verification as verify
from app.db.schema_verification import (
    GUARDED_COLUMNS,
    missing_guarded_columns,
    verify_critical_schema,
)


class HealthyTests(unittest.TestCase):
    def test_the_configured_database_is_complete(self):
        self.assertEqual(missing_guarded_columns(), {})

    def test_verification_passes_quietly_when_nothing_is_missing(self):
        verify_critical_schema()  # must not raise


class FailureTests(unittest.TestCase):
    def test_a_missing_column_refuses_the_start(self):
        with mock.patch.object(
            verify, "missing_guarded_columns",
            return_value={"crew_assignments": ["public_ref"]},
        ):
            with self.assertRaises(RuntimeError) as caught:
                verify_critical_schema()
        self.assertIn("Refusing to start", str(caught.exception))

    def test_the_message_names_every_table_and_column(self):
        with mock.patch.object(
            verify, "missing_guarded_columns",
            return_value={
                "crew_assignments": ["public_ref"],
                "incidents": ["occurred_at"],
            },
        ):
            with self.assertRaises(RuntimeError) as caught:
                verify_critical_schema()
        message = str(caught.exception)
        for token in ("crew_assignments", "public_ref", "incidents", "occurred_at"):
            self.assertIn(token, message)

    def test_the_message_says_what_to_do_about_it(self):
        """A deploy failing at 2am should carry its own remedy."""
        with mock.patch.object(
            verify, "missing_guarded_columns",
            return_value={"crew_assignments": ["public_ref"]},
        ):
            with self.assertRaises(RuntimeError) as caught:
                verify_critical_schema()
        self.assertIn("lock", str(caught.exception).lower())

    def test_an_absent_table_is_not_a_failure(self):
        """create_all builds missing tables reliably; that is not this check."""
        with mock.patch.object(verify, "inspect") as inspector:
            inspector.return_value.get_table_names.return_value = []
            self.assertEqual(missing_guarded_columns(), {})


class CoverageTests(unittest.TestCase):
    """A guard without an entry here can still fail silently."""

    # Four guards build their DDL with an f-string, so the column name is not
    # in the source and cannot be read back from it. They are named here so a
    # *new* interpolated guard trips this test: write the statement out
    # literally, as ensure_ride_otp_attempt_columns does, or add the table
    # here on purpose.
    INTERPOLATED_DDL_TABLES = frozenset({
        "chat_messages", "crew_sos_requests", "expense_bills",
        "crew_assignments", "incidents",
    })

    @staticmethod
    def _altered_columns():
        """Every (table, column) an `ALTER TABLE ... ADD COLUMN` names literally."""
        import pathlib
        import re

        source = pathlib.Path("app/main.py").read_text()
        # Python concatenates adjacent string literals, so a statement split
        # across lines is one SQL string at runtime and must be read as one.
        joined = re.sub(r'"\s*\n\s*"', "", source)
        found = {}
        for table, column in re.findall(
            r"ALTER TABLE (\w+)\s+ADD COLUMN\s+(?:IF NOT EXISTS\s+)?(\w+)", joined
        ):
            found.setdefault(table, set()).add(column)
        return found

    def test_every_startup_added_column_is_verified_or_explicitly_excluded(self):
        """A new column must land in one list or the other.

        This used to ask about the *table*, which is too coarse: the
        cab_bookings OTP counters inherited an exemption written years earlier
        about a different column, and would have shipped unverified. A failed
        guard there breaks every query against the busiest table in the app
        while the deployment reports success.
        """
        from app.db.schema_verification import DELIBERATELY_UNVERIFIED

        for table, columns in self._altered_columns().items():
            for column in columns:
                if column == "IF":
                    continue  # interpolated; covered by the test below
                with self.subTest(table=table, column=column):
                    self.assertTrue(
                        column in GUARDED_COLUMNS.get(table, ())
                        or column in DELIBERATELY_UNVERIFIED.get(table, frozenset()),
                        f"{table}.{column} is added by a startup guard and is in "
                        "neither list. Add it to GUARDED_COLUMNS, or to "
                        "DELIBERATELY_UNVERIFIED with a reason.",
                    )

    def test_no_new_guard_hides_its_column_name_from_this_file(self):
        found = self._altered_columns()
        interpolated = {table for table, columns in found.items() if "IF" in columns}
        self.assertEqual(
            interpolated,
            self.INTERPOLATED_DDL_TABLES,
            "A guard builds its column name with an f-string, so no test can "
            "read it back. Write the ALTER TABLE out literally, or add the "
            "table to INTERPOLATED_DDL_TABLES on purpose.",
        )

    def test_a_not_null_column_cannot_be_exempted(self):
        """Exempting one is always wrong, so this does judge the choice.

        The other coverage tests only check that somebody chose a list. This
        one can tell a bad choice from a good one: if the ORM declares a
        column NOT NULL and its guard fails, every INSERT against that table
        fails immediately. There is no benign reason to leave that unverified.
        """
        import app.db.base  # noqa: F401 — registers every model on Base
        from app.db.base import Base
        from app.db.schema_verification import DELIBERATELY_UNVERIFIED

        by_table = {table.name: table for table in Base.metadata.sorted_tables}
        for table_name, columns in DELIBERATELY_UNVERIFIED.items():
            table = by_table.get(table_name)
            if table is None:
                continue
            for column_name in columns:
                column = table.columns.get(column_name)
                if column is None:
                    continue
                with self.subTest(table=table_name, column=column_name):
                    self.assertTrue(
                        column.nullable,
                        f"{table_name}.{column_name} is NOT NULL and exempt from "
                        "startup verification. A guard that fails to add it takes "
                        "every write to that table down while the deploy reports "
                        "success. Move it to GUARDED_COLUMNS.",
                    )

    def test_the_two_lists_do_not_overlap(self):
        from app.db.schema_verification import DELIBERATELY_UNVERIFIED

        for table, columns in GUARDED_COLUMNS.items():
            overlap = set(columns) & DELIBERATELY_UNVERIFIED.get(table, frozenset())
            self.assertEqual(overlap, set(), f"{table}: {overlap} is in both lists")

    def test_verification_runs_after_every_guard(self):
        import pathlib

        source = pathlib.Path("app/main.py").read_text()
        startup = source[source.index("def on_startup"):source.index("def _log_whatsapp_config")]
        position = startup.index("verify_critical_schema()")
        for guard in ("ensure_assignment_public_ref", "ensure_magic_link_lifecycle",
                      "ensure_incident_occurred_at", "ensure_alembic_baseline"):
            with self.subTest(guard=guard):
                self.assertLess(startup.index(guard + "()"), position)


if __name__ == "__main__":
    unittest.main()


class StartupWiringTests(unittest.TestCase):
    """The names on_startup calls must actually resolve.

    Added after `verify_critical_schema()` was wired into on_startup without
    its import landing. Every unit test passed, because they import the
    function directly, and the application failed on boot with a NameError --
    which the schema verification would have been there to prevent. Caught
    locally by starting the server; that should not have been the first check.
    """

    def test_on_startup_resolves_every_name_it_calls(self):
        import ast
        import pathlib

        import app.main as main

        source = pathlib.Path("app/main.py").read_text()
        tree = ast.parse(source)
        startup = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "on_startup"
        )
        called = {
            node.func.id for node in ast.walk(startup)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        for name in sorted(called):
            with self.subTest(name=name):
                self.assertTrue(
                    hasattr(main, name) or name in dir(__builtins__),
                    f"on_startup calls {name}(), which app.main does not define or import",
                )

    def test_the_application_imports_cleanly(self):
        """A NameError at module scope would fail every deploy."""
        import importlib

        import app.main

        importlib.reload(app.main)
