from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, JSON, func
from sqlalchemy.orm import relationship
import enum

from app.db.base import Base


class TimelineEventType(str, enum.Enum):
    BOOKING_CREATED = "BOOKING_CREATED"
    PROVIDER_NOTIFIED = "PROVIDER_NOTIFIED"
    PROVIDER_ACCEPTED = "PROVIDER_ACCEPTED"
    PROVIDER_REJECTED = "PROVIDER_REJECTED"
    DRIVER_ASSIGNED = "DRIVER_ASSIGNED"
    DRIVER_ACCEPTED = "DRIVER_ACCEPTED"
    TRIP_STARTED = "TRIP_STARTED"
    TRIP_COMPLETED = "TRIP_COMPLETED"
    TRIP_CANCELLED = "TRIP_CANCELLED"


class BookingTimeline(Base):
    __tablename__ = "booking_timeline"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    event_time = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    actor_id = Column(Integer, nullable=True)
    actor_type = Column(String(32), nullable=True)
    event_metadata = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking = relationship("CabBooking", back_populates="timeline_events")
