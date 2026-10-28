"""Super Admin's side of the company claim workflow — Document 2 sections 10, 21.

The asymmetry with `routes_shipping_company.py` is the point. A company sees
only its own claims and never learns who it collided with; Super Admin sees the
conflict queue with both sides, because resolving a conflict is impossible
without them.

Registered under the existing `/api/v1/superadmin` prefix, matching the
convention the rest of the Super Admin surface already uses.
"""

from app.services.collection_pages import page_all

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.db.models.shipping_company import (
    CompanyAuditEvent,
    CompanyUserMembership,
    SensitiveAccessGrant,
    SensitiveAccessRequest,
    ShippingCompany,
    VesselRoleClaim,
)
from app.db.models.user import User
from app.db.session import get_db
from app.services import company_claims as claims
from app.services import company_sensitive_access as access
from app.services import company_verification as verification
from app.services.company_claims import ClaimConflict, ClaimError

router = APIRouter()

OPEN_STATUSES = ("SUBMITTED", "UNDER_REVIEW", "MORE_EVIDENCE")


def _require_superadmin(current_user: User) -> User:
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Super Admin only")
    return current_user


class ReviewClaimOut(BaseModel):
    id: int
    company_id: int
    company_name: str
    vessel_id: int
    imo_number: Optional[str] = None
    role: str
    effective_start: datetime
    effective_end: Optional[datetime] = None
    status: str
    evidence_type: Optional[str] = None
    evidence_reference: Optional[str] = None
    review_note: Optional[str] = None
    # Both sides, which only this surface ever sees.
    conflicts_with: List[int] = []
    conflict_count: int = 0
    conflicts_truncated: bool = False


class DecisionIn(BaseModel):
    reason: Optional[str] = None
    evidence_type: Optional[str] = None
    evidence_reference: Optional[str] = None


class CorrectionIn(BaseModel):
    reason: str = Field(min_length=1)
    effective_start: Optional[datetime] = None
    effective_end: Optional[datetime] = None


class AuditEventOut(BaseModel):
    id: int
    action: str
    actor_user_id: Optional[int] = None
    reason: Optional[str] = None
    evidence_type: Optional[str] = None
    evidence_reference: Optional[str] = None
    before_state: Optional[dict] = None
    after_state: Optional[dict] = None
    created_at: datetime


def _overlap_query(db: Session, claim: VesselRoleClaim):
    """Approved claims this one would collide with.

    The same predicate as the database's exclusion constraint, expressed in
    SQL so the queue can show what the constraint would refuse. Only ever
    reached from a Super Admin route -- running it for a company would hand
    them the competitor the constraint deliberately hides.
    """
    from sqlalchemy import and_, or_

    end = claim.effective_end
    others = (
        db.query(VesselRoleClaim)
        .filter(
            VesselRoleClaim.id != claim.id,
            VesselRoleClaim.vessel_id == claim.vessel_id,
            VesselRoleClaim.role == claim.role,
            VesselRoleClaim.status == "APPROVED",
            VesselRoleClaim.revoked_at.is_(None),
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > claim.effective_start,
            ),
        )
    )
    if end is not None:
        others = others.filter(VesselRoleClaim.effective_start < end)
    return others


def _overlapping_approved(db: Session, claim: VesselRoleClaim) -> List[int]:
    return [
        row.id
        for row in _overlap_query(db, claim)
        .order_by(VesselRoleClaim.id)
        .limit(20)
        .all()
    ]


def _serialize(db: Session, claim: VesselRoleClaim, company_name: str) -> ReviewClaimOut:
    return ReviewClaimOut(
        id=claim.id,
        company_id=claim.company_id,
        company_name=company_name,
        vessel_id=claim.vessel_id,
        imo_number=claim.imo_number,
        role=claim.role,
        effective_start=claim.effective_start,
        effective_end=claim.effective_end,
        status=claim.status,
        evidence_type=claim.evidence_type,
        evidence_reference=claim.evidence_reference,
        review_note=claim.review_reason,
        conflicts_with=_overlapping_approved(db, claim),
        conflict_count=_overlap_query(db, claim).count(),
        conflicts_truncated=_overlap_query(db, claim).count() > 20,
    )


def _load(db: Session, claim_id: int) -> tuple:
    claim = db.query(VesselRoleClaim).filter(VesselRoleClaim.id == claim_id).first()
    if claim is None:
        raise HTTPException(status_code=404, detail="Claim not found")
    company = db.query(ShippingCompany).filter(
        ShippingCompany.id == claim.company_id).first()
    return claim, (company.legal_name if company else "")


