from sqlalchemy import Column, Integer, String, Float, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
from app.db.base import Base

class Driver(Base):
    __tablename__ = "drivers"

    id = Column(Integer, primary_key=True, index=True)
    aggregator_id = Column(Integer, ForeignKey("aggregator_profiles.id", ondelete="CASCADE"), nullable=False)
    
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=False)
    hpid = Column(String(32), unique=True, index=True, nullable=True) # Aggregator relative driver ID
    
    license_number = Column(String(64), nullable=True)
    vehicle_number = Column(String(64), nullable=False)  # Plate number
    vehicle_type = Column(String(64), nullable=True)    # e.g. Premium, AC, XL
    vehicle_name = Column(String(255), nullable=True)   # e.g. Swift Dzire
    
    rating = Column(Float, default=5.0)
    profile_image = Column(String(512), nullable=True)
    status = Column(String(32), server_default="Available")  # Available, Busy, Offline

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationships ---
    aggregator = relationship("AggregatorProfile", backref="drivers")
    cab_bookings = relationship("CabBooking", back_populates="assigned_driver")

    def __repr__(self) -> str:
        return f"<Driver id={self.id} name={self.name} vehicle={self.vehicle_number}>"
