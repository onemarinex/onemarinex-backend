"""Tenant-owned branding and retained company onboarding drafts."""

from sqlalchemy import (
    Column,
    Integer,
    String,
    JSON,
    LargeBinary,
    DateTime,
    ForeignKey,
    func,
)
from app.db.base import Base


class CompanyProfile(Base):
    __tablename__ = "company_profiles"
    company_id = Column(Integer, ForeignKey("shipping_companies.id"), primary_key=True)
    corporate_details = Column(JSON, nullable=False, default=dict)
    logo_png = Column(LargeBinary, nullable=True)
    updated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CompanyOnboardingDraft(Base):
    __tablename__ = "company_onboarding_drafts"
    id = Column(Integer, primary_key=True)
    payload = Column(JSON, nullable=False)
    status = Column(String(32), nullable=False, default="DRAFT")
    company_id = Column(Integer, ForeignKey("shipping_companies.id"), nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
