from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, func
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base

class AggregatorProfile(Base):
    __tablename__ = "aggregator_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    
    company_name = Column(String(255), nullable=False)
    contact_person = Column(String(255), nullable=True)
    operating_port_id = Column(Integer, ForeignKey("ports.id"), nullable=False)
    gst_number = Column(String(64), nullable=True)
    status = Column(String(32), server_default="Active") # Active, Inactive
    profile_image = Column(String(512), nullable=True) # URL to image
    aggregator_identifier = Column(String(64), nullable=True)  # e.g., "AGG-12287-28792-87258"
    fleet = Column(JSONB, nullable=True)
    documents = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationship back to user ---
    user = relationship("User", back_populates="aggregator_profile")
    operating_port = relationship("Port")


    def __repr__(self) -> str:
        return f"<AggregatorProfile id={self.id} user_id={self.user_id} company={self.company_name}>"
