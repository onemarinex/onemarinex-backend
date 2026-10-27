"""Dev seed for the shore-leave cab booking flow (idempotent).

Creates, for "Port of Visakhapatnam":
  - an aggregator provider with an Available driver (guaranteed ride live)
  - a partnered_driver provider whose driver is Offline by default, so
    /crew/cab/ride-availability returns guaranteed-only (spec's initial
    rollout). Set ENABLE_FLEXIBLE=1 to bring the partner driver online.
  - tagged vendors with avg_time_spent_hours (so /itinerary/suggest works)
  - points the test crew profile at the port

Run: PYTHONPATH=. ./.venv/bin/python scripts/seed_shore_leave_dev.py
  (flexible too) ENABLE_FLEXIBLE=1 PYTHONPATH=. ./.venv/bin/python scripts/seed_shore_leave_dev.py
"""
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.session import SessionLocal
from app.db.models.user import User
from app.db.models.port import Port
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.driver import Driver
from app.db.models.vendors import Vendors
from app.db.models.crew_profile import CrewProfile
from app.services.auth import get_password_hash

PORT_NAME = "Port of Visakhapatnam"
PASSWORD = "Test1234!"

# Per the spec's initial rollout, a port enables only the coordinated
# (aggregator) ride; the flexible partner-driver ride appears for crew only
# once partner drivers are live. Keep the partner driver Offline by default so
# /crew/cab/ride-availability returns guaranteed-only. Set ENABLE_FLEXIBLE=1
# to bring the partner driver online when testing the flexible flow.
ENABLE_FLEXIBLE = os.getenv("ENABLE_FLEXIBLE", "0") not in ("0", "", "false", "False")

PROVIDERS = [
    {
        "email": "sl-partner@heyports.dev",
        "company": "Vizag Partner Rides",
        "provider_type": "partnered_driver",
        "driver_status": "Available" if ENABLE_FLEXIBLE else "Offline",
        "driver": {"name": "Suresh Rao", "phone": "+91 9876500001", "vehicle_number": "AP39 PD 1001", "vehicle_type": "AC", "vehicle_name": "Swift Dzire"},
    },
    {
        "email": "sl-test-agg@heyports.dev",
        "company": "Vizag Sea Rides",
        "provider_type": "aggregator",
        "driver_status": "Available",
        "driver": {"name": "Ramesh Kumar", "phone": "+91 9876543210", "vehicle_number": "AP39 AB 1234", "vehicle_type": "XL", "vehicle_name": "Innova Crysta"},
    },
]

VENDORS = [
    ("Oceanic Restaurant", "restaurant", 4.6, 5.2, ["food"], 1.25, 700, "11:00-23:00"),
    ("Rockstar Burgers & Shakes", "restaurant", 4.4, 3.8, ["food"], 0.75, 500, "12:00-23:00"),
    ("Sakura Sushi Lounge", "restaurant", 4.8, 6.1, ["food"], 1.5, 1900, "12:00-22:30"),
    ("Harbour Pub", "pub", 4.3, 4.0, ["pubs", "nightlife"], 0.75, 900, "16:00-01:00"),
    ("Moonlight Brewhouse", "pub", 4.5, 7.3, ["pubs", "nightlife"], 1.5, 1100, "17:00-01:00"),
    ("Kailasagiri Hill Top", "sightseeing", 4.7, 9.5, ["sightseeing"], 1.25, 300, "09:00-20:00"),
    ("Beach View Point", "sightseeing", 4.5, 6.8, ["sightseeing", "relax"], 0.5, 50, "06:00-21:00"),
    ("INS Kursura Submarine Museum", "sightseeing", 4.8, 7.0, ["sightseeing"], 1.0, 150, "10:00-19:00"),
    ("City Center Market", "sightseeing", 4.2, 8.2, ["shopping"], 0.5, 1200, "10:00-22:00"),
    ("Rishikonda Beach Vista", "sightseeing", 4.6, 12.5, ["sightseeing", "relax"], 1.0, 50, "06:00-19:00"),
    ("Sea Pearl Spa & Stay", "hotel", 4.4, 5.5, ["relax"], 2.0, 2500, "09:00-21:00"),
    ("Port SIM & Forex Point", "sightseeing", 4.1, 1.2, ["sim_card", "currency"], 0.25, 0, "09:00-21:00"),
]


def get_or_create_user(db, email, role):
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, hashed_password=get_password_hash(PASSWORD), role=role)
        db.add(user)
        db.flush()
        print(f"created user {email} ({role})")
    return user


def main():
    db = SessionLocal()
    try:
        port = db.query(Port).filter(Port.name == PORT_NAME).first()
        if not port:
            raise SystemExit(f"Port {PORT_NAME!r} not found — seed ports first")

        for spec in PROVIDERS:
            user = get_or_create_user(db, spec["email"], "aggregator")
            profile = db.query(AggregatorProfile).filter(AggregatorProfile.user_id == user.id).first()
            if not profile:
                profile = AggregatorProfile(
                    user_id=user.id,
                    company_name=spec["company"],
                    provider_type=spec["provider_type"],
                    operating_port_id=port.id,
                    status="Active",
                )
                db.add(profile)
                db.flush()
                print(f"created provider {spec['company']} ({spec['provider_type']})")
            else:
                profile.provider_type = spec["provider_type"]
                profile.operating_port_id = port.id
                profile.status = "Active"

            d = spec["driver"]
            driver_status = spec["driver_status"]
            driver = db.query(Driver).filter(Driver.aggregator_id == profile.id, Driver.vehicle_number == d["vehicle_number"]).first()
            if not driver:
                db.add(Driver(
                    aggregator_id=profile.id,
                    name=d["name"], phone=d["phone"], email=spec["email"],
                    vehicle_number=d["vehicle_number"], vehicle_type=d["vehicle_type"],
                    vehicle_name=d["vehicle_name"], status=driver_status,
                    hashed_password=get_password_hash(PASSWORD),
                ))
                print(f"created driver {d['name']} ({d['vehicle_type']}, {driver_status})")
            else:
                driver.status = driver_status
                print(f"updated driver {d['name']} -> {driver_status}")

        for name, category, rating, dist, tags, hrs, price, timings in VENDORS:
            if db.query(Vendors).filter(Vendors.name == name, Vendors.port_id == port.id).first():
                continue
            db.add(Vendors(
                port_id=port.id, name=name, location_name=f"{name}, Visakhapatnam",
                distance_from_port=dist, rating=rating, lat=17.7, lng=83.3,
                phone="+91 8912345678", email="venue@heyports.dev", status="Active",
                category=category,
                other_information={"tags": tags, "avg_time_spent_hours": hrs, "price_per_person": price, "timings": timings},
            ))
            print(f"created vendor {name}")

        for crew in db.query(CrewProfile).join(User).filter(User.email == "sl-test-crew@heyports.dev").all():
            crew.current_port = PORT_NAME

        db.commit()
        print("done")
    finally:
        db.close()


if __name__ == "__main__":
    main()
