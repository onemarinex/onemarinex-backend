from datetime import datetime
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
    return [
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
