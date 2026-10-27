"""The one place that decides what a shipping company may see.

Document 2 section 7 is normative and states the rule almost as code:

    ALLOW(event, company, viewer, category, grant) iff
        company VERIFIED
        and viewer is an active member
        and the event's vessel maps to the canonical vessel
        and an APPROVED, non-revoked claim exists for company+vessel+role
            with the event timestamp inside [start, end)
        and the event's origin is resolved
        and (the category is aggregate-safe
             or an unexpired Super Admin grant covers this exact call,
                category and time)

Two things about that rule drive the shape of this module.

**It is one predicate, not a set of checks per screen.** Every company endpoint
calls `allow()`. The document requires source rows to be filtered *before*
GROUP BY and before dimension joins -- compute a fleet-wide total and then
filter the labels and you have published the aggregate while appearing not to.
So screens are built on top of this; it is never added to a screen afterwards.

**Time is the hard part.** An event is attributed by its own occurrence
timestamp, never by the port call's ETA or the report period, so a port call
spanning a handover splits between two companies. `[start, end)` is half-open,
so an event at the boundary instant belongs to the incoming company and to
exactly one of them.

Open and deliberately not guessed:

* **Which roles exist is settled** (owner answer to Q5: Registered/Legal
  Owner, ISM Manager, Commercial Manager) and lives in
  `app.services.company_roles`. This module still never enumerates them: it
  asks whether a claim covers an instant, not whether a role is spelled
  correctly, and additional roles are expected later. Callers passing `role`
  must pass a canonical code, because the comparison is exact.
* **Which categories are aggregate-safe (Q9, still RECOMMENDED).**
  `AGGREGATE_SAFE_CATEGORIES` is empty on purpose. Until it is agreed, every
  category requires an explicit grant, which fails closed.
* **The canonical occurrence timestamp per event type.** The document warns
  against picking "whichever makes it visible". The caller supplies the
  timestamp; choosing it per event type is a decision this module must not make
  silently on anyone's behalf.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.services.company_event_dictionary import (
    UnknownEventFamily,
    event_time,
)
from app.db.models.shipping_company import (
    CompanyUserMembership,
    SensitiveAccessGrant,
    ShippingCompany,
    VesselRoleClaim,
)

# Categories that may be shown as an aggregate without a per-call grant.
# Empty until Q9 is decided: an unknown category is treated as sensitive, so
# the unanswered question costs visibility rather than privacy.
AGGREGATE_SAFE_CATEGORIES: frozenset[str] = frozenset()


class Decision:
    """Why access was allowed or refused.

    Reasons are for logs and tests, never for the API response: telling a
    caller *which* condition failed describes other companies' tenure to them.
    """

    ALLOWED = "allowed"
    COMPANY_NOT_VERIFIED = "company_not_verified"
    VIEWER_NOT_A_MEMBER = "viewer_not_an_active_member"
    NO_VESSEL = "event_has_no_canonical_vessel"
    NO_TIMESTAMP = "event_has_no_occurrence_timestamp"
    ORIGIN_UNRESOLVED = "event_origin_unresolved"
    OUTSIDE_TENURE = "no_approved_claim_covers_this_instant"
    CATEGORY_NEEDS_GRANT = "category_is_not_aggregate_safe_and_no_grant_covers_it"
    FAMILY_HAS_NO_USABLE_TIME = "event_family_has_no_authoritative_timestamp"


@dataclass(frozen=True)
class AccessResult:
    allowed: bool
    reason: str
    claim_id: Optional[int] = None
    grant_id: Optional[int] = None

    def __bool__(self) -> bool:
        return self.allowed


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """Read a naive timestamp as UTC; older rows were written without one."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def covering_claim(
    db: Session,
    *,
    company_id: int,
    vessel_id: int,
    occurred_at: datetime,
    role: Optional[str] = None,
) -> Optional[VesselRoleClaim]:
    """The approved claim that covers this instant, if there is one.

    Half-open: `effective_start <= occurred_at < effective_end`, and a null
    end means ongoing. `role` narrows the search; left out, any approved role
    qualifies, which is what a company holding several roles on one vessel
    needs. How overlapping roles *combine* for reporting is Q8 and is not
    decided here -- this answers only whether the instant is inside tenure.
    """
    moment = _aware(occurred_at)
    if moment is None:
        return None
    query = (
        db.query(VesselRoleClaim)
        .filter(
            VesselRoleClaim.company_id == company_id,
            VesselRoleClaim.vessel_id == vessel_id,
            VesselRoleClaim.status == "APPROVED",
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.effective_start <= moment,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > moment,
            ),
        )
        .order_by(VesselRoleClaim.effective_start)
    )
    if role is not None:
        query = query.filter(VesselRoleClaim.role == role)
    return query.first()


