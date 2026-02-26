from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, func
from sqlalchemy.orm import relationship
from app.db.base import Base

class AgentProfile(Base):
    __tablename__ = "agent_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)
    
    agency_name = Column(String(255), nullable=False)
    contact_person = Column(String(255), nullable=True)
    location = Column(String(255), nullable=False) # Base location
    assigned_port = Column(String(255), nullable=True) # current assigned port
    gst_number = Column(String(64), nullable=True)
    license_number = Column(String(64), nullable=True)
    status = Column(String(32), server_default="Active") # Active, Inactive
    profile_image = Column(String(512), nullable=True) # URL to image
    agent_identifier = Column(String(64), nullable=True)  # e.g., "12287-28792-87258"

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationship back to user ---
    user = relationship("User", back_populates="agent_profile")

    def __repr__(self) -> str:
        return f"<AgentProfile id={self.id} user_id={self.user_id} agency={self.agency_name}>"
