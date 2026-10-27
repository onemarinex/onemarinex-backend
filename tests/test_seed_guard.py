"""Seeding scripts must not be able to reach a deployed database by accident.

`app/db/seed_superadmin.py` used to hardcode `admin123` and check nothing at
all, so one command in a deployed console created an administrator with a
guessable password. These pin the guard that replaced that.
"""

import unittest
from unittest import mock

from app.db import seed_guard


class DatabaseIsLocalTests(unittest.TestCase):
    def test_the_usual_local_spellings_are_accepted(self):
        for url in (
            "postgresql://u:p@localhost:5432/onemarinex",
            "postgresql://u:p@127.0.0.1:5432/onemarinex",
            "postgresql://u:p@[::1]:5432/onemarinex",
            "postgresql://u:p@db:5432/onemarinex",        # docker-compose
            "postgresql://u:p@postgres:5432/onemarinex",  # docker-compose
        ):
            with self.subTest(url=url):
                self.assertTrue(seed_guard.database_is_local(url))

    def test_a_deployed_host_is_not_local(self):
        for url in (
            "postgresql://u:p@db.example.com:5432/prod",
            "postgresql://u:p@private-db-postgresql-nyc3-0.b.db.ondigitalocean.com:25060/db",
        ):
            with self.subTest(url=url):
                self.assertFalse(seed_guard.database_is_local(url))

    def test_a_host_merely_containing_a_local_name_is_still_refused(self):
        """`localhost.attacker.example` must not read as local."""
        self.assertFalse(
            seed_guard.database_is_local(
                "postgresql://u:p@notlocalhostile.example.com:5432/db"
            )
        )


class RefuseIfRemoteTests(unittest.TestCase):
    def _refuse(self, *, local, override):
        env = {"ALLOW_REMOTE_SEED": "1"} if override else {}
        with mock.patch.object(seed_guard, "database_is_local", return_value=local), \
             mock.patch.dict(seed_guard.os.environ, env, clear=True):
            seed_guard.refuse_if_remote("test")

    def test_a_local_database_is_allowed(self):
        self._refuse(local=True, override=False)  # must not raise

    def test_a_remote_database_exits(self):
        with self.assertRaises(SystemExit) as caught:
            self._refuse(local=False, override=False)
        self.assertIn("does not look local", str(caught.exception))

    def test_the_override_is_what_lets_a_remote_database_through(self):
        self._refuse(local=False, override=True)  # must not raise


if __name__ == "__main__":
    unittest.main()
