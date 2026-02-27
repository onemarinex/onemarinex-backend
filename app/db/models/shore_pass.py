from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean, func
from sqlalchemy.orm import relationship
from app.db.base import Base

class ShorePass(Base):
    __tablename__ = "shore_passes"

    id = Column(Integer, primary_key=True, index=True)
    crew_profile_id = Column(Integer, ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=False)

    agent_name = Column(String(120), nullable=True)
    shore_pass_id = Column(String(64), nullable=False, unique=True)
    port_name = Column(String(128), nullable=True)
    vessel_name = Column(String(128), nullable=True)
    
    out_time = Column(DateTime(timezone=True), nullable=True)
    in_time = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    
    is_verified = Column(Boolean, default=False)
    status = Column(String(32), server_default="pending") # pending, approved, rejected
    rejection_reason = Column(String(255), nullable=True)
    approved_by_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationships ---
    crew_profile = relationship("CrewProfile")

    def __repr__(self) -> str:
        return f"<ShorePass id={self.id} serial={self.shore_pass_id}>"
