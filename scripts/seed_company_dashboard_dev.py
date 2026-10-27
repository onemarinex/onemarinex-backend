"""Seed a shipping company, its members and a handover, for local testing.

The dashboard's hard cases are all about time: an event before a company took
over, an event after it left, and an event on the boundary instant. None of
them can be exercised without data on both sides of a handover, so this builds
exactly that:

    MV SEEDLINE
      Alpha Marine   ISM  [1 Jan 2026, 1 Jul 2026)
      Bravo Shipping ISM  [1 Jul 2026, open)

    bookings and trips in March, in the handover week, and in September
    an SOS in each half

Every figure the dashboard shows should then differ between the two companies,
and anything that does not is a bug worth seeing before production ever does.

Local only. Refuses a non-local DATABASE_URL through the shared seed guard, so
it cannot be pointed at a deployed database.

Idempotent: re-running reuses what exists and adds nothing twice.

Usage (from onemarinex-backend/):
    PYTHONPATH=. python scripts/seed_company_dashboard_dev.py
    PYTHONPATH=. python scripts/seed_company_dashboard_dev.py --reset
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import app.db.base  # noqa: F401 — registers every model on Base

from sqlalchemy import text

from app.db.models.cab_booking import CabBooking
from app.db.models.crew_sos import CrewSos
from app.db.models.shipping_company import (
    CompanyUserMembership,
    SensitiveAccessRequest,
    ShippingCompany,
    VesselRoleClaim,
)
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.seed_guard import refuse_if_remote
from app.db.session import SessionLocal
from app.services.auth import get_password_hash

PASSWORD = "Test1234!"
VESSEL_NAME = "MV SEEDLINE"
IMO = "IMO-SEEDLINE-1"

HANDOVER = datetime(2026, 7, 1, tzinfo=timezone.utc)
START = datetime(2026, 1, 1, tzinfo=timezone.utc)

REVIEWER_EMAIL = "company-review@heyports.dev"
RIVAL_NAME = "Charlie Chartering"

COMPANIES = [
    ("Alpha Marine Management", "alpha-company@heyports.dev", START, HANDOVER),
    ("Bravo Shipping Ltd", "bravo-company@heyports.dev", HANDOVER, None),
]

# Deliberately spread across the handover. The boundary instant belongs to the
# incoming company, because the interval is half-open.
EVENT_TIMES = [
    ("march", datetime(2026, 3, 14, 9, 0, tzinfo=timezone.utc)),
    ("june-last-day", datetime(2026, 6, 30, 23, 0, tzinfo=timezone.utc)),
    ("the-boundary", HANDOVER),
    ("july", datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)),
    ("september", datetime(2026, 9, 9, 15, 0, tzinfo=timezone.utc)),
]


def _get_or_create(db, model, defaults=None, **lookup):
    row = db.query(model).filter_by(**lookup).first()
    if row is not None:
        return row, False
    row = model(**lookup, **(defaults or {}))
    db.add(row)
    db.flush()
    return row, True


def seed(reset: bool) -> int:
    refuse_if_remote("seed the company dashboard")
    db = SessionLocal()
    try:
        vessel, _ = _get_or_create(
            db, Vessel, imo_number=IMO,
            defaults=dict(name=VESSEL_NAME, vessel_type="Bulk Carrier", status="Active"),
        )

        if reset:
            db.execute(text(
                "DELETE FROM vessel_role_claims WHERE vessel_id = :v"), {"v": vessel.id})
            db.execute(text(
                "DELETE FROM cab_bookings WHERE booking_id LIKE 'SEED-%'"))
            db.commit()
            print("  reset: claims and seeded bookings removed")

        call, _ = _get_or_create(
            db, VesselCall, vessel_id=vessel.id, vessel_name=VESSEL_NAME,
            defaults=dict(port_name="port_visakhapatnam"),
        )

        for legal_name, email, start, end in COMPANIES:
            user, made_user = _get_or_create(
                db, User, email=email,
                defaults=dict(
                    hashed_password=get_password_hash(PASSWORD),
                    role="crew", name=legal_name + " Admin",
                ),
            )
            company, made_company = _get_or_create(
                db, ShippingCompany, normalized_name=legal_name.upper(),
                defaults=dict(legal_name=legal_name, status="VERIFIED",
                              verified_at=datetime.now(timezone.utc)),
            )
            _get_or_create(
                db, CompanyUserMembership,
                company_id=company.id, user_id=user.id,
                defaults=dict(role="ADMIN", enabled=True),
            )
            claim, made_claim = _get_or_create(
                db, VesselRoleClaim,
                company_id=company.id, vessel_id=vessel.id, role="ISM_MANAGER",
                defaults=dict(
                    effective_start=start, effective_end=end, status="APPROVED",
                    approved_at=datetime.now(timezone.utc),
                    evidence_type="DOC", evidence_reference=f"SEED-{legal_name[:5].upper()}",
                ),
            )
            window = f"{start:%d %b %Y} -> " + (f"{end:%d %b %Y}" if end else "open")
            print(f"  {legal_name:<28} {email:<30} {window}"
                  + ("  (new)" if made_company or made_claim else ""))

        db.flush()
        made = 0
        for label, moment in EVENT_TIMES:
            booking, is_new = _get_or_create(
                db, CabBooking, booking_id=f"SEED-{label.upper()}",
                defaults=dict(
                    vessel_id=vessel.id, vessel_call_id=call.id,
                    pickup_address="Gate", pickup_lat=0, pickup_lng=0,
                    drop_address="City", drop_lat=0, drop_lng=0,
                    vehicle_type="ac", vehicle_name="Sedan", estimated_price=100,
                    distance_km=5, status="completed", created_at=moment,
                    trip_started_at=moment + timedelta(minutes=20),
                    trip_completed_at=moment + timedelta(hours=1),
                ),
            )
            made += int(is_new)
            sos, sos_new = _get_or_create(
                db, CrewSos, trip_id=f"SEED-{label.upper()}",
                defaults=dict(
                    vessel_id=vessel.id, vessel_call_id=call.id,
                    vessel=VESSEL_NAME, created_at=moment + timedelta(minutes=30),
                    status="RESOLVED",
                ),
            )
            made += int(sos_new)
        # A Super Admin, an open claim that collides with Alpha's, and one
        # unanswered access request -- so /superadmin/company-review has
        # something in both queues and the conflict banner is reachable.
        reviewer, _ = _get_or_create(
            db, User, email=REVIEWER_EMAIL,
            defaults=dict(hashed_password=get_password_hash(PASSWORD),
                          role="superadmin", name="Company Review Admin"),
        )
        rival, _ = _get_or_create(
            db, ShippingCompany, normalized_name=RIVAL_NAME.upper(),
            defaults=dict(legal_name=RIVAL_NAME, status="VERIFIED",
                          verified_at=datetime.now(timezone.utc)),
        )
        db.flush()
        _get_or_create(
            db, VesselRoleClaim,
            company_id=rival.id, vessel_id=vessel.id, role="ISM_MANAGER",
            defaults=dict(
                effective_start=datetime(2026, 4, 1, tzinfo=timezone.utc),
                effective_end=datetime(2026, 10, 1, tzinfo=timezone.utc),
                status="SUBMITTED", evidence_type="DOC",
                evidence_reference="SEED-RIVAL-CONTESTED",
            ),
        )
        alpha = db.query(ShippingCompany).filter(
            ShippingCompany.normalized_name == COMPANIES[0][0].upper()).one()
        alpha_admin = db.query(User).filter(User.email == COMPANIES[0][1]).one()
        _get_or_create(
            db, SensitiveAccessRequest,
            company_id=alpha.id, vessel_call_id=call.id, category="crew_detail",
            defaults=dict(
                requested_start=START, requested_end=HANDOVER,
                reason="Reviewing an incident reported during our tenure",
                requested_by_user_id=alpha_admin.id, status="REQUESTED",
            ),
        )

        db.commit()
        print(f"\n  {len(EVENT_TIMES)} event moments across the handover, {made} row(s) created")
        print(f"  review queues seeded: 1 contested claim, 1 open access request")
        print(f"  Super Admin: {REVIEWER_EMAIL}")
        print(f"  vessel_id={vessel.id}  call_id={call.id}")
        print(f"\n  Sign in as either company admin with: {PASSWORD}")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true",
                        help="remove the seeded claims and bookings first")
    sys.exit(seed(parser.parse_args().reset))