def covering_grant(
    db: Session,
    *,
    company_id: int,
    vessel_call_id: Optional[int],
    category: str,
    occurred_at: datetime,
) -> Optional[SensitiveAccessGrant]:
    """An unexpired grant for this exact call, category and instant."""
    moment = _aware(occurred_at)
    if moment is None or vessel_call_id is None:
        return None
    return (
        db.query(SensitiveAccessGrant)
        .filter(
            SensitiveAccessGrant.company_id == company_id,
            SensitiveAccessGrant.vessel_call_id == vessel_call_id,
            SensitiveAccessGrant.category == category,
            SensitiveAccessGrant.revoked_at.is_(None),
            SensitiveAccessGrant.effective_start <= moment,
            SensitiveAccessGrant.effective_end > moment,
        )
        .first()
    )


def viewer_is_active_member(db: Session, *, company_id: int, user_id: int) -> bool:
    return (
        db.query(CompanyUserMembership.id)
        .filter(
            CompanyUserMembership.company_id == company_id,
            CompanyUserMembership.user_id == user_id,
            CompanyUserMembership.enabled.is_(True),
        )
        .first()
        is not None
    )


def allow(
    db: Session,
    *,
    company_id: int,
    viewer_user_id: int,
    vessel_id: Optional[int],
    occurred_at: Optional[datetime],
    category: str,
    vessel_call_id: Optional[int] = None,
    role: Optional[str] = None,
    origin_resolved: bool = True,
    event_family: Optional[str] = None,
) -> AccessResult:
    """Whether this company's viewer may see this event, in this category.

    `occurred_at` is the event's own canonical occurrence timestamp, chosen by
    the caller per event type. Passing the port call's start, its ETA or the
    report period is the mistake Document 2 names explicitly: it hands a
    company an entire call that it only partly held.

    `event_family` is checked against the event dictionary when given. A family
    with no authoritative timestamp -- shore pass approval and crew sign-on/off,
    neither of which this schema records -- is refused here rather than left to
    each caller to remember, because the failure mode is a caller passing some
    nearby timestamp and the row appearing on the wrong company's dashboard.

    `origin_resolved` is false for the legacy rows whose agency context has
    never been reconciled. The document requires those to be denied and left
    out of tenure-scoped aggregates until evidence-backed reconciliation, so an
    unresolved row is refused here rather than quietly included.
    """
    if event_family is not None:
        try:
            if not event_time(event_family).usable:
                return AccessResult(False, Decision.FAMILY_HAS_NO_USABLE_TIME)
        except UnknownEventFamily:
            # An unlisted family has not been through schema inspection, so
            # nothing here knows which of its timestamps is authoritative.
            return AccessResult(False, Decision.FAMILY_HAS_NO_USABLE_TIME)

    company = db.query(ShippingCompany).filter(ShippingCompany.id == company_id).first()
    if company is None or company.status != "VERIFIED":
        return AccessResult(False, Decision.COMPANY_NOT_VERIFIED)

    if not viewer_is_active_member(db, company_id=company_id, user_id=viewer_user_id):
        return AccessResult(False, Decision.VIEWER_NOT_A_MEMBER)

    if vessel_id is None:
        return AccessResult(False, Decision.NO_VESSEL)

    if occurred_at is None:
        return AccessResult(False, Decision.NO_TIMESTAMP)

    if not origin_resolved:
        return AccessResult(False, Decision.ORIGIN_UNRESOLVED)

    claim = covering_claim(
        db, company_id=company_id, vessel_id=vessel_id,
        occurred_at=occurred_at, role=role,
    )
    if claim is None:
        return AccessResult(False, Decision.OUTSIDE_TENURE)

    if category in AGGREGATE_SAFE_CATEGORIES:
        return AccessResult(True, Decision.ALLOWED, claim_id=claim.id)

    grant = covering_grant(
        db, company_id=company_id, vessel_call_id=vessel_call_id,
        category=category, occurred_at=occurred_at,
    )
    if grant is None:
        return AccessResult(False, Decision.CATEGORY_NEEDS_GRANT, claim_id=claim.id)

    # A grant never widens tenure: it is reached only after a claim already
    # covered this instant, so the two are intersected rather than substituted.
    return AccessResult(True, Decision.ALLOWED, claim_id=claim.id, grant_id=grant.id)


def visible_vessel_ids(
    db: Session, *, company_id: int, occurred_at: datetime,
) -> list[int]:
    """Vessels this company held at one instant.

    For filtering source rows *before* GROUP BY, which is what keeps a
    fleet-wide total from being computed and then relabelled.
    """
    moment = _aware(occurred_at)
    if moment is None:
        return []
    rows = (
        db.query(VesselRoleClaim.vessel_id)
        .filter(
            VesselRoleClaim.company_id == company_id,
            VesselRoleClaim.status == "APPROVED",
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.effective_start <= moment,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > moment,
            ),
        )
        .distinct()
        .all()
    )
    return [row[0] for row in rows]
