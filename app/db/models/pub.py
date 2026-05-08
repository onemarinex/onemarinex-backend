from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime

from app.db.base import Base

class Pub(Base):
    __tablename__ = "pubs"

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True) # Linked to a port
    name = Column(String, nullable=False)
    location_name = Column(String, nullable=False)
    distance_from_port = Column(Float, nullable=False)
    rating = Column(Float, nullable=False)
    price_per_person = Column(Float, nullable=False)
    timings = Column(String, nullable=False)
    service_type = Column(String, nullable=False)
    popular_for = Column(JSON, nullable=True)  # List of strings
    phone = Column(String, nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    image_url = Column(String, nullable=True)
    description = Column(String, nullable=True)
    address = Column(String, nullable=True)
    pub_type = Column(String, nullable=True) # e.g. Brewery, Roof top, Night club
    category = Column(String, nullable=True)
    best_for = Column(String, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    port = relationship("Port")