@router.get("/company-claims", response_model=List[ReviewClaimOut])
def review_queue(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The review queue. Open claims by default, with their conflicts."""
    _require_superadmin(current_user)
    query = db.query(VesselRoleClaim, ShippingCompany.legal_name).join(
        ShippingCompany, ShippingCompany.id == VesselRoleClaim.company_id)
    query = query.filter(
        VesselRoleClaim.status == status
        if status
        else VesselRoleClaim.status.in_(OPEN_STATUSES)
    )
    rows = page_all(
        query.order_by(VesselRoleClaim.created_at),
        collection="routes_company_review.review_queue.0",
        label="Review queue",
    )
    return [_serialize(db, claim, name) for claim, name in rows]


def _decide(db, claim_id, current_user, action, **kwargs):
    _require_superadmin(current_user)
    claim, company_name = _load(db, claim_id)
    try:
        action(db, claim=claim, actor=current_user, **kwargs)
    except ClaimConflict as conflict:
        db.rollback()
        # 409 and the service's generic message. Super Admin can see the
        # conflicting ids through the queue; this response does not carry them,
        # so the same handler is safe if it is ever reached another way.
        raise HTTPException(status_code=409, detail=str(conflict)) from None
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(claim)
    return _serialize(db, claim, company_name)


@router.post("/company-claims/{claim_id}/review", response_model=ReviewClaimOut)
def start_review(claim_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    return _decide(db, claim_id, current_user, claims.begin_review)


@router.post("/company-claims/{claim_id}/approve", response_model=ReviewClaimOut)
def approve(claim_id: int, body: DecisionIn, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    return _decide(
        db, claim_id, current_user, claims.approve_claim,
        evidence_type=body.evidence_type or "", 
        evidence_reference=body.evidence_reference or "",
        reason=body.reason,
    )


@router.post("/company-claims/{claim_id}/reject", response_model=ReviewClaimOut)
def reject(claim_id: int, body: DecisionIn, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    return _decide(db, claim_id, current_user, claims.reject_claim,
                   reason=body.reason or "")


@router.post("/company-claims/{claim_id}/more-evidence", response_model=ReviewClaimOut)
def more_evidence(claim_id: int, body: DecisionIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    return _decide(db, claim_id, current_user, claims.request_more_evidence,
                   reason=body.reason or "")


@router.post("/company-claims/{claim_id}/revoke", response_model=ReviewClaimOut)
def revoke(claim_id: int, body: DecisionIn, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    return _decide(db, claim_id, current_user, claims.revoke_claim,
                   reason=body.reason or "")


@router.post("/company-claims/{claim_id}/correct", response_model=ReviewClaimOut)
def correct(claim_id: int, body: CorrectionIn, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """Replaces the claim rather than editing it, so the old interval survives."""
    _require_superadmin(current_user)
    claim, company_name = _load(db, claim_id)
    try:
        replacement = claims.correct_claim(
            db, claim=claim, actor=current_user, reason=body.reason,
            effective_start=body.effective_start, effective_end=body.effective_end,
        )
    except ClaimConflict as conflict:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(conflict)) from None
    except ClaimError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(replacement)
    return _serialize(db, replacement, company_name)


@router.get("/company-claims/{claim_id}/audit", response_model=List[AuditEventOut])
def claim_audit(claim_id: int, db: Session = Depends(get_db),
                current_user: User = Depends(get_current_user)):
    """Every decision on this claim, oldest first. Append-only, so complete."""
    _require_superadmin(current_user)
    rows = page_all(
        db.query(CompanyAuditEvent)
        .filter(
            CompanyAuditEvent.target_type == "vessel_role_claim",
            CompanyAuditEvent.target_id == claim_id,
        )
        .order_by(CompanyAuditEvent.id),
        collection="routes_company_review.claim_audit.0",
        label="Claim audit",
    )
    return [AuditEventOut.model_validate(row, from_attributes=True) for row in rows]


# --- sensitive access requests ----------------------------------------------
#
# The company can ask (routes_shipping_company.py) and can list its own asks.
# Nothing could decide one until now, which meant no grant could ever exist --
# and with AGGREGATE_SAFE_CATEGORIES empty pending Q9, no category was visible
# to any company at all. These are the routes that close that loop.


class AccessRequestReviewOut(BaseModel):
    id: int
    company_id: int
    company_name: str
    vessel_call_id: int
    category: str
    requested_start: datetime
    requested_end: datetime
    reason: str
    status: str
    requested_by_user_id: Optional[int] = None
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    grant_id: Optional[int] = None
    # Set only while a grant is live, so the queue distinguishes an approved
    # request from one whose grant has since been revoked or has run out.
    grant_active: bool = False


class GrantIn(BaseModel):
    reason: str = Field(min_length=1)
    # Omitted means "exactly what was requested". Narrower is allowed; wider
    # is refused by the service.
    effective_start: Optional[datetime] = None
    effective_end: Optional[datetime] = None


def _serialize_request(db: Session, row, company_name: str) -> AccessRequestReviewOut:
    grant = None
    if row.grant_id:
        grant = (
            db.query(SensitiveAccessGrant)
            .filter(SensitiveAccessGrant.id == row.grant_id)
            .first()
        )
    now = datetime.now(timezone.utc)
    active = bool(
        grant is not None
        and grant.revoked_at is None
        and _as_utc(grant.effective_end) > now
        and _as_utc(grant.effective_start) <= now
    )
    return AccessRequestReviewOut(
        id=row.id, company_id=row.company_id, company_name=company_name,
        vessel_call_id=row.vessel_call_id, category=row.category,
        requested_start=row.requested_start, requested_end=row.requested_end,
        reason=row.reason, status=row.status,
        requested_by_user_id=row.requested_by_user_id,
        decided_at=row.decided_at, decision_reason=row.decision_reason,
        grant_id=row.grant_id, grant_active=active,
    )


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _load_request(db: Session, request_id: int) -> tuple:
    row = (
        db.query(SensitiveAccessRequest)
        .filter(SensitiveAccessRequest.id == request_id)
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Access request not found")
    company = db.query(ShippingCompany).filter(
        ShippingCompany.id == row.company_id).first()
    return row, (company.legal_name if company else "")


@router.get("/access-requests", response_model=List[AccessRequestReviewOut])
def access_request_queue(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Open requests by default, oldest first, so the queue is a work list."""
    _require_superadmin(current_user)
    query = (
        db.query(SensitiveAccessRequest, ShippingCompany.legal_name)
        .join(ShippingCompany, ShippingCompany.id == SensitiveAccessRequest.company_id)
    )
    if status:
        query = query.filter(SensitiveAccessRequest.status == status.upper())
    else:
        query = query.filter(SensitiveAccessRequest.status.in_(access.OPEN_STATUSES))
    rows = page_all(
        query.order_by(SensitiveAccessRequest.created_at),
        collection="routes_company_review.access_request_queue.0",
        label="Access request queue",
    )
    return [_serialize_request(db, row, name) for row, name in rows]


def _decide_request(db, request_id, current_user, action, **kwargs):
    _require_superadmin(current_user)
    row, company_name = _load_request(db, request_id)
    try:
        action(db, request=row, actor=current_user, **kwargs)
    except access.AccessDecisionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.refresh(row)
    return _serialize_request(db, row, company_name)


@router.post("/access-requests/{request_id}/review",
             response_model=AccessRequestReviewOut)
def start_access_review(request_id: int, db: Session = Depends(get_db),
                        current_user: User = Depends(get_current_user)):
    return _decide_request(db, request_id, current_user, access.begin_review)


@router.post("/access-requests/{request_id}/approve",
             response_model=AccessRequestReviewOut)
def approve_access(request_id: int, body: GrantIn, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Creates the grant. The only route in the app that does."""
    return _decide_request(
        db, request_id, current_user, access.approve, reason=body.reason,
        effective_start=body.effective_start, effective_end=body.effective_end,
    )


@router.post("/access-requests/{request_id}/reject",
             response_model=AccessRequestReviewOut)
def reject_access(request_id: int, body: DecisionIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    return _decide_request(db, request_id, current_user, access.reject,
                           reason=body.reason or "")


@router.post("/access-requests/{request_id}/revoke",
             response_model=AccessRequestReviewOut)
def revoke_access(request_id: int, body: DecisionIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    return _decide_request(db, request_id, current_user, access.revoke,
                           reason=body.reason or "")


@router.get("/access-requests/{request_id}/audit", response_model=List[AuditEventOut])
def access_request_audit(request_id: int, db: Session = Depends(get_db),
                         current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    rows = page_all(
        db.query(CompanyAuditEvent)
        .filter(
            CompanyAuditEvent.target_type == "sensitive_access_request",
            CompanyAuditEvent.target_id == request_id,
        )
        .order_by(CompanyAuditEvent.id),
        collection="routes_company_review.access_request_audit.0",
        label="Access request audit",
    )
    return [AuditEventOut.model_validate(row, from_attributes=True) for row in rows]


# --- company verification ----------------------------------------------------
#
# `app/services/company_verification.py` was complete, mutation-tested and
# called by nothing: no route, script or screen reached it, so a company's
# status could only be set by writing the row by hand. These are its callers.
#
# Section 21's prohibited shortcut — "a self-registered user cannot set
# verified status" — is enforced twice over: the service refuses a non-Super
# Admin actor, and the company-side router has no verification route at all,
# only a request.


class CompanyOut(BaseModel):
    id: int
    legal_name: str
    status: str
    member_count: int
    claim_count: int
    verified_at: Optional[datetime] = None
    created_at: datetime


class VerifyIn(BaseModel):
    evidence_type: str = Field(min_length=1)
    evidence_reference: str = Field(min_length=1)
    reason: Optional[str] = None


PENDING_COMPANY_STATUSES = ("DRAFT", "PENDING_VERIFICATION")


def _serialize_company(db: Session, company: ShippingCompany) -> CompanyOut:
    members = (
        db.query(func.count(CompanyUserMembership.id))
        .filter(CompanyUserMembership.company_id == company.id,
                CompanyUserMembership.enabled.is_(True))
        .scalar()
    )
    claim_rows = (
        db.query(func.count(VesselRoleClaim.id))
        .filter(VesselRoleClaim.company_id == company.id)
        .scalar()
    )
    return CompanyOut(
        id=company.id, legal_name=company.legal_name, status=company.status,
        member_count=int(members or 0), claim_count=int(claim_rows or 0),
        verified_at=company.verified_at, created_at=company.created_at,
    )


def _load_company(db: Session, company_id: int) -> ShippingCompany:
    company = (
        db.query(ShippingCompany).filter(ShippingCompany.id == company_id).first()
    )
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@router.get("/companies", response_model=List[CompanyOut])
def company_queue(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Registrations awaiting a decision by default, oldest first."""
    _require_superadmin(current_user)
    query = db.query(ShippingCompany)
    if status:
        query = query.filter(ShippingCompany.status == status.upper())
    else:
        query = query.filter(ShippingCompany.status.in_(PENDING_COMPANY_STATUSES))
    return [
        _serialize_company(db, row)
        for row in page_all(
            query.order_by(ShippingCompany.created_at),
            collection="routes_company_review.company_queue.0",
            label="Company queue",
        )
    ]


def _decide_company(db, company_id, current_user, action, **kwargs):
    _require_superadmin(current_user)
    company = _load_company(db, company_id)
    try:
        action(db, company=company, actor=current_user, **kwargs)
    except verification.VerificationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(company)
    return _serialize_company(db, company)


@router.post("/companies/{company_id}/verify", response_model=CompanyOut)
def verify_company(company_id: int, body: VerifyIn, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """The only route in the app that can set VERIFIED."""
    return _decide_company(
        db, company_id, current_user, verification.verify,
        evidence_type=body.evidence_type, evidence_reference=body.evidence_reference,
        reason=body.reason,
    )


@router.post("/companies/{company_id}/reject", response_model=CompanyOut)
def reject_company(company_id: int, body: DecisionIn, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    return _decide_company(db, company_id, current_user, verification.reject,
                           reason=body.reason or "")


@router.post("/companies/{company_id}/suspend", response_model=CompanyOut)
def suspend_company(company_id: int, body: DecisionIn, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    """Access stops at once. Claims, grants and memberships are untouched."""
    return _decide_company(db, company_id, current_user, verification.suspend,
                           reason=body.reason or "")


@router.post("/companies/{company_id}/reinstate", response_model=CompanyOut)
def reinstate_company(company_id: int, body: DecisionIn, db: Session = Depends(get_db),
                      current_user: User = Depends(get_current_user)):
    return _decide_company(db, company_id, current_user, verification.reinstate,
                           reason=body.reason or "")


@router.post("/companies/{company_id}/close", response_model=CompanyOut)
def close_company(company_id: int, body: DecisionIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Terminal, and reachable only from SUSPENDED — never in one click."""
    return _decide_company(db, company_id, current_user, verification.close,
                           reason=body.reason or "")


@router.get("/companies/{company_id}/audit", response_model=List[AuditEventOut])
def company_audit(company_id: int, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    rows = page_all(
        db.query(CompanyAuditEvent)
        .filter(
            CompanyAuditEvent.target_type == "shipping_company",
            CompanyAuditEvent.target_id == company_id,
        )
        .order_by(CompanyAuditEvent.id),
        collection="routes_company_review.company_audit.0",
        label="Company audit",
    )
    return [AuditEventOut.model_validate(row, from_attributes=True) for row in rows]


@router.get("/company-claims/{claim_id}/conflicts")
def claim_conflicts(
    claim_id: int,
    offset: int = 0,
    limit: int = 20,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_superadmin(current_user)
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid conflict pagination")
    claim, _ = _load(db, claim_id)
    query = _overlap_query(db, claim)
    total = query.count()
    rows = query.order_by(VesselRoleClaim.id).offset(offset).limit(limit).all()
    companies = {
        c.id: c.legal_name
        for c in db.query(ShippingCompany)
        .filter(ShippingCompany.id.in_([r.company_id for r in rows]))
        .all()
    }
    return {
        "items": [
            {
                "id": r.id,
                "company_name": companies.get(r.company_id, ""),
                "effective_start": r.effective_start,
                "effective_end": r.effective_end,
            }
            for r in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }
