"""Email possession invitations; bearer secrets are never persisted in plaintext."""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from app.db.base import Base


class CompanyInvitation(Base):
    __tablename__ = "company_invitations"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id"), nullable=False, index=True
    )
    email = Column(String(255), nullable=False, index=True)
    role = Column(String(32), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    invited_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True))
    revoked_at = Column(DateTime(timezone=True))
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
