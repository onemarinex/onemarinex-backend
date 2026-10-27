from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class CrewAssignment(Base):
    """A crew member's membership of a specific historical vessel call."""

    __tablename__ = "crew_assignments"
    __table_args__ = (
        Index(
            "uq_crew_assignments_active_manifest",
            "vessel_call_id",
            "vessel_crew_id",
            unique=True,
            postgresql_where=text(
                "ended_at IS NULL AND vessel_crew_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_crew_assignments_active_profile",
            "vessel_call_id",
            "crew_profile_id",
            unique=True,
            postgresql_where=text(
                "ended_at IS NULL AND crew_profile_id IS NOT NULL"
            ),
        ),
        Index(
            "uq_crew_assignments_active_pending_passport",
            "vessel_call_id",
            text("upper(replace(trim(passport_number), ' ', ''))"),
            unique=True,
            postgresql_where=text(
                "ended_at IS NULL AND crew_profile_id IS NULL "
                "AND passport_number IS NOT NULL AND trim(passport_number) <> ''"
            ),
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    vessel_call_id = Column(
        Integer,
        ForeignKey("vessel_calls.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    crew_profile_id = Column(
        Integer,
        ForeignKey("crew_profiles.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    vessel_crew_id = Column(
        Integer,
        ForeignKey("vessel_crew.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    crew_name = Column(String(255), nullable=False)
    rank = Column(String(100), nullable=True)
    nationality = Column(String(100), nullable=True)
    # The legacy public identifier, which embeds the passport number. Retained
    # so existing references keep resolving; being replaced by public_ref.
    hpid = Column(String(100), nullable=True, index=True)
    # The opaque per-call public identifier. Random and meaningless, reissued
    # for every assignment, and never derived from the person. Nullable while
    # the backfill runs; see app/services/assignment_ref.py.
    public_ref = Column(String(40), nullable=True, unique=True, index=True)
    # How this assignment came to exist. The HPID specification §11 requires it
    # on the developer contract, and §9 needs it to tell a self-selected
    # unmanaged row from one an agent put on a manifest: the first records that
    # a crew member picked the vessel themselves, and is NOT evidence of
    # verified crew membership or immigration clearance.
    #
    # Every row that existed before this column was manifest-derived, which is
    # why MANIFEST is the server default rather than something meaning unknown.
    source = Column(
        String(32), nullable=False, default="MANIFEST", server_default="MANIFEST",
        index=True,
    )
    passport_number = Column(String(64), nullable=True)
    # Emergency contacts are vessel-assignment specific. A profile-level value
    # cannot represent a sailor serving on two vessels at the same time.
    emergency_email = Column(String(255), nullable=True)
    shore_pass_eligible = Column(Boolean, nullable=False, default=False, server_default="false")

    started_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at = Column(DateTime(timezone=True), nullable=True)
    # When an agent *confirmed* the movement, which is not the same as when
    # the row was written. HPID spec §8: "Trip booked/completed is NOT
    # automatically confirmed sign-on/off. Require explicit movement
    # confirmation for verified company movement metrics." Nothing in the
    # booking flow sets either of these.
    signed_on_at = Column(DateTime(timezone=True), nullable=True)
    signed_off_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    vessel_call = relationship("VesselCall", back_populates="crew_assignments")
    crew_profile = relationship("CrewProfile")
    vessel_crew = relationship("VesselCrew")
