"""One brute-force policy for every secret this application checks.

The ride OTP is `CrewProfile.ride_otp`: **four digits, and the column comment
calls it a lifetime OTP** — it is not rotated per ride. Ten thousand values,
reused for every trip that crew member ever takes.

Two routes check it. The public magic-link route grew a proper lockout with
the driver-token work. `POST /drivers/rides/{ride_id}/start` — the
authenticated driver app — grew nothing, so the same four digits could be
guessed there without limit, and a guess that lands is not good for one ride:
it is that person's OTP from then on.

It also covers passwords, which had nothing at all. `POST /auth/login`,
`/auth/login-unified` and `/drivers/login` compared a password and returned
401, with no counter, no cooldown and no lockout — unlimited guesses against
any account, superadmin included.

The policy lives here rather than in each route, because a security control
with four implementations has one implementation and three liabilities. It
operates on any row carrying a counter, a last-attempt time and a lockout
time, under whatever names that row uses:

    DriverMagicLink, CabBooking   otp_failed_attempts / otp_last_attempt_at / otp_locked_until
    User, Driver                  failed_auth_attempts / last_auth_attempt_at / auth_locked_until

State is persisted rather than in-process: the app runs more than one worker,
and a counter in memory gives an attacker one budget per instance.

**Not fixed here, and worth an owner decision:** a four-digit lifetime OTP is
weak whatever the rate limit. Rotating it per ride would make a landed guess
worth one trip instead of all of them.

**A known tradeoff:** answering 429 to a locked-out login tells the caller the
account exists, where 401 would not. Every account lockout has this property.
The alternative is to keep answering 401 while locked, which hides the account
but leaves a legitimate person with no way to tell a wrong password from a
lockout. Saying so is the better trade against an unlimited guessing surface,
and the lockout is short.
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from fastapi import HTTPException

MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION = timedelta(minutes=15)
ATTEMPT_COOLDOWN = timedelta(seconds=1)


class Fields(NamedTuple):
    """What the three counters are called on a particular row."""

    failed: str
    last_attempt: str
    locked_until: str


OTP = Fields("otp_failed_attempts", "otp_last_attempt_at", "otp_locked_until")
AUTH = Fields("failed_auth_attempts", "last_auth_attempt_at", "auth_locked_until")


def _aware_utc(value: datetime) -> datetime:
    """Treat a naive timestamp as UTC, which is how these columns are written."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def assert_attempt_allowed(row, *, now: datetime, fields: Fields = OTP) -> None:
    """Refuse an attempt that is locked out or too soon after the last one.

    Clears an expired lockout as a side effect, so the next attempt starts
    from zero rather than staying locked forever.
    """
    locked_until = getattr(row, fields.locked_until, None)
    if locked_until and _aware_utc(locked_until) > now:
        retry_after = max(1, int((_aware_utc(locked_until) - now).total_seconds()))
        raise HTTPException(
            status_code=429,
            detail="Too many invalid OTP attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    if locked_until:
        setattr(row, fields.failed, 0)
        setattr(row, fields.locked_until, None)

    last_attempt_at = getattr(row, fields.last_attempt, None)
    if last_attempt_at:
        retry_in = ATTEMPT_COOLDOWN - (now - _aware_utc(last_attempt_at))
        if retry_in.total_seconds() > 0:
            raise HTTPException(
                status_code=429,
                detail="Please wait before trying the OTP again.",
                headers={"Retry-After": str(max(1, int(retry_in.total_seconds()) + 1))},
            )

    setattr(row, fields.last_attempt, now)


def record_failure(row, *, now: datetime, fields: Fields = OTP) -> bool:
    """Count a wrong secret. True once this attempt has tripped the lockout."""
    failed_attempts = (getattr(row, fields.failed, 0) or 0) + 1
    setattr(row, fields.failed, failed_attempts)
    if failed_attempts >= MAX_FAILED_ATTEMPTS:
        setattr(row, fields.locked_until, now + LOCKOUT_DURATION)
        return True
    return False


def record_success(row, *, fields: Fields = OTP) -> None:
    setattr(row, fields.failed, 0)
    setattr(row, fields.locked_until, None)


def locked_out_error() -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many invalid OTP attempts. Try again later.",
        headers={"Retry-After": str(int(LOCKOUT_DURATION.total_seconds()))},
    )
