from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Date, Numeric
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.base import Base

class Vessel(Base):
    __tablename__ = "vessels"

    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    
    name = Column(String(255), nullable=False)
    imo_number = Column(String(100), nullable=False, unique=True)
    vessel_type = Column(String(100), nullable=False)
    berth_assignment = Column(String(100), nullable=True)
    crew_count = Column(Integer, default=0)
    
    eta = Column(DateTime(timezone=True), nullable=True)
    etd = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(50), default="Active") # Active, Departing, Departed

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    agent = relationship("User", backref="vessels")
    crew_manifest = relationship("VesselCrew", back_populates="vessel", cascade="all, delete-orphan")
