"""Agency-billed transport, isolated from the shore-leave marketplace."""

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    ForeignKey,
    JSON,
    Text,
    func,
    UniqueConstraint,
)
from app.db.base import Base


class AgencyTransfer(Base):
    __tablename__ = "agency_transfers"
    __table_args__ = (
        UniqueConstraint(
            "agency_id", "client_request_id", name="uq_agency_transfer_request"
        ),
    )
    client_request_id = Column(String(64), nullable=True)
    request_fingerprint = Column(String(64), nullable=True)
    id = Column(Integer, primary_key=True)
    agency_id = Column(
        Integer, ForeignKey("agent_profiles.id"), nullable=False, index=True
    )
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id"), nullable=False, index=True
    )
    provider_id = Column(
        Integer, ForeignKey("aggregator_profiles.id"), nullable=False, index=True
    )
    assignment_ids = Column(JSON, nullable=False)
    movement_type = Column(String(16), nullable=False)
    pickup = Column(String(500), nullable=False)
    destination = Column(String(500), nullable=False)
    travel_at = Column(DateTime(timezone=True), nullable=False)
    requirements = Column(Text)
    status = Column(
        String(32), nullable=False, default="OFFERED", server_default="OFFERED"
    )
    driver_id = Column(Integer, ForeignKey("drivers.id"))
    driver_token_hash = Column(String(64), unique=True)
    driver_link_expires_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgencyTransferEvent(Base):
    __tablename__ = "agency_transfer_events"
    id = Column(Integer, primary_key=True)
    transfer_id = Column(
        Integer, ForeignKey("agency_transfers.id"), nullable=False, index=True
    )
    actor_user_id = Column(Integer, ForeignKey("users.id"))
    actor_driver_id = Column(Integer, ForeignKey("drivers.id"))
    action = Column(String(32), nullable=False)
    previous_state = Column(JSON)
    next_state = Column(JSON, nullable=False)
    reason = Column(Text)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CrewMovementEvent(Base):
    __tablename__ = "crew_movement_events"
    id = Column(Integer, primary_key=True)
    assignment_id = Column(
        Integer, ForeignKey("crew_assignments.id"), nullable=False, index=True
    )
    actor_user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(16), nullable=False)
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
