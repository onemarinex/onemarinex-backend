"""Crew feedback is separate from formal safety cases."""

from sqlalchemy import (
    Column,
    Integer,
    JSON,
    Text,
    DateTime,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    func,
)
from app.db.base import Base


class WelfareFeedback(Base):
    __tablename__ = "welfare_feedback"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "assignment_id", name="uq_welfare_feedback_user_assignment"
        ),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    assignment_id = Column(
        Integer, ForeignKey("crew_assignments.id"), nullable=True, index=True
    )
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id"), nullable=True, index=True
    )
    vessel_id = Column(Integer, ForeignKey("vessels.id"), nullable=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True, index=True)
    experience_at = Column(DateTime(timezone=True), nullable=True, index=True)
    answers = Column(JSON, nullable=False, default=dict)
    message = Column(Text, nullable=False, default="")
    contact_opt_in = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
