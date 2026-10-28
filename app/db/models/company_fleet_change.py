"""Company-proposed fleet corrections. Only Super Admin can apply them."""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, JSON, func
from app.db.base import Base


class CompanyFleetChange(Base):
    __tablename__ = "company_fleet_changes"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id"), nullable=False, index=True
    )
    claim_id = Column(
        Integer, ForeignKey("vessel_role_claims.id"), nullable=False, index=True
    )
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(16), nullable=False)
    payload = Column(JSON, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="REQUESTED")
    decided_by_user_id = Column(Integer, ForeignKey("users.id"))
    decision_reason = Column(Text)
    replacement_claim_id = Column(Integer, ForeignKey("vessel_role_claims.id"))
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at = Column(DateTime(timezone=True))
