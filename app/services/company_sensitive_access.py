"""Deciding a company's request to see detail on one port call — Document 2 §4, §21.

The company side of this already exists: `POST /shipping-company/access-requests`
creates a REQUESTED row and nothing else, because section 21 forbids the
shortcut "Company admin cannot grant itself detail". This module is the other
half — the only path by which a request becomes a `SensitiveAccessGrant`.

Until Q9 is answered, `AGGREGATE_SAFE_CATEGORIES` is empty, so a grant made
here is the *only* thing that makes any category visible to a company. That
makes two rules load-bearing rather than decorative:

**The grant never exceeds what was asked for.** A Super Admin may narrow the
window or approve it as requested; a window reaching outside the request is
refused. Approving more than a company asked for is a disclosure nobody
requested, and the request row is the record of why it was justified.

**The category is the request's own.** It cannot be substituted at approval
time, for the same reason.

What this module deliberately does *not* do is clamp the grant to tenure.
`company_access.allow()` requires a covering claim *and* a covering grant, so
the intersection happens at read time on every single read. Clamping here as
well would be a second, staler copy of tenure that a later claim correction
would not reach.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.shipping_company import (
    CompanyAuditEvent,
    SensitiveAccessGrant,
    SensitiveAccessRequest,
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"UNDER_REVIEW", "APPROVED", "REJECTED"}),
    "UNDER_REVIEW": frozenset({"APPROVED", "REJECTED"}),
    # An approved request can lose its grant, by hand or by time.
    "APPROVED": frozenset({"REVOKED", "EXPIRED"}),
    "REJECTED": frozenset(),
    "REVOKED": frozenset(),
    "EXPIRED": frozenset(),
}

OPEN_STATUSES = ("REQUESTED", "UNDER_REVIEW")


class AccessDecisionError(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_superadmin(actor) -> None:
    if getattr(actor, "role", None) != "superadmin":
        raise AccessDecisionError(
            "Only a Super Admin may decide an access request. A company "
            "cannot grant itself detail."
        )


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _transition(request: SensitiveAccessRequest, to_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(request.status, frozenset())
    if to_status not in allowed:
        raise AccessDecisionError(
            f"An access request cannot go from {request.status} to {to_status}. "
            f"Allowed from {request.status}: "
            + (", ".join(sorted(allowed)) if allowed else "nothing, it is final")
        )
    request.status = to_status


def _state(request: SensitiveAccessRequest) -> dict:
    return {
        "status": request.status,
        "category": request.category,
        "company_id": request.company_id,
        "vessel_call_id": request.vessel_call_id,
        "requested_start": request.requested_start.isoformat() if request.requested_start else None,
        "requested_end": request.requested_end.isoformat() if request.requested_end else None,
        "grant_id": request.grant_id,
    }


def _audit(db, *, actor, request, action, before, after, reason=None) -> CompanyAuditEvent:
    event = CompanyAuditEvent(
        actor_user_id=getattr(actor, "id", None),
        company_id=request.company_id,
        target_type="sensitive_access_request",
        target_id=request.id,
        action=action,
        before_state=before,
        after_state=after,
        reason=reason,
    )
    db.add(event)
    return event


def begin_review(db: Session, *, request: SensitiveAccessRequest, actor):
    _require_superadmin(actor)
    before = _state(request)
    _transition(request, "UNDER_REVIEW")
    _audit(db, actor=actor, request=request, action="access_review_started",
           before=before, after=_state(request))
    db.commit()
    db.refresh(request)
    return request


def approve(
    db: Session,
    *,
    request: SensitiveAccessRequest,
    actor,
    reason: str,
    effective_start: Optional[datetime] = None,
    effective_end: Optional[datetime] = None,
) -> SensitiveAccessGrant:
    """Create the grant this request asked for, no wider than it asked.

    Returns the grant; the request is updated in place to APPROVED and points
    at it. Omitting the window approves exactly what was requested.
    """
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise AccessDecisionError("An approval must record why")

    asked_start = _aware(request.requested_start)
    asked_end = _aware(request.requested_end)
    start = _aware(effective_start) or asked_start
    end = _aware(effective_end) or asked_end

    if end <= start:
        raise AccessDecisionError("The end must come after the start")
    if start < asked_start or end > asked_end:
        raise AccessDecisionError(
            "A grant cannot reach outside the window that was requested. "
            "Narrow it, or ask the company to request the wider period."
        )

    before = _state(request)
    grant = SensitiveAccessGrant(
        company_id=request.company_id,
        vessel_call_id=request.vessel_call_id,
        # The request's own category. Never the reviewer's.
        category=request.category,
        effective_start=start,
        effective_end=end,
        granted_by_user_id=getattr(actor, "id", None),
        reason=reason,
    )
    db.add(grant)
    db.flush()

    _transition(request, "APPROVED")
    request.grant_id = grant.id
    request.decided_by_user_id = getattr(actor, "id", None)
    request.decided_at = _utc_now()
    request.decision_reason = reason
    _audit(db, actor=actor, request=request, action="access_approved",
           before=before, after=_state(request), reason=reason)
    db.commit()
    db.refresh(grant)
    db.refresh(request)
    return grant


def reject(db: Session, *, request: SensitiveAccessRequest, actor, reason: str):
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise AccessDecisionError("A rejection must record why")
    before = _state(request)
    _transition(request, "REJECTED")
    request.decided_by_user_id = getattr(actor, "id", None)
    request.decided_at = _utc_now()
    request.decision_reason = reason
    _audit(db, actor=actor, request=request, action="access_rejected",
           before=before, after=_state(request), reason=reason)
    db.commit()
    db.refresh(request)
    return request


def revoke(db: Session, *, request: SensitiveAccessRequest, actor, reason: str):
    """Withdraw an approved grant. The request row stays as the record."""
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise AccessDecisionError("A revocation must record why")
    before = _state(request)
    _transition(request, "REVOKED")
    grant = (
        db.query(SensitiveAccessGrant)
        .filter(SensitiveAccessGrant.id == request.grant_id)
        .first()
        if request.grant_id
        else None
    )
    if grant is not None and grant.revoked_at is None:
        grant.revoked_at = _utc_now()
    request.decision_reason = reason
    _audit(db, actor=actor, request=request, action="access_revoked",
           before=before, after=_state(request), reason=reason)
    db.commit()
    db.refresh(request)
    return request
