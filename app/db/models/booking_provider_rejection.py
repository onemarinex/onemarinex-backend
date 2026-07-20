from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base


class BookingProviderRejection(Base):
    __tablename__ = "booking_provider_rejections"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    provider_id = Column(Integer, ForeignKey("aggregator_profiles.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking = relationship("CabBooking", back_populates="provider_rejections")
    provider = relationship("AggregatorProfile")

    __table_args__ = (
        UniqueConstraint("booking_id", "provider_id", name="uq_booking_provider_rejection"),
    )
