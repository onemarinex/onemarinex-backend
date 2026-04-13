from sqlalchemy import Column, Integer, String, Float, DateTime, JSON, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.base import Base

class Sightseeing(Base):
    __tablename__ = "sightseeings"

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True) # Linked to a port
    name = Column(String, nullable=False)
    location_name = Column(String, nullable=False)
    distance_from_port = Column(Float, nullable=False)
    rating = Column(Float, nullable=False)
    price_per_person = Column(Float, nullable=False, default=0)
    timings = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    lat = Column(Float, nullable=False)
    lng = Column(Float, nullable=False)
    image_url = Column(String, nullable=True)
    images = Column(JSON, nullable=True)  # List of image URLs
    description = Column(String, nullable=True)
    address = Column(String, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship
    port = relationship("Port")
