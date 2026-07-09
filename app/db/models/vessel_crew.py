from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Date, Boolean
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base

class VesselCrew(Base):
    __tablename__ = "vessel_crew"

    id = Column(Integer, primary_key=True, index=True)
    vessel_id = Column(Integer, ForeignKey("vessels.id", ondelete="CASCADE"), nullable=False)
    
    name = Column(String(255), nullable=False)
    rank = Column(String(100), nullable=False)
    nationality = Column(String(100), nullable=True)
    hp_id = Column(String(100), nullable=True)
    expiry_date = Column(Date, nullable=True)
    status = Column(String(50), default="Pending") # Mapped, Pending
    shore_pass_eligible = Column(Boolean, default=False, nullable=False, server_default="false")
    shore_pass_valid_upto = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    vessel = relationship("Vessel", back_populates="crew_manifest")
