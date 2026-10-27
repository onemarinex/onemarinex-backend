"""Audit R03 — driver trip links must expire and must be revocable.

The public GET resolved a token and served the trip payload with no expiry, no
revocation and no driver binding. A holder of an old URL kept reading it for as
long as the row existed, and reassigning the driver left the previous one with
a working link and a verified OTP session.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.db.models.driver_magic_link import DriverMagicLink
from app.db.session import engine
from app.services import magic_link_service as mls


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _link(**kwargs):
    """A link object with only the fields the lifecycle rules read."""
    base = dict(
        expires_at=None,
        revoked_at=None,
        created_at=datetime.now(timezone.utc),
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


class ExpiryTests(unittest.TestCase):
    def test_a_fresh_link_is_usable(self):
        mls.assert_link_usable(_link(
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1)
        ))  # must not raise

    def test_an_expired_link_is_gone_not_merely_absent(self):
        with self.assertRaises(HTTPException) as caught:
            mls.assert_link_usable(_link(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
            ))
        self.assertEqual(caught.exception.status_code, 410)
        self.assertIn("expired", caught.exception.detail.lower())

    def test_a_revoked_link_is_refused_even_before_it_expires(self):
        with self.assertRaises(HTTPException) as caught:
            mls.assert_link_usable(_link(
                expires_at=datetime.now(timezone.utc) + timedelta(days=30),
                revoked_at=datetime.now(timezone.utc),
            ))
        self.assertEqual(caught.exception.status_code, 410)
        self.assertIn("revoked", caught.exception.detail.lower())

    # --- the rows that already exist ----------------------------------------

    def test_a_null_expiry_is_not_treated_as_immortal(self):
        """Links created before the column existed are exactly the holders the
        audit was about. They must not be exempt."""
        old = _link(
            expires_at=None,
            created_at=datetime.now(timezone.utc)
            - timedelta(hours=mls.MAGIC_LINK_TTL_HOURS + 1),
        )
        with self.assertRaises(HTTPException) as caught:
            mls.assert_link_usable(old)
        self.assertEqual(caught.exception.status_code, 410)

    def test_a_recent_null_expiry_link_still_works(self):
        """The fallback must not kill links that are legitimately in use."""
        mls.assert_link_usable(_link(expires_at=None))  # created just now

    def test_the_fallback_is_creation_plus_the_configured_ttl(self):
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(
            mls.link_expires_at(_link(created_at=created)),
            created + timedelta(hours=mls.MAGIC_LINK_TTL_HOURS),
        )

    def test_a_naive_timestamp_is_read_as_utc(self):
        """Older rows were written without a timezone."""
        naive = datetime.now() - timedelta(hours=mls.MAGIC_LINK_TTL_HOURS + 2)
        with self.assertRaises(HTTPException):
            mls.assert_link_usable(_link(expires_at=None, created_at=naive))


class RotationTests(unittest.TestCase):
    """Reassigning the driver has to reset authorization, not extend it."""

    def setUp(self):
        self.connection = engine.connect()
        self.transaction = self.connection.begin()
        self.db = Session(bind=self.connection)

        from app.db.models.cab_booking import CabBooking

        self.booking = CabBooking(
            booking_id=_uniq("CAB"),
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type="ac", vehicle_name="Sedan", estimated_price=100,
            distance_km=5, status="driver_assigned",
        )
        self.db.add(self.booking)
        self.db.flush()
        self.link = DriverMagicLink(
            booking_id=self.booking.id,
            token=_uniq("tok"),
            itinerary_stops=[],
            otp_verified_at=datetime.now(timezone.utc),
            otp_failed_attempts=3,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.db.add(self.link)
        self.db.flush()
        self.original_token = self.link.token

    def tearDown(self):
        self.db.close()
        self.transaction.rollback()
        self.connection.close()

    def test_rotation_replaces_the_token_so_the_old_url_resolves_to_nothing(self):
        mls.rotate_magic_link(self.db, self.booking)
        self.db.refresh(self.link)
        self.assertNotEqual(self.link.token, self.original_token)

    def test_rotation_clears_the_previous_otp_session(self):
        """The new driver verifies for themselves; they do not inherit one."""
        mls.rotate_magic_link(self.db, self.booking)
        self.db.refresh(self.link)
        self.assertIsNone(self.link.otp_verified_at)
        self.assertEqual(self.link.otp_failed_attempts, 0)

    def test_rotation_gives_the_new_holder_a_full_window(self):
        mls.rotate_magic_link(self.db, self.booking)
        self.db.refresh(self.link)
        self.assertGreater(
            mls.link_expires_at(self.link),
            datetime.now(timezone.utc) + timedelta(hours=mls.MAGIC_LINK_TTL_HOURS - 1),
        )

    def test_revoking_leaves_the_link_refused(self):
        mls.revoke_magic_link(self.db, self.booking)
        self.db.refresh(self.link)
        with self.assertRaises(HTTPException) as caught:
            mls.assert_link_usable(self.link)
        self.assertEqual(caught.exception.status_code, 410)


class ChokepointTests(unittest.TestCase):
    def test_every_public_handler_resolves_through_a_checked_path(self):
        """Either get_magic_link_by_token, which checks, or an explicit call."""
        import pathlib

        routes = pathlib.Path("app/api/v1/routes_bookings.py").read_text()
        service = pathlib.Path("app/services/magic_link_service.py").read_text()

        # the shared lookup enforces it
        lookup = service[service.index("def get_magic_link_by_token"):]
        self.assertIn("assert_link_usable(magic_link)", lookup)

        # and the one handler that queries the row itself does too
        self.assertIn("assert_link_usable(magic_link)", routes)

    def test_a_genuine_driver_change_rotates(self):
        import pathlib

        routes = pathlib.Path("app/api/v1/routes_bookings.py").read_text()
        self.assertIn("rotate_magic_link(db, booking)", routes)
        self.assertIn("already_assigned_driver_id != driver.id", routes)


if __name__ == "__main__":
    unittest.main()
