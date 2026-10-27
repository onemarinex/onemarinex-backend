"""The spec-hardening script must encrypt credentials and nothing else.

Audit E01: every environment variable on the deployed backend is type GENERAL,
which DigitalOcean stores in plaintext. This script flips the credential-bearing
ones to SECRET. It is deliberately a generator rather than an applier --
`doctl apps update --spec` replaces the whole spec, and there is no staging
environment to discover a mistake on.

Tested against a synthetic spec, never a real one.
"""

import unittest

from scripts.harden_app_secrets import SECRET_KEYS, _flip


class FlipTests(unittest.TestCase):
    def _envs(self):
        return [
            {"key": "SECRET_KEY", "value": "s", "type": "GENERAL"},
            {"key": "DATABASE_URL", "value": "d", "type": "GENERAL"},
            {"key": "SPACES_SECRET", "value": "p", "type": "GENERAL"},
            {"key": "APP_NAME", "value": "HeyPorts", "type": "GENERAL"},
            {"key": "DEBUG", "value": "false", "type": "GENERAL"},
        ]

    def test_credentials_are_flipped(self):
        envs = self._envs()
        changed = _flip(envs)
        self.assertEqual(
            sorted(changed), ["DATABASE_URL", "SECRET_KEY", "SPACES_SECRET"]
        )
        for e in envs:
            expected = "SECRET" if e["key"] in SECRET_KEYS else "GENERAL"
            self.assertEqual(e["type"], expected, e["key"])

    def test_a_hostname_or_flag_is_left_readable(self):
        """Encrypting these buys nothing and makes the console harder to read."""
        envs = self._envs()
        _flip(envs)
        self.assertEqual(
            [e["type"] for e in envs if e["key"] in ("APP_NAME", "DEBUG")],
            ["GENERAL", "GENERAL"],
        )

    def test_no_value_is_altered(self):
        envs = self._envs()
        before = {e["key"]: e["value"] for e in envs}
        _flip(envs)
        self.assertEqual({e["key"]: e["value"] for e in envs}, before)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        envs = self._envs()
        _flip(envs)
        self.assertEqual(_flip(envs), [])

    def test_an_already_encrypted_value_is_not_touched(self):
        envs = [{"key": "SECRET_KEY", "value": "EV[1:abc]", "type": "SECRET"}]
        self.assertEqual(_flip(envs), [])

    def test_every_credential_the_audit_named_is_covered(self):
        for key in (
            "SECRET_KEY", "DATABASE_URL", "SPACES_KEY", "SPACES_SECRET",
            "ANTHROPIC_API_KEY", "WHATSAPP_ACCESS_TOKEN", "SMTP_PASSWORD",
        ):
            self.assertIn(key, SECRET_KEYS)


if __name__ == "__main__":
    unittest.main()
