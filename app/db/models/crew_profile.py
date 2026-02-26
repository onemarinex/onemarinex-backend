from sqlalchemy import Column, Integer, String, Date, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
from app.db.base import Base

class CrewProfile(Base):
    __tablename__ = "crew_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)

    full_name = Column(String(255), nullable=False)
    rank = Column(String(64), nullable=False)
    nationality = Column(String(2), nullable=False)  # ISO Alpha-2
    passport_number = Column(String(64), nullable=False)
    date_of_birth = Column(Date, nullable=False)
    
    # Professional details
    current_port = Column(String(128), nullable=True)
    vessel = Column(String(128), nullable=True)
    ride_otp = Column(String(4), nullable=True) # Lifetime OTP for ride starts

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationship back to user ---
    user = relationship("User", back_populates="crew_profile")
    cab_bookings = relationship("CabBooking", back_populates="crew", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<CrewProfile id={self.id} user_id={self.user_id} rank={self.rank}>"
