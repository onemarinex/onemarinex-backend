"""The claim lifecycle: submit, review, approve, revoke — and never auto-approve.

Document 2 section 21 gives the state machine and names the shortcuts that are
prohibited rather than merely discouraged:

    DRAFT -> SUBMITTED -> UNDER_REVIEW -> APPROVED / REJECTED / MORE_EVIDENCE
    APPROVED -> REVOKED / CORRECTED

    "No approval on CSV upload or auto-approval for nonconflicting IMO."
    "A self-registered user cannot set verified status."
    "Bulk approval is allowed only if each row independently passes the same
     review and transaction-safe overlap rule; default is manual approval."

Two rules here are security properties rather than workflow preferences.

**A blocked claim must not describe its competitor.** Section 21: "Competing
claims must be shown in a private Super Admin conflict queue; the requesting
company gets a generic pending/conflict status, never competitor identity,
evidence or effective dates." So `ClaimConflict` carries no identifying detail,
and the overlap is detected by the database refusing the write rather than by a
query this code could accidentally leak the result of.

**A correction preserves who previously held the role.** Section 21 again:
"avoid in-place changes that erase who previously held the role." A correction
therefore closes the old claim as CORRECTED and writes a new one, and every
decision appends an audit row with the interval before and after.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models.shipping_company import (
    CompanyAuditEvent,
    ShippingCompany,
    VesselRoleClaim,
)
from app.services.company_roles import validate_role

# Section 21's table, as code. A transition absent from here is refused.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    # An accepted declaration can enter independent review. It is never
    # promoted to APPROVED without the existing SA evidence workflow.
    "AUTO_ACCEPTED": frozenset({"UNDER_REVIEW", "REVOKED"}),
    "DRAFT": frozenset({"SUBMITTED"}),
    "SUBMITTED": frozenset({"UNDER_REVIEW"}),
    "UNDER_REVIEW": frozenset({"APPROVED", "REJECTED", "MORE_EVIDENCE"}),
    "MORE_EVIDENCE": frozenset({"UNDER_REVIEW"}),
    "APPROVED": frozenset({"REVOKED", "CORRECTED"}),
    "REJECTED": frozenset(),
    "REVOKED": frozenset(),
    "CORRECTED": frozenset(),
}


class ClaimError(Exception):
    """Something about this claim or the actor is wrong."""


class ClaimConflict(ClaimError):
    """Another approved claim already covers this vessel, role and period.

    Deliberately says nothing else. The company that submitted must not learn
    who holds it, on what evidence, or for which dates -- that is a competitor's
    commercial position, and section 21 routes it to a private Super Admin
    queue instead.
    """

    def __init__(self):
        super().__init__(
            "This claim conflicts with an existing approved claim and has been "
            "sent for review."
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_superadmin(actor) -> None:
    """Only Super Admin decides. Section 21's first prohibited shortcut."""
    if getattr(actor, "role", None) != "superadmin":
        raise ClaimError("Only a Super Admin may decide a claim")


def _interval(claim: Optional[VesselRoleClaim]) -> Optional[dict]:
    if claim is None:
        return None
    return {
        "status": claim.status,
        "role": claim.role,
        "vessel_id": claim.vessel_id,
        "company_id": claim.company_id,
        "effective_start": claim.effective_start.isoformat() if claim.effective_start else None,
        "effective_end": claim.effective_end.isoformat() if claim.effective_end else None,
        "revoked_at": claim.revoked_at.isoformat() if claim.revoked_at else None,
    }


def _audit(db, *, actor, claim, action, before, after, reason=None,
           evidence_type=None, evidence_reference=None) -> CompanyAuditEvent:
    event = CompanyAuditEvent(
        actor_user_id=getattr(actor, "id", None),
        company_id=claim.company_id if claim else None,
        target_type="vessel_role_claim",
        target_id=claim.id if claim else None,
        action=action,
        before_state=before,
        after_state=after,
        reason=reason,
        evidence_type=evidence_type,
        evidence_reference=evidence_reference,
    )
    db.add(event)
    return event


