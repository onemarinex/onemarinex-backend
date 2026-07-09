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
    flag = Column(String(100), nullable=True)
    crew_count = Column(Integer, default=0)
    
    eta = Column(DateTime(timezone=True), nullable=True)
    etd = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(50), default="Active") # Active, Departing, Departed

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # Relationships
    agent = relationship("User", backref="vessels")
    crew_manifest = relationship("VesselCrew", back_populates="vessel", cascade="all, delete-orphan")

    @property
    def total_crew(self) -> int:
        return self.crew_count or 0

    @property
    def eligible_crew_count(self) -> int:
        return sum(1 for c in self.crew_manifest if c.shore_pass_eligible)

    @property
    def crew_ashore_count(self) -> int:
        from sqlalchemy.orm import object_session
        from app.db.models.crew_profile import CrewProfile
        from app.db.models.shore_pass import ShorePass
        
        session = object_session(self)
        if not session:
            return 0
        crew_hpids = [c.hp_id for c in self.crew_manifest if c.hp_id]
        if not crew_hpids:
            return 0
        crew_profile_ids = [cp.id for cp in session.query(CrewProfile).filter(CrewProfile.hpid.in_(crew_hpids)).all()]
        if not crew_profile_ids:
            return 0
        return session.query(ShorePass).filter(
            ShorePass.crew_profile_id.in_(crew_profile_ids),
            ShorePass.in_time.is_(None)
        ).count()
