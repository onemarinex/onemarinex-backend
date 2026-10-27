"""The ride OTP could be guessed without limit on the driver route.

`CrewProfile.ride_otp` is `String(4)` and its own column comment calls it a
lifetime OTP — it is not rotated per ride. Ten thousand values, reused for
every trip that crew member ever takes.

Two routes check it. The public magic-link route grew a cooldown and a
five-failure lockout with the driver-token work. `POST /drivers/rides/{id}/start`
grew nothing: it compared the code and returned 400, for ever. A landed guess
there is not worth one ride, it is that person's OTP from then on — including
on the magic-link route, whose lockout then protects nothing.

The policy now lives in one place and these drive it through both routes, so
a future change cannot protect one and forget the other again.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.api.v1.routes_drivers import StartRideIn, start_ride
from app.db.models.cab_booking import BookingStatus, CabBooking, VehicleType
from app.db.models.crew_profile import CrewProfile
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.driver import Driver
from app.db.models.user import User
from app.db.session import engine
from app.services import attempt_limits


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _run(coro):
    import asyncio
    return asyncio.run(coro)


class RideStartOtpLimitTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        crew_user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
        self.db.add(crew_user)
        self.db.flush()
        self.crew = CrewProfile(user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                                nationality="IN", hpid=_uniq("HP"), ride_otp="4821")
        self.db.add(self.crew)
        self.aggregator_id = self.make_aggregator()
        self.driver = self.make_driver("Ramesh")
        self.db.flush()

        self.booking = CabBooking(
            booking_id=_uniq("CAB"), crew_id=self.crew.id, driver_id=self.driver.id,
            pickup_address="Gate", pickup_lat=0, pickup_lng=0,
            drop_address="City", drop_lat=0, drop_lng=0,
            vehicle_type=VehicleType.AC, vehicle_name="Sedan", estimated_price=100,
            distance_km=5, status=BookingStatus.DRIVER_ASSIGNED,
        )
        self.db.add(self.booking)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_aggregator(self):
        from app.db.models.port import Port
        user = User(email=_uniq("agg") + "@example.com", hashed_password="x", role="aggregator")
        self.db.add(user)
        self.db.flush()
        port_id = self.db.query(Port.id).order_by(Port.id).limit(1).scalar()
        profile = AggregatorProfile(user_id=user.id, company_name=_uniq("Cabs"),
                                    operating_port_id=port_id)
        self.db.add(profile)
        self.db.flush()
        return profile.id

    def make_driver(self, name):
        driver = Driver(aggregator_id=self.aggregator_id, name=name,
                        phone=_uniq("+9100")[:20], hpid=_uniq("DRV"),
                        vehicle_number=_uniq("AP39")[:64])
        self.db.add(driver)
        self.db.flush()
        return driver

    def guess(self, otp):
        """One attempt, with the cooldown wound back so only the count is under test."""
        self.booking.otp_last_attempt_at = None
        self.db.flush()
        return _run(start_ride(
            ride_id=self.booking.id, body=StartRideIn(otp=otp),
            db=self.db, current_driver=self.driver,
        ))

    def test_the_right_otp_starts_the_ride(self):
        result = self.guess("4821")
        self.assertEqual(result["message"], "Ride started")

    def test_a_wrong_otp_is_counted(self):
        with self.assertRaises(HTTPException):
            self.guess("0000")
        self.db.refresh(self.booking)
        self.assertEqual(self.booking.otp_failed_attempts, 1)

    def test_guessing_is_locked_out_after_five_tries(self):
        for _ in range(attempt_limits.MAX_FAILED_ATTEMPTS - 1):
            with self.assertRaises(HTTPException) as error:
                self.guess("0000")
            self.assertEqual(error.exception.status_code, 400)

        with self.assertRaises(HTTPException) as error:
            self.guess("0000")
        self.assertEqual(error.exception.status_code, 429)
        self.assertIn("Retry-After", error.exception.headers)

        # And the lockout holds even for the correct code, so a guesser who
        # lands on the last try gains nothing.
        with self.assertRaises(HTTPException) as error:
            self.guess("4821")
        self.assertEqual(error.exception.status_code, 429)

    def test_attempts_too_close_together_are_refused(self):
        with self.assertRaises(HTTPException):
            self.guess("0000")
        # No winding back this time: the second attempt is immediate.
        with self.assertRaises(HTTPException) as error:
            _run(start_ride(ride_id=self.booking.id, body=StartRideIn(otp="0001"),
                            db=self.db, current_driver=self.driver))
        self.assertEqual(error.exception.status_code, 429)

    def test_an_expired_lockout_lets_the_driver_try_again(self):
        self.booking.otp_failed_attempts = attempt_limits.MAX_FAILED_ATTEMPTS
        self.booking.otp_locked_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.db.flush()
        self.assertEqual(self.guess("4821")["message"], "Ride started")

    def test_a_successful_start_clears_the_counter(self):
        with self.assertRaises(HTTPException):
            self.guess("0000")
        self.guess("4821")
        self.db.refresh(self.booking)
        self.assertEqual(self.booking.otp_failed_attempts, 0)
        self.assertIsNone(self.booking.otp_locked_until)

    def test_another_driver_cannot_reach_the_ride_at_all(self):
        other = self.make_driver("Suresh")
        with self.assertRaises(HTTPException) as error:
            _run(start_ride(ride_id=self.booking.id, body=StartRideIn(otp="4821"),
                            db=self.db, current_driver=other))
        self.assertEqual(error.exception.status_code, 404)


class SharedPolicyTests(unittest.TestCase):
    """The counters are the same three fields on both rows that carry them."""

    def test_both_carriers_expose_the_fields_the_policy_needs(self):
        from app.db.models.driver_magic_link import DriverMagicLink

        for model in (CabBooking, DriverMagicLink):
            for field in ("otp_failed_attempts", "otp_last_attempt_at", "otp_locked_until"):
                self.assertTrue(hasattr(model, field), f"{model.__name__} lacks {field}")

    def test_a_lockout_is_not_cleared_by_merely_asking(self):
        row = SimpleNamespace(
            otp_failed_attempts=5,
            otp_last_attempt_at=None,
            otp_locked_until=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        with self.assertRaises(HTTPException):
            attempt_limits.assert_attempt_allowed(row, now=datetime.now(timezone.utc))
        self.assertEqual(row.otp_failed_attempts, 5)

    def test_a_naive_timestamp_is_read_as_utc_not_as_local_time(self):
        """These columns are written naive in places, and reading one as local
        time shifts every lockout by the server's offset — which is why this
        asserts the conversion rather than a 429 that a machine running on UTC
        would produce either way.
        """
        naive = datetime(2026, 10, 26, 12, 0, 0)
        converted = attempt_limits._aware_utc(naive)

        self.assertEqual(converted.tzinfo, timezone.utc)
        # Same wall clock, labelled UTC. `astimezone` would move the hands.
        self.assertEqual(converted.replace(tzinfo=None), naive)

    def test_an_aware_timestamp_is_left_alone(self):
        aware = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        self.assertEqual(attempt_limits._aware_utc(aware), aware)


if __name__ == "__main__":
    unittest.main()
