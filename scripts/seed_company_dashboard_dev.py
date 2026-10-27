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

`--populate` additionally makes each dashboard show NUMBERS.

The five boundary moments above are what prove the handover is attributed
correctly, and they are deliberately few. But the suppression threshold is 5,
and those five split two/three across the handover -- so every figure on both
dashboards reads "Withheld", which is correct and demonstrates nothing. A
reader sees an empty product.

`--populate` adds enough distinct crew, assignments, shore passes and
completed trips on each side of the handover to clear the threshold, so the
figures resolve to real counts. The boundary moments are untouched, so what
the script proves does not change -- it just becomes visible.

Usage (from onemarinex-backend/):
    PYTHONPATH=. python scripts/seed_company_dashboard_dev.py
    PYTHONPATH=. python scripts/seed_company_dashboard_dev.py --populate
    PYTHONPATH=. python scripts/seed_company_dashboard_dev.py --reset --populate
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


# Enough to clear the suppression threshold of 5 on each side of the handover
# with room to spare, so a figure reads as a count rather than as "Withheld".
POPULATE_PER_SIDE = 7

# Inside each company's own window, well clear of the boundary so these add
# volume without weakening what the boundary moments prove.
POPULATE_WINDOWS = {
    "alpha": datetime(2026, 2, 10, 8, 0, tzinfo=timezone.utc),
    "bravo": datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc),
}


def _populate(db, vessel, call, made: int) -> int:
    """Distinct crew, assignments, shore passes and trips on each side.

    Distinct *people*, not repeated trips by one: several metrics count
    distinct crew, and a single crew member taking seven taxis is one person
    ashore, not seven.
    """
    from app.db.models.crew_assignment import CrewAssignment
    from app.db.models.crew_profile import CrewProfile
    from app.db.models.shore_pass import ShorePass

    for side, base in POPULATE_WINDOWS.items():
        for n in range(POPULATE_PER_SIDE):
            moment = base + timedelta(days=n)
            tag = f"{side}-{n:02d}"

            user, _ = _get_or_create(
                db, User, email=f"seed-crew-{tag}@example.com",
                defaults=dict(hashed_password=get_password_hash(PASSWORD),
                              role="crew", name=f"Seed Crew {tag}"),
            )
            db.flush()
            profile, new_profile = _get_or_create(
                db, CrewProfile, user_id=user.id,
                defaults=dict(full_name=f"Seed Crew {tag}", rank="able_seaman",
                              nationality="IN", hpid=f"SEEDHP-{tag.upper()}"),
            )
            made += int(new_profile)
            db.flush()

            assignment, new_assignment = _get_or_create(
                db, CrewAssignment, vessel_call_id=call.id, crew_profile_id=profile.id,
                defaults=dict(crew_name=profile.full_name, rank="able_seaman",
                              nationality="IN", shore_pass_eligible=True,
                              started_at=moment, source="MANIFEST"),
            )
            made += int(new_assignment)
            db.flush()

            # A recorded exit, which is what `actually_ashore` counts -- one
            # per person, never a seat count.
            _, new_pass = _get_or_create(
                db, ShorePass, shore_pass_id=f"SEEDSP-{tag.upper()}",
                defaults=dict(vessel_call_id=call.id, crew_profile_id=profile.id,
                              crew_assignment_id=assignment.id,
                              out_time=moment + timedelta(hours=1)),
            )
            made += int(new_pass)

            _, new_booking = _get_or_create(
                db, CabBooking, booking_id=f"SEED-{tag.upper()}",
                defaults=dict(
                    vessel_id=vessel.id, vessel_call_id=call.id,
                    crew_id=profile.id, crew_assignment_id=assignment.id,
                    pickup_address="Gate", pickup_lat=0, pickup_lng=0,
                    drop_address="City", drop_lat=0, drop_lng=0,
                    vehicle_type="ac", vehicle_name="Sedan", estimated_price=100,
                    distance_km=5, status="completed", created_at=moment,
                    trip_started_at=moment + timedelta(minutes=20),
                    trip_completed_at=moment + timedelta(hours=1),
                ),
            )
            made += int(new_booking)
    return made


def seed(reset: bool, populate: bool = False) -> int:
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

        if populate:
            made = _populate(db, vessel, call, made)

        db.commit()
        print(f"\n  {len(EVENT_TIMES)} event moments across the handover, {made} row(s) created")
        if populate:
            print(f"  populated: {POPULATE_PER_SIDE} distinct crew each side, "
                  "so figures clear the suppression threshold of 5")
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
    parser.add_argument("--populate", action="store_true",
                        help="add enough crew and trips that figures show "
                             "counts instead of Withheld")
    args = parser.parse_args()
    sys.exit(seed(args.reset, args.populate))
