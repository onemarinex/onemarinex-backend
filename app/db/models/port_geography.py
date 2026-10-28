"""Reviewed country metadata. Existing port rows are retained unchanged."""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from app.db.base import Base


class PortGeography(Base):
    __tablename__ = "port_geography"
    port_id = Column(Integer, ForeignKey("ports.id"), primary_key=True)
    country_code = Column(String(2), nullable=False, index=True)
    evidence_reference = Column(String(1000), nullable=False)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
