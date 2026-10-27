from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey, Enum as SQLEnum, Numeric, Index, text
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.db.base import Base
from app.db.types import UTCDateTime


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
    __table_args__ = (
        Index(
            "uq_cab_bookings_crew_idempotency_key",
            "crew_id",
            "client_idempotency_key",
            unique=True,
            postgresql_where=text("client_idempotency_key IS NOT NULL"),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(String, unique=True, index=True, nullable=False)
    port = Column(String(255), nullable=True, index=True)

    crew_id = Column(
        Integer, ForeignKey("crew_profiles.id", ondelete="SET NULL"), nullable=True
    )
    crew = relationship("CrewProfile", back_populates="cab_bookings")

    # The ship this trip was taken from, resolved once when the booking is made.
    #
    # Without it a trip's vessel had to be inferred at read time from whoever
    # booked it — and a crew member who joins a second ship is on both
    # manifests, so every trip they ever took followed them onto the new one.
    # Nullable because rows written before this column cannot be backfilled
    # reliably; readers fall back to crew linkage for those.
    vessel_id = Column(Integer, ForeignKey("vessels.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id", ondelete="SET NULL"), nullable=True, index=True
    )
    crew_assignment_id = Column(
        Integer,
        ForeignKey("crew_assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    vessel_call = relationship("VesselCall")
    crew_assignment = relationship("CrewAssignment")
    agency_id = Column(
        Integer, ForeignKey("agent_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    port_id = Column(
        Integer, ForeignKey("ports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    context_resolution = Column(String(32), nullable=True)
    # A retry of the same client action must return this booking instead of
    # creating another provider broadcast. The payload fingerprint prevents a
    # caller from accidentally reusing the key for a different journey.
    client_idempotency_key = Column(String(64), nullable=True, index=True)
    request_fingerprint = Column(String(64), nullable=True)

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
    # "package_trip" or "coordinated_transfer" — distinguishes the two booking
    # flows for WhatsApp template selection (independent of ride_type, which
    # is about the fulfilling provider, not the trip shape).
    trip_type = Column(String(32), nullable=True)

    num_passengers = Column(Integer, nullable=False, default=1)
    crew_member_ids = Column(JSON, nullable=True)

    scheduled_time = Column(UTCDateTime, nullable=True)

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
    agent_number = Column(String, nullable=True)
    # Set per booking from the port's configured helpline. Readers fall back to
    # Missing verified contact data is shown honestly; never fill either field
    # with a demo number.
    helpline_number = Column(String, nullable=True)
    otp = Column(String(10), nullable=True)
    # Brute-force counters for the ride OTP, the same three fields
    # DriverMagicLink carries. The authenticated driver route checked the OTP
    # with no limit at all, and CrewProfile.ride_otp is four digits that never
    # rotate, so an unlimited guesser got that person's code permanently.
    otp_failed_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    otp_last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    otp_locked_until = Column(DateTime(timezone=True), nullable=True)
    arrived_at = Column(DateTime(timezone=True), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    status = Column(
        SQLEnum(BookingStatus, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=BookingStatus.PENDING_PROVIDER_RESPONSE,
        nullable=False,
    )

    created_at = Column(UTCDateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    timeline_events = relationship(
        "BookingTimeline",
        back_populates="booking",
        cascade="all, delete-orphan",
        order_by="BookingTimeline.event_time",
    )

    invitations = relationship(
        "BookingInvitation",
        back_populates="booking",
        cascade="all, delete-orphan",
    )

    provider_rejections = relationship(
        "BookingProviderRejection",
        back_populates="booking",
        cascade="all, delete-orphan",
    )

    magic_link = relationship(
        "DriverMagicLink",
        back_populates="booking",
        uselist=False,
        cascade="all, delete-orphan",
    )

    reviews = relationship(
        "BookingReview",
        back_populates="booking",
        cascade="all, delete-orphan",
    )
