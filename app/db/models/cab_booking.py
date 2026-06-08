from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, Enum as SQLEnum, Numeric
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.db.base import Base


class BookingStatus(str, enum.Enum):
    PENDING_PROVIDER_RESPONSE = "pending_provider_response"
    PROVIDER_ACCEPTED = "provider_accepted"
    PROVIDER_REJECTED = "provider_rejected"
    DRIVER_ASSIGNED = "driver_assigned"
    DRIVER_ACCEPTED = "driver_accepted"
    ON_TRIP = "on_trip"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    # Legacy statuses kept for backward compatibility
    PENDING = "pending"
    CONFIRMED = "confirmed"
    ARRIVED = "arrived"
    IN_PROGRESS = "in_progress"


class RideType(str, enum.Enum):
    FLEXIBLE_RIDE = "flexible_ride"
    GUARANTEED_COORDINATED_RIDE = "guaranteed_coordinated_ride"


class VehicleType(str, enum.Enum):
    AC = "ac"
    PREMIUM = "premium"
    XL = "xl"


class CabBooking(Base):
    __tablename__ = "cab_bookings"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(String, unique=True, index=True, nullable=False)
    port = Column(String(255), nullable=True, index=True)

    crew_id = Column(Integer, ForeignKey("crew_profiles.id"), nullable=False)
    crew = relationship("CrewProfile", back_populates="cab_bookings")

    pickup_address = Column(String, nullable=False)
    pickup_lat = Column(Float, nullable=False)
    pickup_lng = Column(Float, nullable=False)

    drop_address = Column(String, nullable=False)
    drop_lat = Column(Float, nullable=False)
    drop_lng = Column(Float, nullable=False)

    vehicle_type = Column(SQLEnum(VehicleType), nullable=False)
    vehicle_name = Column(String, nullable=False)
    vehicle_category = Column(String(64), nullable=True)
    estimated_price = Column(Numeric(10, 2), nullable=False)
    distance_km = Column(Float, nullable=False)

    ride_type = Column(SQLEnum(RideType), nullable=True)

    num_passengers = Column(Integer, nullable=False, default=1)
    crew_member_ids = Column(JSON, nullable=True)

    scheduled_time = Column(DateTime, nullable=True)

    provider_id = Column(Integer, ForeignKey("aggregator_profiles.id"), nullable=True)
    provider_response_status = Column(String(32), nullable=True)
    provider_response_at = Column(DateTime(timezone=True), nullable=True)

    driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=True)
    assigned_driver_id = Column(Integer, ForeignKey("drivers.id"), nullable=True)
    driver_assigned_at = Column(DateTime(timezone=True), nullable=True)
    driver_accepted_at = Column(DateTime(timezone=True), nullable=True)
    trip_started_at = Column(DateTime(timezone=True), nullable=True)
    trip_completed_at = Column(DateTime(timezone=True), nullable=True)

    aggregator_id = Column(Integer, ForeignKey("aggregator_profiles.id"), nullable=True)
    provider = relationship("AggregatorProfile", foreign_keys=[provider_id])
    aggregator = relationship("AggregatorProfile", foreign_keys=[aggregator_id])

    assigned_driver = relationship("Driver", foreign_keys=[assigned_driver_id], back_populates="cab_bookings")

    driver_name = Column(String, nullable=True)
    driver_phone = Column(String, nullable=True)
    driver_plate = Column(String, nullable=True)
    aggregator_name = Column(String, nullable=True)
    agent_number = Column(String, default="+91 9876543251")
    helpline_number = Column(String, default="+91 1800-HEYPORTS")
    otp = Column(String(10), nullable=True)
    arrived_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(
        SQLEnum(BookingStatus),
        default=BookingStatus.PENDING_PROVIDER_RESPONSE,
        nullable=False,
    )

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    timeline_events = relationship(
        "BookingTimeline",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingTimeline.event_time",
    )
