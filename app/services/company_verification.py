"""Who a company is, and who may say so — Document 2 section 21.

    Company: DRAFT -> PENDING_VERIFICATION -> VERIFIED / REJECTED;
             VERIFIED -> SUSPENDED -> VERIFIED or CLOSED
    Prohibited shortcut: "A self-registered user cannot set verified status."

That prohibition is the whole reason this module exists separately from the
model. A status column anyone can write is not a verification; the transition
has to pass through a function that checks who is asking.

Every decision appends to the same audit log the claims use, because
"all approval/revocation actions must record Admin user, timestamp,
legal/operational evidence, before/after... and reason" applies here too: a
company's verification is the root of every access decision beneath it.

Suspension is deliberately reversible and distinct from closure. A company
under investigation loses access immediately -- the policy refuses anything but
VERIFIED -- without its claims, history or audit being touched, so restoring it
is a status change rather than a reconstruction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.shipping_company import CompanyAuditEvent, ShippingCompany

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "DRAFT": frozenset({"PENDING_VERIFICATION"}),
    "PENDING_VERIFICATION": frozenset({"VERIFIED", "REJECTED"}),
    "VERIFIED": frozenset({"SUSPENDED"}),
    "SUSPENDED": frozenset({"VERIFIED", "CLOSED"}),
    "REJECTED": frozenset(),
    "CLOSED": frozenset(),
}

# Only this one grants access. Everything else, including SUSPENDED, is
# refused by app/services/company_access.py.
ACTIVE_STATUS = "VERIFIED"


class VerificationError(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_superadmin(actor) -> None:
    if getattr(actor, "role", None) != "superadmin":
        raise VerificationError(
            "Only a Super Admin may change a company's verification status. "
            "A company cannot verify itself."
        )


def _audit(db, *, actor, company, action, before, after, reason=None,
           evidence_type=None, evidence_reference=None):
    db.add(CompanyAuditEvent(
        actor_user_id=getattr(actor, "id", None),
        company_id=company.id,
        target_type="shipping_company",
        target_id=company.id,
        action=action,
        before_state={"status": before},
        after_state={"status": after},
        reason=reason,
        evidence_type=evidence_type,
        evidence_reference=evidence_reference,
    ))


def _transition(company: ShippingCompany, to_status: str) -> str:
    allowed = ALLOWED_TRANSITIONS.get(company.status, frozenset())
    if to_status not in allowed:
        raise VerificationError(
            f"A company cannot go from {company.status} to {to_status}. "
            f"Allowed from {company.status}: "
            + (", ".join(sorted(allowed)) if allowed else "nothing, it is final")
        )
    before = company.status
    company.status = to_status
    return before


def submit_for_verification(db: Session, *, company: ShippingCompany, actor):
    """The company itself may do this. It is a request, not a decision."""
    before = _transition(company, "PENDING_VERIFICATION")
    db.flush()
    _audit(db, actor=actor, company=company, action="verification_requested",
           before=before, after=company.status)
    return company


def verify(db: Session, *, company: ShippingCompany, actor,
           evidence_type: str, evidence_reference: str,
           reason: Optional[str] = None):
    """Super Admin only, and only on evidence."""
    _require_superadmin(actor)
    if not (evidence_type or "").strip() or not (evidence_reference or "").strip():
        raise VerificationError("Verification must record the evidence it rests on")
    before = _transition(company, "VERIFIED")
    company.verified_at = _utc_now()
    company.verified_by_user_id = getattr(actor, "id", None)
    db.flush()
    _audit(db, actor=actor, company=company, action="verified",
           before=before, after=company.status, reason=reason,
           evidence_type=evidence_type, evidence_reference=evidence_reference)
    return company


def reject(db: Session, *, company: ShippingCompany, actor, reason: str):
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise VerificationError("A rejection must record why")
    before = _transition(company, "REJECTED")
    db.flush()
    _audit(db, actor=actor, company=company, action="rejected",
           before=before, after=company.status, reason=reason)
    return company


def suspend(db: Session, *, company: ShippingCompany, actor, reason: str):
    """Access stops at once; nothing beneath the company is touched.

    Claims, grants, memberships and audit all survive, because the policy
    refuses anything that is not VERIFIED and restoring is then one status
    change rather than a reconstruction.
    """
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise VerificationError("A suspension must record why")
    before = _transition(company, "SUSPENDED")
    db.flush()
    _audit(db, actor=actor, company=company, action="suspended",
           before=before, after=company.status, reason=reason)
    return company


def reinstate(db: Session, *, company: ShippingCompany, actor, reason: str):
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise VerificationError("A reinstatement must record why")
    before = _transition(company, "VERIFIED")
    db.flush()
    _audit(db, actor=actor, company=company, action="reinstated",
           before=before, after=company.status, reason=reason)
    return company


def close(db: Session, *, company: ShippingCompany, actor, reason: str):
    """Terminal. Reachable only from SUSPENDED, so closing is a two-step act.

    Section 21's machine does not allow VERIFIED -> CLOSED, which means an
    active company cannot be ended by one click.
    """
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise VerificationError("A closure must record why")
    before = _transition(company, "CLOSED")
    db.flush()
    _audit(db, actor=actor, company=company, action="closed",
           before=before, after=company.status, reason=reason)
    return company
