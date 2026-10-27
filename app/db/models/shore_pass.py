from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Boolean, func
from sqlalchemy.orm import relationship
from app.db.base import Base

class ShorePass(Base):
    __tablename__ = "shore_passes"

    id = Column(Integer, primary_key=True, index=True)
    # Nullable since 26 Oct 2026 — owner answer Q1, HPID spec §7 and §16,
    # audit R10. An agent may mark a crew member eligible before that person
    # has ever registered: "crew app registration controls use of HeyPorts
    # services, not the agent's legal ability to process shore leave". While
    # this was non-null no such pass could be written at all, so the schema
    # contradicted the card the crew member was shown.
    #
    # The assignment is what a pass is really against -- one person on one
    # call -- and it is filled on every pass this application writes. The
    # profile is the link to an account, which may arrive later or never.
    crew_profile_id = Column(
        Integer, ForeignKey("crew_profiles.id", ondelete="CASCADE"), nullable=True
    )
    crew_assignment_id = Column(
        Integer,
        ForeignKey("crew_assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    vessel_call_id = Column(
        Integer,
        ForeignKey("vessel_calls.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

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
    approved_by_name = Column(String(120), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationships ---
    crew_profile = relationship("CrewProfile", back_populates="cab_bookings" if "cab_bookings" in locals() else None) # backref is handled in crew_profile.py

    @property
    def hpid(self):
        return self.crew_profile.hpid if self.crew_profile else None

    def __repr__(self) -> str:
        return f"<ShorePass id={self.id} serial={self.shore_pass_id}>"
