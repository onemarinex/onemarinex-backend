"""Server copy of the read-only company demonstration records.

Provenance: heyports-frontend/src/components/ShippingCompany/Demo/demoData.ts,
createDemoData and CALL_SETUP, copied 2026-10-29. Names, bookings, statuses,
seats, survey answers and ratings retain those source values. Numeric IDs are
transport aliases only. No real company, crew, case or booking rows are read.

Explicit fictional extensions: stable person_key by source ordinal across
months, plus generated event-time metadata inside the copied call interval.
These make date filters and shared respondent statistics testable. Original
source values remain unchanged; taxi minutes are not treated as time ashore.
"""
from datetime import datetime, timedelta, timezone

FIXTURE_VERSION = "company-demo-2026.10.29.2"
PORTS = [
    {"id": 1, "name": "Visakhapatnam", "country_code": "IN", "country_name": "India"},
    {"id": 2, "name": "Chennai", "country_code": "IN", "country_name": "India"},
    {"id": 3, "name": "Cochin", "country_code": "IN", "country_name": "India"},
]
VESSELS = [
    {"id": 1, "key": "coral", "name": "Coral", "reference": "VSL-001", "roles": ["Owner", "Commercial Manager"]},
    {"id": 2, "key": "harbour", "name": "Harbour", "reference": "VSL-002", "roles": ["ISM Manager"]},
    {"id": 3, "key": "beacon", "name": "Beacon", "reference": "VSL-003", "roles": ["Owner", "ISM Manager"]},
    {"id": 4, "key": "horizon", "name": "Horizon", "reference": "VSL-004", "roles": ["Commercial Manager"]},
]
SETUP = [
    {"port_id": 1, "bookings": 22, "running": 2, "completed": 14, "seats": 3, "running_seats": 4},
    {"port_id": 2, "bookings": 20, "running": 2, "completed": 12, "seats": 3, "running_seats": 5},
    {"port_id": 1, "bookings": 14, "running": 0, "completed": 10, "seats": 3, "running_seats": 3},
    {"port_id": 3, "bookings": 16, "running": 0, "completed": 16, "seats": 2, "running_seats": 2},
]


