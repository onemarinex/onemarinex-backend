from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey,Enum
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB
from datetime import datetime
from app.db.base import Base
import enum


class PlaceCategory(enum.Enum):
    restaurant = "restaurant"
    pub = "pub"
    hotel = "hotel"
    sightseeing = "sightseeing"


class Vendors(Base):

    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True) # Linked to a port
    name = Column(String, nullable=False)
    location_name = Column(String, nullable=False)
    distance_from_port = Column(Float, nullable=False)
    rating = Column(Float, nullable=False)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    phone = Column(String, nullable=False)
    email = Column(String,nullable=False)
    status = Column(String(32), server_default="Active") # Active, Inactive
    documents = Column(JSONB, nullable=True)
    images = Column(JSONB, nullable=True)
    other_information = Column(JSONB, nullable=True)    # service type,price,timings
    category = Column(Enum(PlaceCategory), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    port = relationship("Port")
