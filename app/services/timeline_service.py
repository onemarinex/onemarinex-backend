from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.models.booking_timeline import BookingTimeline, TimelineEventType


EVENT_LABELS = {
    TimelineEventType.BOOKING_CREATED: "Booking Created",
    TimelineEventType.PROVIDER_NOTIFIED: "Provider Notified",
    TimelineEventType.PROVIDER_ACCEPTED: "Provider Accepted",
    TimelineEventType.PROVIDER_REJECTED: "Provider Rejected",
    TimelineEventType.DRIVER_ASSIGNED: "Driver Assigned",
    TimelineEventType.DRIVER_ACCEPTED: "Driver Accepted",
    TimelineEventType.TRIP_STARTED: "Trip Started",
    TimelineEventType.TRIP_COMPLETED: "Trip Completed",
    TimelineEventType.TRIP_CANCELLED: "Trip Cancelled",
}

STOP_EVENT_LABELS = {
    "pickup": "Pickup Point",
    "drop": "Drop Point",
    "waypoint": "Waypoint",
    "facility": "Facility Stop",
    "custom": "Driver Added Stop",
}


def create_timeline_event(
    db: Session,
    *,
    booking_db_id: int,
    event_type: TimelineEventType,
    actor_id: Optional[int] = None,
    actor_type: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    event_time: Optional[datetime] = None,
) -> BookingTimeline:
    entry = BookingTimeline(
        booking_id=booking_db_id,
        event_type=event_type.value,
        event_time=event_time or datetime.utcnow(),
        actor_id=actor_id,
        actor_type=actor_type,
        event_metadata=metadata,
    )
    db.add(entry)
    return entry


def get_booking_timeline(db: Session, booking_db_id: int) -> List[Dict[str, Any]]:
    entries = (
        db.query(BookingTimeline)
        .filter(BookingTimeline.booking_id == booking_db_id)
        .order_by(BookingTimeline.event_time.asc(), BookingTimeline.id.asc())
        .all()
    )
    events: List[Dict[str, Any]] = [
        {
            "id": entry.id,
            "event_type": entry.event_type,
            "event_label": EVENT_LABELS.get(TimelineEventType(entry.event_type), entry.event_type),
            "event_time": entry.event_time,
            "actor_id": entry.actor_id,
            "actor_type": entry.actor_type,
            "metadata": entry.event_metadata,
            "created_at": entry.created_at,
        }
        for entry in entries
    ]

    from app.db.models.driver_magic_link import DriverMagicLink
    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.booking_id == booking_db_id)
        .first()
    )
    if magic_link and magic_link.itinerary_stops:
        for stop_idx, stop in enumerate(magic_link.itinerary_stops):
            stop_type = (stop.get("type") or stop.get("stop_type") or "custom").lower()
            events.append({
                "id": -(abs(hash(str(stop.get("name", "")) + str(stop.get("address", "")))) % 1000000),
                "event_type": f"STOP_{stop_type.upper()}",
                "event_label": STOP_EVENT_LABELS.get(stop_type, stop.get("name", "Stop")),
                "event_time": stop.get("reached_at") or stop.get("created_at"),
                "actor_id": None,
                "actor_type": "stop",
                "metadata": {
                    "name": stop.get("name"),
                    "address": stop.get("address"),
                    "latitude": stop.get("latitude"),
                    "longitude": stop.get("longitude"),
                    "stop_type": stop_type,
                    "reached": stop.get("reached", False),
                },
                "created_at": stop.get("created_at"),
                "_sort_order": 1000 + stop_idx,
            })

    def _to_naive(dt_val):
        if dt_val is None:
            return datetime.min
        if isinstance(dt_val, str):
            try:
                dt_val = datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return datetime.min
        if isinstance(dt_val, datetime) and dt_val.tzinfo is not None:
            return dt_val.replace(tzinfo=None)
        if isinstance(dt_val, datetime):
            return dt_val
        return datetime.min

    def _sort_key(e):
        if "_sort_order" in e:
            return (1, e["_sort_order"])
        return (0, _to_naive(e.get("event_time") or e.get("created_at")))

    events.sort(key=_sort_key, reverse=False)
    return events
