"""The app must not sign tokens with the key that is in this repository.

Audit "additional controls — signing configuration". `SECRET_KEY` fell back to
a literal in `app/core/config.py`, so any environment that failed to set it
signed every JWT with a value anyone reading the source could use to mint a
token for any account, superadmin included. Tokens last 14 days.

Checked against the deployed environment on 26 Oct 2026: SECRET_KEY *is* set
there, so this guard changes nothing about the current deployment. It exists so
the fallback cannot come into use later without the deploy failing loudly.
"""

import subprocess
import sys
import textwrap
import unittest


def _boot(secret_key, database_url):
    """Import config in a clean interpreter and report whether it started.

    A subprocess because the guard calls sys.exit at import, and because
    `load_dotenv()` would otherwise put this machine's .env back in the way.
    """
    code = textwrap.dedent("""
        from app.core.config import settings
        print("STARTED")
    """)
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": ".",
        "SECRET_KEY": secret_key,
        "DATABASE_URL": database_url,
    }
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120,
    )
    return "STARTED" in result.stdout, (result.stdout + result.stderr)


class SigningKeyGuardTests(unittest.TestCase):
    FALLBACK = "onemarinexsecret"

    def test_the_repository_key_against_a_remote_database_refuses_to_start(self):
        started, output = _boot(self.FALLBACK, "postgresql://u:p@db.example.com:5432/prod")
        self.assertFalse(started, "the insecure fallback was accepted")
        self.assertIn("Refusing to start", output)

    def test_a_laptop_is_left_alone(self):
        started, _ = _boot(self.FALLBACK, "postgresql://u:p@localhost:5432/onemarinex")
        self.assertTrue(started, "local development must keep working")

    def test_a_real_key_against_a_remote_database_starts(self):
        started, _ = _boot("a-real-secret-value", "postgresql://u:p@db.example.com:5432/prod")
        self.assertTrue(started)

    def test_a_host_merely_containing_localhost_does_not_slip_through(self):
        started, output = _boot(
            self.FALLBACK, "postgresql://u:p@notlocalhostile.example.com:5432/db"
        )
        self.assertFalse(started)
        self.assertIn("Refusing to start", output)


class IgnoredEnvironmentVariableTests(unittest.TestCase):
    """Both were set on the deployed app and read by nothing."""

    def _value(self, name, value, attribute):
        code = textwrap.dedent(f"""
            from app.core.config import settings
            print(settings.{attribute})
        """)
        env = {
            "PATH": "/usr/bin:/bin", "PYTHONPATH": ".",
            "SECRET_KEY": "a-real-secret-value",
            "DATABASE_URL": "postgresql://u:p@localhost:5432/onemarinex",
            name: value,
        }
        result = subprocess.run(
            [sys.executable, "-c", code], env=env,
            capture_output=True, text=True, timeout=120,
        )
        return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""

    def test_access_token_expiry_is_now_configurable(self):
        self.assertEqual(
            self._value("ACCESS_TOKEN_EXPIRE_MINUTES", "60", "ACCESS_TOKEN_EXPIRE_MINUTES"),
            "60",
        )

    def test_the_default_is_unchanged_when_unset(self):
        self.assertEqual(
            self._value("APP_NAME", "ignored", "ACCESS_TOKEN_EXPIRE_MINUTES"), "20160"
        )

    def test_algorithm_is_now_configurable(self):
        self.assertEqual(self._value("ALGORITHM", "HS512", "ALGORITHM"), "HS512")


if __name__ == "__main__":
    unittest.main()
