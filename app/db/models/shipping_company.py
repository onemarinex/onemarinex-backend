"""The shipping company domain — Document 2, section 4.

Four of the seven entities that document names: the ones the authorization
policy reads. Fleet claim submission, sensitive access *requests* and the audit
event feed are the workflow around them and come later; the policy decides what
a company may see, and it only needs to know who the company is, who is asking,
what they have been approved for, and what has been granted.

The company is NOT a port agent. It cannot approve shore passes, dispatch
drivers, manage SOS or resolve incidents. Nothing here grants a write path into
agency operations, and nothing should be added that does.
"""

from sqlalchemy import (
    JSON, Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


# Document 2 section 4: "precise canonical enum and external evidence
# requirements must be approved by Super Admin" (owner question Q5, still open).
# The column is therefore a string, not a database enum: the taxonomy can be
# fixed without a schema change, and the overlap constraint is keyed on
# whatever value is stored either way. Do not conflate these -- the document is
# explicit that ISM manager, commercial manager and owner are distinct roles
# and one company may hold several on one vessel.
ROLE_MAX_LENGTH = 64

# Document 2 section 21's machine, spelled as it is written there:
#   DRAFT -> PENDING_VERIFICATION -> VERIFIED / REJECTED
#   VERIFIED -> SUSPENDED -> VERIFIED or CLOSED
# "A self-registered user cannot set verified status" is enforced in
# app/services/company_verification.py, not here.
COMPANY_STATUSES = (
    "DRAFT", "PENDING_VERIFICATION", "VERIFIED",
    "REJECTED", "SUSPENDED", "CLOSED",
)
MEMBERSHIP_ROLES = ("ADMIN", "ANALYST", "VIEWER")
# Document 2 section 21. Transitions are enforced in app/services/company_claims.py;
# the column is a string so a correction never needs a migration.
CLAIM_STATUSES = (
    "DRAFT", "SUBMITTED", "UNDER_REVIEW",
    "APPROVED", "REJECTED", "MORE_EVIDENCE",
    "REVOKED", "CORRECTED",
)


class ShippingCompany(Base):
    """Created, verified and suspended by Super Admin. Never self-verified."""

    __tablename__ = "shipping_companies"

    id = Column(Integer, primary_key=True, index=True)
    legal_name = Column(String(255), nullable=False)
    # Normalised for duplicate detection, the way crew identity normalises a
    # passport: the display name keeps whatever the company wrote.
    normalized_name = Column(String(255), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="DRAFT", server_default="DRAFT")

    verified_at = Column(DateTime(timezone=True), nullable=True)
    verified_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    memberships = relationship("CompanyUserMembership", back_populates="company")
    claims = relationship("VesselRoleClaim", back_populates="company")

    __table_args__ = (
        UniqueConstraint("normalized_name", name="uq_shipping_companies_normalized_name"),
    )


class CompanyUserMembership(Base):
    """Who may sign in on a company's behalf, and in what capacity."""

    __tablename__ = "company_user_memberships"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = Column(String(32), nullable=False, default="VIEWER", server_default="VIEWER")
    # Disabled rather than deleted: a membership that granted access to a
    # report is part of why that access happened, and the audit needs it.
    enabled = Column(Boolean, nullable=False, default=True, server_default="true")

    invited_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    company = relationship("ShippingCompany", back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("company_id", "user_id", name="uq_company_membership_user"),
    )


class VesselRoleClaim(Base):
    """A company's approved relationship to one vessel over one period.

    The interval is half-open [start, end): a handover on the boundary belongs
    to the incoming company, and the two periods do not overlap. `effective_end`
    is null for ongoing tenure, which Document 2 permits only where Super Admin
    has verified it.

    Stored as UTC instants. A date-only boundary is interpreted as midnight
    Asia/Kolkata before it reaches here (owner answer to Q7), because tenure is
    company-vessel-role and has no port whose midnight could define it.

    Overlap is refused by the database, not by an ORM check: two simultaneous
    approvals must not both succeed. The exclusion constraint is added by
    `app.main.ensure_company_domain_schema`, since create_all() cannot express
    EXCLUDE.
    """

    __tablename__ = "vessel_role_claims"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vessel_id = Column(
        Integer, ForeignKey("vessels.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Kept beside vessel_id because a claim is submitted against an IMO and the
    # canonical vessel is resolved afterwards; the document requires the
    # submitted identity to survive that resolution.
    imo_number = Column(String(100), nullable=True, index=True)
    role = Column(String(ROLE_MAX_LENGTH), nullable=False, index=True)

    effective_start = Column(DateTime(timezone=True), nullable=False)
    effective_end = Column(DateTime(timezone=True), nullable=True)

    status = Column(String(32), nullable=False, default="SUBMITTED", server_default="SUBMITTED")
    evidence_type = Column(String(120), nullable=True)
    evidence_reference = Column(String(255), nullable=True)
    review_reason = Column(Text, nullable=True)

    approved_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    company = relationship("ShippingCompany", back_populates="claims")

    __table_args__ = (
        Index("ix_vessel_role_claims_lookup", "vessel_id", "role", "status"),
        Index("ix_vessel_role_claims_company_vessel", "company_id", "vessel_id"),
    )


class SensitiveAccessGrant(Base):
    """Super Admin permission for one company to see detail on one call.

    Scoped to a specific vessel call and category, and time-bounded. Document 2
    is explicit that a grant never widens the company's role interval: it is
    intersected with tenure, never substituted for it.
    """

    __tablename__ = "sensitive_access_grants"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    category = Column(String(64), nullable=False, index=True)

    effective_start = Column(DateTime(timezone=True), nullable=False)
    effective_end = Column(DateTime(timezone=True), nullable=False)

    granted_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason = Column(Text, nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        Index(
            "ix_sensitive_grants_lookup",
            "company_id", "vessel_call_id", "category",
        ),
    )


class CompanyAuditEvent(Base):
    """Append-only record of every privileged decision.

    Document 2 section 21: "All approval/revocation actions must record Admin
    user, timestamp, legal/operational evidence, before/after role interval,
    reason and affected vessel/claims."

    Append-only is the point. A corrected claim must preserve who previously
    held the role, and an in-place edit erases exactly the thing a later
    dispute needs. Nothing in the service updates or deletes a row here.
    """

    __tablename__ = "company_audit_events"

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id", ondelete="CASCADE"),
        nullable=True, index=True,
    )
    # Which object was acted on, and which one. Kept as a string rather than a
    # foreign key so an audit row survives the deletion of what it describes.
    target_type = Column(String(64), nullable=False)
    target_id = Column(Integer, nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)

    # The interval before and after, which is what a dispute actually turns on.
    before_state = Column(JSON, nullable=True)
    after_state = Column(JSON, nullable=True)

    evidence_type = Column(String(120), nullable=True)
    evidence_reference = Column(String(255), nullable=True)
    reason = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("ix_company_audit_target", "target_type", "target_id"),
        Index("ix_company_audit_company_time", "company_id", "created_at"),
    )


class FleetClaimSubmission(Base):
    """The envelope a batch of claims arrives in — Document 2 section 4.

    A single claim does not need one; this exists so a CSV upload is reviewable
    as a unit and its rows keep their provenance. Section 21 is explicit that
    bulk approval is allowed "only if each row independently passes the same
    review and transaction-safe overlap rule", so this never approves anything
    on its own: it groups rows that are each decided individually.
    """

    __tablename__ = "fleet_claim_submissions"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    submitted_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # CSV, SINGLE or ADMIN. How it arrived, which decides what validation ran.
    source = Column(String(16), nullable=False, default="SINGLE", server_default="SINGLE")
    state = Column(String(32), nullable=False, default="DRAFT", server_default="DRAFT")

    original_filename = Column(String(255), nullable=True)
    row_count = Column(Integer, nullable=True)
    # Per-row validation findings, kept so a rejected upload can be explained
    # without re-parsing a file nobody stored.
    findings = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class SensitiveAccessRequest(Base):
    """A company asking to see detail on one call — Document 2 section 4.

    Distinct from the grant it may produce. Section 21: "Company admin cannot
    grant itself detail", so a request is never self-approving, and an approved
    request creates a SensitiveAccessGrant that the policy reads. The request
    survives the grant's expiry as the record of why it was asked for.
    """

    __tablename__ = "sensitive_access_requests"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    category = Column(String(64), nullable=False)
    requested_start = Column(DateTime(timezone=True), nullable=False)
    requested_end = Column(DateTime(timezone=True), nullable=False)
    reason = Column(Text, nullable=False)

    requested_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # REQUESTED -> UNDER_REVIEW -> APPROVED / REJECTED; APPROVED -> EXPIRED / REVOKED
    status = Column(String(32), nullable=False, default="REQUESTED", server_default="REQUESTED")
    decided_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_reason = Column(Text, nullable=True)
    # The grant this produced, if it was approved.
    grant_id = Column(
        Integer, ForeignKey("sensitive_access_grants.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        Index("ix_sensitive_requests_queue", "status", "created_at"),
    )