def _transition(claim: VesselRoleClaim, to_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(claim.status, frozenset())
    if to_status not in allowed:
        raise ClaimError(
            f"A claim cannot go from {claim.status} to {to_status}. "
            f"Allowed from {claim.status}: "
            + (", ".join(sorted(allowed)) if allowed else "nothing, it is final")
        )
    claim.status = to_status


# --- submission --------------------------------------------------------------

def submit_claim(
    db: Session, *, company_id: int, vessel_id: int, role: str,
    effective_start: datetime, effective_end: Optional[datetime] = None,
    submitted_by, imo_number: Optional[str] = None,
    evidence_type: Optional[str] = None, evidence_reference: Optional[str] = None,
) -> VesselRoleClaim:
    """A company asks for a role on a vessel. It is never approved by asking."""
    canonical_role = validate_role(role)

    company = db.query(ShippingCompany).filter(ShippingCompany.id == company_id).first()
    if company is None:
        raise ClaimError("No such company")
    if company.status != "VERIFIED":
        # An unverified company's claim would be meaningless even if approved,
        # since the policy refuses every event for it anyway.
        raise ClaimError("Only a verified company may claim a vessel")

    if effective_end is not None and effective_end <= effective_start:
        raise ClaimError("A claim's end must come after its start")

    claim = VesselRoleClaim(
        company_id=company_id, vessel_id=vessel_id, role=canonical_role,
        imo_number=imo_number,
        effective_start=effective_start, effective_end=effective_end,
        status="SUBMITTED",
        evidence_type=evidence_type, evidence_reference=evidence_reference,
    )
    db.add(claim)
    db.flush()
    _audit(db, actor=submitted_by, claim=claim, action="submitted",
           before=None, after=_interval(claim),
           evidence_type=evidence_type, evidence_reference=evidence_reference)
    return claim


def begin_review(db: Session, *, claim: VesselRoleClaim, actor) -> VesselRoleClaim:
    _require_superadmin(actor)
    before = _interval(claim)
    _transition(claim, "UNDER_REVIEW")
    db.flush()
    _audit(db, actor=actor, claim=claim, action="review_started",
           before=before, after=_interval(claim))
    return claim


# --- decisions ---------------------------------------------------------------

def approve_claim(
    db: Session, *, claim: VesselRoleClaim, actor,
    evidence_type: str, evidence_reference: str, reason: Optional[str] = None,
) -> VesselRoleClaim:
    """Approve, or refuse without describing the competitor.

    The overlap is caught by the database rather than by a prior query: a query
    would return the conflicting row, and the natural next step is to put it in
    the error message. The exclusion constraint refuses the write and tells us
    nothing, which is exactly the right amount.
    """
    _require_superadmin(actor)
    if not (evidence_type or "").strip() or not (evidence_reference or "").strip():
        # Section 21 requires evidence on every approval, and the role decides
        # which evidence is acceptable -- see app/services/company_roles.py.
        raise ClaimError("An approval must record the evidence it rests on")

    before = _interval(claim)
    _transition(claim, "APPROVED")
    claim.evidence_type = evidence_type
    claim.evidence_reference = evidence_reference
    claim.approved_by_user_id = getattr(actor, "id", None)
    claim.approved_at = _utc_now()

    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        claim.status = before["status"] if before else "UNDER_REVIEW"
        claim.approved_at = None
        claim.approved_by_user_id = None
        raise ClaimConflict() from None

    _audit(db, actor=actor, claim=claim, action="approved",
           before=before, after=_interval(claim), reason=reason,
           evidence_type=evidence_type, evidence_reference=evidence_reference)
    return claim


def reject_claim(db: Session, *, claim, actor, reason: str) -> VesselRoleClaim:
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise ClaimError("A rejection must record why")
    before = _interval(claim)
    _transition(claim, "REJECTED")
    claim.review_reason = reason
    db.flush()
    _audit(db, actor=actor, claim=claim, action="rejected",
           before=before, after=_interval(claim), reason=reason)
    return claim


def request_more_evidence(db: Session, *, claim, actor, reason: str) -> VesselRoleClaim:
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise ClaimError("Say what evidence is missing")
    before = _interval(claim)
    _transition(claim, "MORE_EVIDENCE")
    claim.review_reason = reason
    db.flush()
    _audit(db, actor=actor, claim=claim, action="more_evidence_requested",
           before=before, after=_interval(claim), reason=reason)
    return claim


def revoke_claim(db: Session, *, claim, actor, reason: str) -> VesselRoleClaim:
    """Withdraw an approved claim. The row stays; the access stops."""
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise ClaimError("A revocation must record why")
    before = _interval(claim)
    _transition(claim, "REVOKED")
    claim.revoked_at = _utc_now()
    claim.review_reason = reason
    db.flush()
    _audit(db, actor=actor, claim=claim, action="revoked",
           before=before, after=_interval(claim), reason=reason)
    return claim


def correct_claim(
    db: Session, *, claim: VesselRoleClaim, actor, reason: str,
    effective_start: Optional[datetime] = None,
    effective_end: Optional[datetime] = None,
) -> VesselRoleClaim:
    """Replace an approved claim's interval without erasing the old one.

    Section 21: "avoid in-place changes that erase who previously held the
    role." The original becomes CORRECTED and a new APPROVED claim carries the
    revised interval, so the history reads as a sequence of decisions rather
    than a single row that quietly changed.
    """
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise ClaimError("A correction must record why")

    before = _interval(claim)
    _transition(claim, "CORRECTED")
    claim.review_reason = reason
    claim.revoked_at = _utc_now()
    db.flush()

    replacement = VesselRoleClaim(
        company_id=claim.company_id, vessel_id=claim.vessel_id, role=claim.role,
        imo_number=claim.imo_number,
        effective_start=effective_start or claim.effective_start,
        effective_end=effective_end if effective_end is not None else claim.effective_end,
        status="APPROVED",
        evidence_type=claim.evidence_type,
        evidence_reference=claim.evidence_reference,
        approved_by_user_id=getattr(actor, "id", None),
        approved_at=_utc_now(),
    )
    db.add(replacement)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise ClaimConflict() from None

    _audit(db, actor=actor, claim=claim, action="corrected",
           before=before, after=_interval(replacement), reason=reason)
    return replacement


def claim_intervals_cover(claims, start, end):
    """Union coverage, without double-counting multiple roles or bridging gaps."""
    cursor = start
    for claim in sorted(claims, key=lambda c: c.effective_start):
        if claim.effective_start > cursor:
            return False
        if claim.effective_end is None:
            return True
        cursor = max(cursor, claim.effective_end)
        if end is not None and cursor >= end:
            return True
    return False
