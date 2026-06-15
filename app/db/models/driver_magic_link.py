from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Float, JSON, func
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
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    booking = relationship("CabBooking")
    created_by_aggregator = relationship("AggregatorProfile")
    reach_events = relationship(
        "DriverMagicLinkReachEvent",
        back_populates="magic_link",
        cascade="all, delete-orphan",
        order_by="DriverMagicLinkReachEvent.reached_at.desc()",
    )


class DriverMagicLinkReachEvent(Base):
    __tablename__ = "driver_magic_link_reach_events"

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
