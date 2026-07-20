from sqlalchemy import Column, Integer, String, Float, Text, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base


class BookingReview(Base):
    __tablename__ = "booking_reviews"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False, index=True)
    crew_id = Column(Integer, ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False, index=True)

    # What is being reviewed: "driver" or "facility_stop"
    review_type = Column(String(32), nullable=False, index=True)

    # For driver reviews
    driver_id = Column(Integer, ForeignKey("drivers.id", ondelete="SET NULL"), nullable=True, index=True)

    # For facility_stop reviews (free-text since facilities have no dedicated table)
    facility_name = Column(String(255), nullable=True)
    facility_stop_id = Column(String(64), nullable=True)  # matches itinerary_stops[].id

    # Rating + review
    rating = Column(Float, nullable=False)  # 1.0 - 5.0
    review_text = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    booking = relationship("CabBooking", back_populates="reviews")
    crew = relationship("CrewProfile")
    driver = relationship("Driver")

    __table_args__ = (
        UniqueConstraint("booking_id", "crew_id", "review_type", "driver_id", name="uq_booking_driver_review"),
        UniqueConstraint("booking_id", "crew_id", "review_type", "facility_stop_id", name="uq_booking_facility_review"),
    )
