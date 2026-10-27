"""Passwords could be guessed at without limit.

`POST /auth/login`, `/auth/login-unified` and `/drivers/login` each compared a
password and raised 401. No counter, no cooldown, no lockout, and
`login_events` records only *successful* logins — so nothing even recorded a
failure. Every account in the system, superadmin included, was an unlimited
guessing surface.

The same policy that limits OTP attempts now covers passwords. These drive it
through the real routes, because a policy that is correct in isolation and
wired into two of three login paths is the defect it replaced.

Runs inside a transaction that is always rolled back.
"""

import asyncio
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import routes_auth, routes_drivers
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.driver import Driver
from app.db.models.port import Port
from app.db.models.user import User
from app.db.session import engine
from app.services import attempt_limits
from app.services.auth import get_password_hash as hash_password

PASSWORD = "correct-horse-battery"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class LoginAttemptLimitTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.email = _uniq("crew") + "@example.com"
        self.user = User(email=self.email, hashed_password=hash_password(PASSWORD),
                         role="crew")
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def attempt(self, password, *, email=None):
        """One login, with the cooldown wound back so only the count is tested."""
        self.user.last_auth_attempt_at = None
        self.db.flush()
        return routes_auth.login(
            body=SimpleNamespace(email=email or self.email, password=password),
            db=self.db,
        )

    def test_the_right_password_still_works(self):
        result = self.attempt(PASSWORD)
        self.assertTrue(result.access_token)

    def test_a_wrong_password_is_counted(self):
        with self.assertRaises(HTTPException):
            self.attempt("wrong")
        self.db.refresh(self.user)
        self.assertEqual(self.user.failed_auth_attempts, 1)

    def test_guessing_is_locked_out_after_five_tries(self):
        for _ in range(attempt_limits.MAX_FAILED_ATTEMPTS - 1):
            with self.assertRaises(HTTPException) as error:
                self.attempt("wrong")
            self.assertEqual(error.exception.status_code, 401)

        with self.assertRaises(HTTPException) as error:
            self.attempt("wrong")
        self.assertEqual(error.exception.status_code, 429)

        # The lockout holds against the real password too, so landing on the
        # last guess wins nothing.
        with self.assertRaises(HTTPException) as error:
            self.attempt(PASSWORD)
        self.assertEqual(error.exception.status_code, 429)

    def test_a_wrong_password_and_an_unknown_account_look_the_same(self):
        """A 401 must not say whether the address exists."""
        with self.assertRaises(HTTPException) as wrong:
            self.attempt("wrong")
        with self.assertRaises(HTTPException) as unknown:
            self.attempt("wrong", email=_uniq("nobody") + "@example.com")
        self.assertEqual(wrong.exception.status_code, unknown.exception.status_code)
        self.assertEqual(wrong.exception.detail, unknown.exception.detail)

    def test_an_unknown_account_is_never_locked_out(self):
        """There is nothing to count against, and it must not raise 429."""
        for _ in range(attempt_limits.MAX_FAILED_ATTEMPTS + 3):
            with self.assertRaises(HTTPException) as error:
                self.attempt("wrong", email="no-such-user@example.com")
            self.assertEqual(error.exception.status_code, 401)

    def test_attempts_too_close_together_are_refused(self):
        with self.assertRaises(HTTPException):
            self.attempt("wrong")
        with self.assertRaises(HTTPException) as error:
            routes_auth.login(
                body=SimpleNamespace(email=self.email, password="wrong"), db=self.db)
        self.assertEqual(error.exception.status_code, 429)

    def test_a_successful_login_clears_the_counter(self):
        with self.assertRaises(HTTPException):
            self.attempt("wrong")
        self.attempt(PASSWORD)
        self.db.refresh(self.user)
        self.assertEqual(self.user.failed_auth_attempts, 0)
        self.assertIsNone(self.user.auth_locked_until)

    def test_an_expired_lockout_lets_them_back_in(self):
        self.user.failed_auth_attempts = attempt_limits.MAX_FAILED_ATTEMPTS
        self.user.auth_locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.flush()
        self.assertTrue(self.attempt(PASSWORD).access_token)

    def test_the_unified_route_is_covered_too(self):
        for _ in range(attempt_limits.MAX_FAILED_ATTEMPTS):
            self.user.last_auth_attempt_at = None
            self.db.flush()
            with self.assertRaises(HTTPException) as error:
                routes_auth.login_unified(
                    body=SimpleNamespace(email=self.email, password="wrong"), db=self.db)
        self.assertEqual(error.exception.status_code, 429)


class DriverLoginAttemptLimitTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        user = User(email=_uniq("agg") + "@example.com", hashed_password="x",
                    role="aggregator")
        self.db.add(user)
        self.db.flush()
        port_id = self.db.query(Port.id).order_by(Port.id).limit(1).scalar()
        profile = AggregatorProfile(user_id=user.id, company_name=_uniq("Cabs"),
                                    operating_port_id=port_id)
        self.db.add(profile)
        self.db.flush()

        self.email = _uniq("driver") + "@example.com"
        self.driver = Driver(aggregator_id=profile.id, name="Ramesh",
                             email=self.email, phone=_uniq("+9100")[:20],
                             hpid=_uniq("DRV"), vehicle_number=_uniq("AP39")[:64],
                             hashed_password=hash_password(PASSWORD))
        self.db.add(self.driver)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def test_the_driver_route_is_covered_too(self):
        for _ in range(attempt_limits.MAX_FAILED_ATTEMPTS):
            self.driver.last_auth_attempt_at = None
            self.db.flush()
            with self.assertRaises(HTTPException) as error:
                asyncio.run(routes_drivers.driver_login(
                    body=SimpleNamespace(email=self.email, password="wrong"),
                    db=self.db))
        self.assertEqual(error.exception.status_code, 429)
        self.db.refresh(self.driver)
        self.assertIsNotNone(self.driver.auth_locked_until)


if __name__ == "__main__":
    unittest.main()
