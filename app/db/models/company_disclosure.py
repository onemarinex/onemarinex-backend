from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    JSON,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from app.db.base import Base


class CompanySafetyEscalation(Base):
    __tablename__ = "company_safety_escalations"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "source_kind",
            "source_id",
            name="uq_company_safety_recipient_source",
        ),
    )
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id"), nullable=False, index=True
    )
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id"), nullable=False, index=True
    )
    source_kind = Column(String(16), nullable=False)
    source_id = Column(Integer, nullable=False)
    summary = Column(Text, nullable=False)
    urgency = Column(String(32), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CompanyDisclosureRequest(Base):
    __tablename__ = "company_disclosure_requests"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id"), nullable=False, index=True
    )
    source_kind = Column(String(16), nullable=False)
    source_id = Column(Integer, nullable=False)
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id"), nullable=False, index=True
    )
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    fields = Column(JSON, nullable=False)
    reason = Column(Text, nullable=False)
    event_start = Column(DateTime(timezone=True), nullable=False)
    event_end = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(32), nullable=False, default="REQUESTED")
    approved_scope = Column(JSON, nullable=True)
    access_expires_at = Column(DateTime(timezone=True), nullable=True)
    decision_reason = Column(Text, nullable=True)
    decided_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class CompanyManagementReport(Base):
    __tablename__ = "company_management_reports"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id"), nullable=False, index=True
    )
    report_type = Column(String(32), nullable=False)
    payload = Column(JSON, nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
