"""Welfare organization identity and explicitly approved aggregate scope."""
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint, func

from app.db.base import Base


class WelfareOrganization(Base):
    __tablename__ = "welfare_organizations"
    id = Column(Integer, primary_key=True)
    legal_name = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="DRAFT", server_default="DRAFT")
    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'SUSPENDED', 'REJECTED')", name="ck_welfare_organization_status"),
        CheckConstraint("status != 'ACTIVE' OR (verified_at IS NOT NULL AND verified_by_user_id IS NOT NULL)", name="ck_welfare_active_verified"),
    )


class WelfareMembership(Base):
    __tablename__ = "welfare_memberships"
    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey("welfare_organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    role = Column(String(32), nullable=False, default="VIEWER", server_default="VIEWER")
    enabled = Column(Boolean, nullable=False, default=False, server_default="false")
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id", name="uq_welfare_membership_user"),
        CheckConstraint("role IN ('ADMIN', 'ANALYST', 'VIEWER')", name="ck_welfare_membership_role"),
    )


class WelfareScope(Base):
    __tablename__ = "welfare_scopes"
    organization_id = Column(Integer, ForeignKey("welfare_organizations.id"), primary_key=True)
    countries = Column(JSON, nullable=False, default=list)
    modules = Column(JSON, nullable=False, default=list)
    data_categories = Column(JSON, nullable=False, default=list)
    aggregate_levels = Column(JSON, nullable=False, default=list)
    export_allowed = Column(Boolean, nullable=False, default=False, server_default="false")
    effective_from = Column(DateTime(timezone=True), nullable=False)
    effective_until = Column(DateTime(timezone=True), nullable=True)
    __table_args__ = (CheckConstraint("effective_until IS NULL OR effective_until > effective_from", name="ck_welfare_scope_interval"),)


class WelfareScopePort(Base):
    __tablename__ = "welfare_scope_ports"
    organization_id = Column(Integer, ForeignKey("welfare_scopes.organization_id"), primary_key=True)
    port_id = Column(Integer, ForeignKey("ports.id"), primary_key=True)
