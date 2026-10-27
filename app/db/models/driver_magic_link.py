from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Float, JSON, UniqueConstraint, func
from sqlalchemy.orm import relationship

from app.db.base import Base


class DriverMagicLink(Base):
    __tablename__ = "driver_magic_links"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(
        Integer,
        ForeignKey("cab_bookings.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    token = Column(String(128), unique=True, nullable=False, index=True)
    created_by_aggregator_id = Column(
        Integer,
        ForeignKey("aggregator_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    itinerary_stops = Column(JSON, nullable=False)
    otp_verified_at = Column(DateTime(timezone=True), nullable=True)
    otp_failed_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    otp_last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    otp_locked_until = Column(DateTime(timezone=True), nullable=True)
    # When this link stops working. Null on rows created before expiry existed;
    # those fall back to created_at + the configured TTL, so the policy applies
    # to them without rewriting a single row.
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # Set to kill a link without replacing it. Reassigning the driver rotates
    # the token instead, which revokes the previous holder by making their URL
    # resolve to nothing.
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    booking = relationship("CabBooking", back_populates="magic_link")
    created_by_aggregator = relationship("AggregatorProfile")
    reach_events = relationship(
        "DriverMagicLinkReachEvent",
        back_populates="magic_link",
        cascade="all, delete-orphan",
        order_by="DriverMagicLinkReachEvent.reached_at.desc()",
    )


class DriverMagicLinkReachEvent(Base):
    __tablename__ = "driver_magic_link_reach_events"
    __table_args__ = (
        UniqueConstraint(
            "magic_link_id",
            "stop_id",
            name="uq_driver_magic_link_reach_event_stop",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    magic_link_id = Column(
        Integer,
        ForeignKey("driver_magic_links.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stop_id = Column(String(64), nullable=False, index=True)
    stop_name = Column(String(255), nullable=False)
    latitude = Column(Float, nullable=False)
    longitude = Column(Float, nullable=False)
    notes = Column(String(500), nullable=True)
    reached_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)

    magic_link = relationship("DriverMagicLink", back_populates="reach_events")
