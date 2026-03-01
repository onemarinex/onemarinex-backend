from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.db.base import Base

class IncidentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"

class IncidentType(str, enum.Enum):
    CREW = "CREW"
    DRIVER = "DRIVER"

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(String(64), unique=True, index=True) # e.g. INC-001
    aggregator_id = Column(Integer, ForeignKey("aggregator_profiles.id", ondelete="CASCADE"), nullable=True)
    port_name = Column(String(128), nullable=True) # Port where incident occurred
    
    type = Column(SQLEnum(IncidentType), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    
    status = Column(SQLEnum(IncidentStatus), default=IncidentStatus.ACTIVE)
    
    # Reporter/Context info from Screenshot 1 & 2
    reporter_name = Column(String(255), nullable=True) # e.g. John
    reporter_role = Column(String(255), nullable=True) # e.g. Chief Officer
    reporter_id = Column(String(64), nullable=True)   # e.g. HPID-19383-9282
    
    trip_id = Column(String(64), nullable=True)      # e.g. TR 101
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    aggregator = relationship("AggregatorProfile", backref="incidents")
    notes = relationship("IncidentNote", back_populates="incident", cascade="all, delete-orphan")

class IncidentNote(Base):
    __tablename__ = "incident_notes"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id", ondelete="CASCADE"))
    author_name = Column(String(255), nullable=True)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    incident = relationship("Incident", back_populates="notes")