def month_start(anchor, offset=0):
    index = anchor.year * 12 + anchor.month - 1 + offset
    return datetime(index // 12, index % 12 + 1, 1, tzinfo=timezone.utc)


def fictional_event_time(call, anchor, index, total):
    """Generated fiction, not recovered source timestamps. Existing TS fixture
    has only month tags. This explicit metadata enables exact date filtering.
    """
    start = datetime.fromisoformat(call["started_at"])
    stop = min(datetime.fromisoformat(call["ended_at"]) if call["ended_at"] else anchor, anchor)
    seconds = max(0, (stop - start).total_seconds())
    return start + timedelta(seconds=seconds * (index + 1) / (total + 1))


def create_demo_data(anchor):
    anchor = datetime(anchor.year, anchor.month, 28, 18, tzinfo=timezone.utc)
    months = [month_start(anchor, offset).strftime("%Y-%m") for offset in (-2, -1, 0)]
    calls, trips, surveys, reviews = [], [], [], []
    for period in months:
        for vi, vessel in enumerate(VESSELS):
            setup = SETUP[vi]
            active = period == months[2] and vi < 3
            source_id = f"demo-{period}-{vessel['key']}"
            call = {
                "id": len(calls) + 1, "reference": source_id, "vessel_id": vessel["id"],
                "vessel_name": vessel["name"], "imo_number": None, "port_id": setup["port_id"],
                "port_name": PORTS[setup["port_id"] - 1]["name"], "country_code": "IN", "country_name": "India",
                "agent": "Shore Services", "month": period, "active": active,
                "started_at": f"{period}-01T06:00:00+05:30",
                "ended_at": None if active else f"{period}-{'20' if vi == 3 else '28'}T18:00:00+05:30",
                "status": "ACTIVE" if active else "CLOSED", "timezone": "Asia/Kolkata",
            }
            calls.append(call)
            for i in range(setup["bookings"]):
                running = active and i < setup["running"]
                completed = not active or i < setup["running"] + setup["completed"]
                trips.append({
                    "id": f"DEMO-RIDE-{period}-{vi + 1}-{i + 1}", "call_id": call["id"],
                    "vessel_id": vessel["id"], "month": period,
                    "status": "In progress" if running else "Completed" if completed else "Scheduled",
                    "seats": setup["running_seats"] if running else setup["seats"],
                    "minutes": 60 + i % 4 * 30 if completed and not running else None,
                    "created_at": fictional_event_time(call, anchor, i, setup["bookings"]).isoformat(),
                    "trip_started_at": fictional_event_time(call, anchor, i, setup["bookings"]).isoformat() if running or completed else None,
                })
        for i in range(40):
            vessel = VESSELS[i % 4]
            call_id = next(c["id"] for c in calls if c["month"] == period and c["vessel_id"] == vessel["id"])
            call = next(c for c in calls if c["id"] == call_id)
            experience = fictional_event_time(call, anchor, i // 4, 10).isoformat()
            barriers = ["Port access restriction"] if i < 3 else ["Transport unavailable"] if i < 6 else ["Agent delay", "Port processing delay"] if i < 10 else ["Transport cost"] if i < 12 else []
            surveys.append({
                "id": f"DEMO-SURVEY-{period}-{i + 1}", "call_id": call_id,
                "vessel_id": vessel["id"], "month": period, "ordinal": i + 1,
                "person_key": f"demo-person-{i+1}", "experience_at": experience,
                "went_ashore": i >= 6, "barriers": barriers,
                "extra_charge": "Paid" if 6 <= i < 8 else "Requested, not paid" if 8 <= i < 10 else "Not requested",
                "satisfaction": None if i < 6 else 3 if i < 12 else 4 if i % 3 == 0 else 5,
                "comment": "Shore leave was not possible." if i < 6 else "Shore leave was delayed." if i < 12 else "Transport and facilities were accessible.",
            })
            driver = i < 26
            position = i if driver else i - 26
            reviews.append({
                "id": f"DEMO-REVIEW-{period}-{i + 1}", "call_id": call_id,
                "vessel_id": vessel["id"], "month": period, "ordinal": i + 1,
                "person_key": f"demo-person-{i+1}", "created_at": experience,
                "type": "driver" if driver else "facility_stop",
                "provider": "Port Cabs" if driver else "Seafarers Centre",
                "rating": (5 if position < 18 else 4 if position < 24 else 3) if driver else (5 if position < 6 else 4 if position < 12 else 3),
                "comment": "Pickup was clear and the return was on time." if driver else "The centre was accessible during shore leave.",
            })
    for call in calls:
        own = [trip for trip in trips if trip["call_id"] == call["id"]]
        completed = [trip for trip in own if trip["status"] == "Completed"]
        start = datetime.fromisoformat(call["started_at"])
        stop = min(datetime.fromisoformat(call["ended_at"]) if call["ended_at"] else anchor,anchor)-timedelta(hours=12)
        for index, trip in enumerate(own):
            departed = None if trip["status"] == "Scheduled" else anchor-timedelta(minutes=30+index*20) if trip["status"] == "In progress" else start+(stop-start)*(completed.index(trip)+1)/(len(completed)+1)
            trip["trip_started_at"] = departed.isoformat() if departed else None
            trip["ashore_departed_at"] = trip["trip_started_at"]
            trip["ashore_returned_at"] = (departed+timedelta(minutes=trip["minutes"])).isoformat() if trip["status"] == "Completed" else None
            trip["person_keys"] = [f"{trip['vessel_id']}-person-{(index*trip['seats']+seat)%18+1}" for seat in range(trip["seats"])]
    return {"snapshot":anchor.isoformat(),"months": months, "month": months[2], "calls": calls, "trips": trips, "surveys": surveys, "reviews": reviews}
