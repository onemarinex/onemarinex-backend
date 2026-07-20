from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base


class BookingInvitation(Base):
    __tablename__ = "booking_invitations"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("cab_bookings.id", ondelete="CASCADE"), nullable=False)
    invited_crew_id = Column(Integer, ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False)
    invited_by_id = Column(Integer, ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False)
    status = Column(String(20), default="active")  # active, removed
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    booking = relationship("CabBooking", back_populates="invitations")
    invited_crew = relationship("CrewProfile", foreign_keys=[invited_crew_id])
    invited_by = relationship("CrewProfile", foreign_keys=[invited_by_id])

    __table_args__ = (
        UniqueConstraint("booking_id", "invited_crew_id", name="uq_booking_invited_crew"),
    )
