"""Independent fleet workspace and genuine conflict review, with private feeds."""

from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, cast, func, literal, or_, select, String, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_company_onboarding import admin
from app.api.v1.routes_shipping_company import current_company_member, _require_company_admin
from app.db.models.company_fleet_change import CompanyFleetChange
from app.db.models.shipping_company import CompanyAuditEvent, CompanyUserMembership, ShippingCompany, VesselRoleClaim
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.session import get_db
from app.services import company_fleet_declarations as fleet
from app.services import company_fleet_registry as registry
from app.services.company_fleet_identity import identity_key, lock_identity

router = APIRouter()
admin_router = APIRouter()
CLAIM_TARGETS = ("fleet_declaration", "vessel_role_claim")
FLEET_ACTIONS = ("aggregate_declaration_accepted", "declaration_pending_review", "submitted", "approved", "rejected", "review_started", "more_evidence_requested", "revoked", "corrected", "declaration_period_amended", "aggregate_conflict_accepted")
DESCRIPTIONS = {
    "aggregate_declaration_accepted": "Vessel role added",
    "declaration_pending_review": "Vessel role needs conflict resolution",
    "registry_roles_updated": "Company roles and dates updated",
    "registry_relationships_ended": "Vessel relationships ended",
    "registry_identity_corrected": "Vessel details corrected",
    "registry_conflict_requested": "Fleet change needs conflict resolution",
    "registry_conflict_accepted": "Fleet conflict resolved",
    "registry_conflict_rejected": "Fleet change rejected",
    "registry_conflict_evidence_requested": "More evidence requested",
    "fleet_import_completed": "Fleet upload submitted",
    "fleet_file_uploaded": "Fleet file uploaded",
    "approved": "Vessel relationship independently approved",
    "aggregate_conflict_accepted": "Vessel role accepted for aggregate data",
    "rejected": "Vessel role rejected",
    "revoked": "Vessel role withdrawn",
    "corrected": "Vessel role dates corrected",
    "review_started": "Relationship review started",
    "more_evidence_requested": "More evidence requested",
    "declaration_period_amended": "Vessel role dates amended",
    "fleet_conflict_evidence_submitted": "Company evidence submitted",
    "fleet_conflict_cancelled": "Fleet review cancelled",
    "fleet_decision_notification": "Company review notice recorded",
}
REVIEW_ACTIONS = (
    "registry_conflict_accepted", "registry_conflict_rejected", "registry_conflict_evidence_requested",
    "aggregate_conflict_accepted", "rejected", "more_evidence_requested", "fleet_change_approved",
    "fleet_change_rejected", "fleet_conflict_cancelled",
)


def review_audit(db, actor, company_id, target_id, kind, action, *, before=None, after=None,
                 reason=None, evidence_reference=None, evidence_type="fleet_review"):
    event = CompanyAuditEvent(actor_user_id=actor.id, company_id=company_id,
                              target_type="fleet_change" if kind == "CHANGE" else "vessel_role_claim",
                              target_id=target_id, action=action, before_state=before, after_state=after,
                              reason=reason, evidence_type=evidence_type if evidence_reference else None,
                              evidence_reference=evidence_reference)
    db.add(event)
    db.flush()
    return event


def review_metadata(db, company_id, kind, target_id):
    types = ("fleet_change",) if kind == "CHANGE" else CLAIM_TARGETS
    events = db.query(CompanyAuditEvent).filter(
        CompanyAuditEvent.company_id == company_id, CompanyAuditEvent.target_type.in_(types),
        CompanyAuditEvent.target_id == target_id,
    )
    latest = events.filter(CompanyAuditEvent.action.in_(REVIEW_ACTIONS)).order_by(CompanyAuditEvent.id.desc()).first()
    submissions = events.filter(CompanyAuditEvent.action == "fleet_conflict_evidence_submitted").order_by(CompanyAuditEvent.id)
    notices = events.filter(CompanyAuditEvent.action == "fleet_decision_notification").order_by(CompanyAuditEvent.id)
    def out(event):
        return {"id": event.id, "action": event.action, "actor_user_id": event.actor_user_id,
                "created_at": event.created_at, "reason": event.reason,
                "evidence_type": event.evidence_type, "evidence_reference": event.evidence_reference,
                "before_state": event.before_state, "after_state": event.after_state}
    return {"latest_review": out(latest) if latest else None,
            "evidence_submissions": [out(row) for row in submissions],
            "notifications": [out(row) for row in notices]}


def own_review_target(db, company_id, kind, target_id, *, lock=False):
    change = db.query(CompanyFleetChange).filter_by(id=target_id, company_id=company_id).first() if kind == "CHANGE" else None
    if kind == "CHANGE" and change is None:
        raise HTTPException(404, "Fleet review not found.")
    claim_id = change.claim_id if change else target_id
    claim = db.query(VesselRoleClaim).filter_by(id=claim_id, company_id=company_id).first()
    if claim is None:
        raise HTTPException(404, "Fleet review not found.")
    if lock:
        key = db.query(identity_key()).filter(VesselRoleClaim.id == claim.id).scalar()
        proposed = change.payload.get("imo_number") if change and change.action == "REGISTRY" else None
        try:
            values = {key, fleet.imo(proposed) if proposed else key}
        except ValueError:
            raise HTTPException(409, "The saved proposal has an invalid vessel identity. Cancel it and submit a corrected proposal.") from None
        for value in sorted(values):
            lock_identity(db, value)
        if change:
            change = db.query(CompanyFleetChange).filter_by(id=change.id, company_id=company_id).populate_existing().with_for_update().one()
        claim = db.query(VesselRoleClaim).filter_by(id=claim.id, company_id=company_id).populate_existing().with_for_update().one()
    return change, claim


def require_pending(change, claim):
    if change:
        pending = change.status == "REQUESTED"
    else:
        pending = claim.status in ("SUBMITTED", "UNDER_REVIEW", "MORE_EVIDENCE") and not claim.revoked_at
    if not pending:
        raise HTTPException(409, "This review is closed. Read its decision in fleet history.")


@router.get("/conflicts/{kind}/{target_id}")
def own_review(kind: Literal["CLAIM", "CHANGE"], target_id: int, db: Session = Depends(get_db),
               context=Depends(current_company_member)):
    change, claim = own_review_target(db, context[0].id, kind, target_id)
    vessel = db.get(Vessel, claim.vessel_id) if claim.vessel_id else None
    return {"kind": kind, "id": target_id, "vessel_name": claim.declared_vessel_name or (vessel.name if vessel else "Name not recorded"),
            "imo_number": claim.imo_number or (vessel.imo_number if vessel else None), "role": claim.role,
            "status": change.status if change else claim.status, "reason": change.reason if change else claim.review_reason,
            "payload": change.payload if change else registry.snapshot(claim),
            **review_metadata(db, context[0].id, kind, target_id)}


class EvidenceIn(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    evidence_reference: str = Field(min_length=10, max_length=255)
    reason: str = Field(min_length=10, max_length=4000)


@router.post("/conflicts/{kind}/{target_id}/evidence")
def submit_evidence(kind: Literal["CLAIM", "CHANGE"], target_id: int, body: EvidenceIn,
                    db: Session = Depends(get_db), context=Depends(current_company_member), actor=Depends(get_current_user)):
    _require_company_admin(context[1])
    change, claim = own_review_target(db, context[0].id, kind, target_id, lock=True)
    require_pending(change, claim)
    before = {"payload": change.payload, "status": change.status} if change else registry.snapshot(claim)
    if not change:
        claim.evidence_type = "company_document"
        claim.evidence_reference = body.evidence_reference
        claim.status = "UNDER_REVIEW"
    # The original proposal and legacy revision are immutable. Additional
    # evidence lives in the append-only review record, not in GROUP payloads.
    review_audit(db, actor, context[0].id, target_id, kind, "fleet_conflict_evidence_submitted",
                 before=before, after={"status": change.status if change else claim.status,
                                       "evidence_reference": body.evidence_reference},
                 reason=body.reason, evidence_reference=body.evidence_reference, evidence_type="company_document")
    db.commit()
    return {"status": "EVIDENCE_SUBMITTED", "kind": kind, "id": target_id}


class CancelIn(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    reason: str = Field(min_length=10, max_length=4000)


@router.post("/conflicts/{kind}/{target_id}/cancel")
def cancel_review(kind: Literal["CLAIM", "CHANGE"], target_id: int, body: CancelIn,
                  db: Session = Depends(get_db), context=Depends(current_company_member), actor=Depends(get_current_user)):
    _require_company_admin(context[1])
    change, claim = own_review_target(db, context[0].id, kind, target_id, lock=True)
    require_pending(change, claim)
    before = {"payload": change.payload, "status": change.status} if change else registry.snapshot(claim)
    if change:
        change.status = "CANCELLED"
        change.decided_by_user_id = actor.id
        change.decided_at = datetime.now(timezone.utc)
        change.decision_reason = body.reason
    else:
        claim.status = "REVOKED"
        claim.revoked_at = datetime.now(timezone.utc)
        claim.review_reason = body.reason
    review_audit(db, actor, context[0].id, target_id, kind, "fleet_conflict_cancelled", before=before,
                 after={"status": "CANCELLED"}, reason=body.reason)
    db.commit()
    return {"status": "CANCELLED", "kind": kind, "id": target_id}


@router.get("/registry-entries")
def entries(
    search: str = Query("", max_length=120), view: Literal["active", "historical", "all"] = "active",
    role: Optional[Literal["REGISTERED_OWNER", "COMMERCIAL_MANAGER", "ISM_MANAGER"]] = None,
    sort: Literal["name", "imo", "start", "end"] = "name", descending: bool = False,
    offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db), context=Depends(current_company_member),
):
    return registry.registry_page(db, context[0].id, search, view, role, sort, descending, offset, limit)


@router.patch("/registry-entries/{entry_id}")
def edit_entry(entry_id: int, body: registry.RegistryEdit, db: Session = Depends(get_db),
               context=Depends(current_company_member), actor=Depends(get_current_user)):
    _require_company_admin(context[1])
    try:
        result = registry.apply_edit(db, context[0], actor, entry_id, body)
        db.commit()
        return result
    except IntegrityError as exc:
        db.rollback()
        if getattr(exc.orig, "pgcode", None) == "23P01":
            raise HTTPException(409, "A vessel role changed during this request. Refresh and check the conflict.") from None
        raise


def _activity_query(db, company_id=None, search=""):
    q = db.query(CompanyAuditEvent).filter(or_(
        CompanyAuditEvent.target_type.in_(("fleet_declaration", "fleet_change", "fleet_import")),
        and_(CompanyAuditEvent.target_type == "vessel_role_claim", CompanyAuditEvent.action.in_(FLEET_ACTIONS)),
    ))
    if company_id is not None:
        q = q.filter(CompanyAuditEvent.company_id == company_id)
    if search.strip():
        term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        matching_claims = select(VesselRoleClaim.id).outerjoin(Vessel, Vessel.id == VesselRoleClaim.vessel_id).where(or_(
            VesselRoleClaim.declared_vessel_name.ilike(term, escape="\\"),
            VesselRoleClaim.imo_number.ilike(term, escape="\\"),
            Vessel.name.ilike(term, escape="\\"), Vessel.imo_number.ilike(term, escape="\\"),
        ))
        q = q.filter(or_(
            CompanyAuditEvent.action.ilike(term, escape="\\"),
            cast(CompanyAuditEvent.before_state, String).ilike(term, escape="\\"),
            cast(CompanyAuditEvent.after_state, String).ilike(term, escape="\\"),
            CompanyAuditEvent.actor_user_id.in_(select(User.id).where(or_(User.name.ilike(term, escape="\\"), User.email.ilike(term, escape="\\")))),
            CompanyAuditEvent.company_id.in_(select(ShippingCompany.id).where(ShippingCompany.legal_name.ilike(term, escape="\\"))),
            and_(CompanyAuditEvent.target_type.in_(CLAIM_TARGETS), CompanyAuditEvent.target_id.in_(matching_claims)),
            and_(CompanyAuditEvent.target_type == "fleet_change", CompanyAuditEvent.target_id.in_(select(CompanyFleetChange.id).where(CompanyFleetChange.claim_id.in_(matching_claims)))),
        ))
    return q


def _activity_out(db, event, include_company=False):
    change = db.get(CompanyFleetChange, event.target_id) if event.target_type == "fleet_change" else None
    claim_id = change.claim_id if change else event.target_id if event.target_type in CLAIM_TARGETS else None
    claim = db.get(VesselRoleClaim, claim_id) if claim_id else None
    vessel = db.get(Vessel, claim.vessel_id) if claim and claim.vessel_id else None
    actor = db.get(User, event.actor_user_id) if event.actor_user_id else None
    pending = "pending" in event.action or event.action in ("registry_conflict_requested", "fleet_group_change_requested", "fleet_change_requested", "more_evidence_requested", "registry_conflict_evidence_requested", "fleet_conflict_evidence_submitted")
    pending = pending or any(
        r.get("status") in ("SUBMITTED", "UNDER_REVIEW", "MORE_EVIDENCE")
        for r in (event.after_state or {}).get("claims", []) if isinstance(r, dict)
    )
    rejected = event.action in ("rejected", "fleet_change_rejected", "registry_conflict_rejected")
    # Prefer the event's immutable identity snapshot over today's master name.
    snapshots = [event.after_state, event.before_state]
    identity_snapshot = {}
    for state in snapshots:
        if not isinstance(state, dict):
            continue
        candidates = [state, *state.get("claims", [])]
        identity_snapshot = next((value for value in candidates if isinstance(value, dict) and value.get("vessel_name")), {})
        if identity_snapshot:
            break
    out = {
        "id": event.id,
        "vessel_name": identity_snapshot.get("vessel_name") or (claim.declared_vessel_name or (vessel.name if vessel else "Name not recorded") if claim else "Fleet upload"),
        "imo_number": identity_snapshot.get("imo_number") or (claim.imo_number or (vessel.imo_number if vessel else None) if claim else None),
        "role": claim.role if claim else None,
        "description": DESCRIPTIONS.get(event.action, event.action.replace("_", " ").capitalize()),
        "actor_name": actor.name or actor.email if actor else "System",
        "created_at": event.created_at,
        "outcome": "Cancelled" if event.action == "fleet_conflict_cancelled" else "Rejected — Conflicting Claim" if rejected else "Pending Conflict Resolution" if pending else "Applied",
        "before_state": event.before_state,
        "after_state": event.after_state,
        "reason": event.reason,
        "decision_reason": event.reason if event.action in REVIEW_ACTIONS else None,
        "evidence_type": event.evidence_type,
        "evidence_reference": event.evidence_reference,
    }
    if include_company:
        company = db.get(ShippingCompany, event.company_id) if event.company_id else None
        out["company_name"] = company.legal_name if company else "Company not recorded"
    return out


def activity_page(db, company_id, search, offset, limit, include_company=False):
    q = _activity_query(db, company_id, search)
    return {"total": q.count(), "offset": offset, "limit": limit,
            "items": [_activity_out(db, r, include_company) for r in q.order_by(CompanyAuditEvent.id.desc()).offset(offset).limit(limit)]}


@router.get("/activity")
def activity(search: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
             db: Session = Depends(get_db), context=Depends(current_company_member)):
    return activity_page(db, context[0].id, search, offset, limit)


@admin_router.get("/fleet-activity")
def all_activity(search: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                 db: Session = Depends(get_db), actor=Depends(admin)):
    return activity_page(db, None, search, offset, limit, True)


def _claim_context(db, claim):
    company = db.get(ShippingCompany, claim.company_id)
    vessel = db.get(Vessel, claim.vessel_id) if claim.vessel_id else None
    key = db.query(identity_key()).filter(VesselRoleClaim.id == claim.id).scalar()
    q = fleet.overlapping(db, key, [claim.role], registry.moment(claim.effective_start), registry.moment(claim.effective_end)).filter(
        VesselRoleClaim.id != claim.id, VesselRoleClaim.status.in_(fleet.ACCEPTED)
    )
    return {
        "company_name": company.legal_name,
        "vessel_name": claim.declared_vessel_name or (vessel.name if vessel else "Name not recorded"),
        "imo_number": claim.imo_number or (vessel.imo_number if vessel else None),
        "role": claim.role,
        "effective_start": claim.effective_start, "effective_end": claim.effective_end,
        "competing_claims": [dict(id=other.id, company_name=db.get(ShippingCompany, other.company_id).legal_name,
                                 role=other.role, effective_start=other.effective_start, effective_end=other.effective_end,
                                 evidence_type=other.evidence_type, evidence_reference=other.evidence_reference) for other in q.order_by(VesselRoleClaim.id)],
    }


def _proposed_context(db, change, claim, item):
    if change.action != "REGISTRY":
        return item
    body = registry.RegistryEdit(**change.payload)
    key, rows = registry.load(db, change.company_id, claim.id)
    proposed = []
    if body.action == "UPDATE":
        for field, role in fleet.ROLE_FIELDS.items():
            if getattr(body, field):
                proposed.append((role, fleet.boundary(getattr(body, field + "_start_date")), fleet.boundary(getattr(body, field + "_end_date"))))
    elif body.action == "CORRECT":
        key = fleet.imo(body.imo_number or key)
        for role, group in registry.selected_periods(rows).items():
            proposed.append((role, min(registry.moment(r.effective_start) for r in group),
                             None if any(r.effective_end is None for r in group) else max(registry.moment(r.effective_end) for r in group)))
    if not proposed:
        return item
    intervals = []
    for role, start, end in proposed:
        predicate = and_(VesselRoleClaim.role == role, or_(VesselRoleClaim.effective_end.is_(None), VesselRoleClaim.effective_end > start))
        if end is not None:
            predicate = and_(predicate, VesselRoleClaim.effective_start < end)
        intervals.append(predicate)
    competitors = db.query(VesselRoleClaim).filter(
        identity_key() == key, VesselRoleClaim.company_id != change.company_id,
        VesselRoleClaim.status.in_(fleet.ACCEPTED), VesselRoleClaim.revoked_at.is_(None), or_(*intervals),
    ).order_by(VesselRoleClaim.id)
    item["competing_claims"] = [dict(id=other.id, company_name=db.get(ShippingCompany, other.company_id).legal_name,
                                     role=other.role, effective_start=other.effective_start, effective_end=other.effective_end,
                                     evidence_type=other.evidence_type, evidence_reference=other.evidence_reference) for other in competitors]
    item["proposed_periods"] = [dict(role=role, effective_start=start, effective_end=end) for role, start, end in proposed]
    item["effective_start"] = min(start for _, start, _ in proposed)
    item["effective_end"] = None if any(end is None for _, _, end in proposed) else max(end for _, _, end in proposed)
    item["imo_number"] = key
    return item


@admin_router.get("/fleet-conflicts")
def conflicts(search: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
              db: Session = Depends(get_db), actor=Depends(admin)):
    claims = select(literal("CLAIM").label("kind"), VesselRoleClaim.id.label("id"), VesselRoleClaim.created_at.label("created_at")).where(
        VesselRoleClaim.status.in_(("SUBMITTED", "UNDER_REVIEW", "MORE_EVIDENCE")), VesselRoleClaim.revoked_at.is_(None)
    )
    changes = select(literal("CHANGE").label("kind"), CompanyFleetChange.id.label("id"), CompanyFleetChange.created_at.label("created_at")).where(CompanyFleetChange.status == "REQUESTED")
    if search.strip():
        term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        matches = select(VesselRoleClaim.id).outerjoin(Vessel, Vessel.id == VesselRoleClaim.vessel_id).join(ShippingCompany, ShippingCompany.id == VesselRoleClaim.company_id).where(or_(
            VesselRoleClaim.declared_vessel_name.ilike(term, escape="\\"), VesselRoleClaim.imo_number.ilike(term, escape="\\"),
            Vessel.name.ilike(term, escape="\\"), Vessel.imo_number.ilike(term, escape="\\"), ShippingCompany.legal_name.ilike(term, escape="\\"),
        ))
        claims = claims.where(VesselRoleClaim.id.in_(matches))
        changes = changes.where(CompanyFleetChange.claim_id.in_(matches))
    combined = union_all(claims, changes).subquery()
    total = db.query(func.count()).select_from(combined).scalar()
    keys = db.query(combined).order_by(combined.c.created_at, combined.c.kind, combined.c.id).offset(offset).limit(limit)
    items = []
    for row in keys:
        change = db.get(CompanyFleetChange, row.id) if row.kind == "CHANGE" else None
        claim = db.get(VesselRoleClaim, change.claim_id if change else row.id)
        item = dict(kind=row.kind, id=row.id, created_at=row.created_at, **_claim_context(db, claim))
        item.update(status=change.status if change else claim.status,
                    action=change.action if change else "CLAIM",
                    payload=change.payload if change else registry.snapshot(claim),
                    reason=change.reason if change else claim.review_reason,
                    evidence_type=claim.evidence_type, evidence_reference=claim.evidence_reference)
        if change:
            item = _proposed_context(db, change, claim, item)
        item.update(review_metadata(db, claim.company_id, row.kind, row.id))
        item["review_kind"] = "ROLE_CONFLICT" if item["competing_claims"] else "IDENTITY_REVIEW" if "identity" in (claim.review_reason or "").lower() else "LEGACY_REVIEW" if (change and change.action != "REGISTRY") or not claim.fleet_group else "RELATIONSHIP_REVIEW"
        item["allowed_decisions"] = ["ACCEPT", "REJECT", "MORE_EVIDENCE"]
        items.append(item)
    return {"total": total, "offset": offset, "limit": limit, "items": items}


class ConflictDecision(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    decision: Literal["ACCEPT", "REJECT", "MORE_EVIDENCE"]
    reason: str = Field(min_length=10, max_length=4000)
    evidence_reference: Optional[str] = Field(default=None, min_length=10, max_length=255)


def _notify(db, actor, company_id, target_id, reason, outcome, kind, decision_event_id=None):
    from app.services import email as mail
    members = db.query(User).join(CompanyUserMembership, CompanyUserMembership.user_id == User.id).filter(
        CompanyUserMembership.company_id == company_id, CompanyUserMembership.role == "ADMIN", CompanyUserMembership.enabled.is_(True)
    ).all()
    sent = 0
    for user in members:
        if mail.send_email(user.email, "Your HeyPorts fleet review was updated",
                           f"Your fleet review is {outcome}. Sign in to Your Fleet to view the decision and any evidence request."):
            sent += 1
    fleet.audit(db, actor, company_id, target_id, "fleet_decision_notification",
                after={"recipient_count": len(members), "sent_count": sent, "decision_event_id": decision_event_id}, target_type="fleet_change" if kind == "CHANGE" else "fleet_declaration")
    db.commit()
    return {"recipients": len(members), "sent": sent}


@admin_router.post("/fleet-conflicts/{kind}/{target_id}/decision")
def decide(kind: Literal["CLAIM", "CHANGE"], target_id: int, body: ConflictDecision,
           db: Session = Depends(get_db), actor=Depends(admin)):
    if body.decision == "ACCEPT" and not (body.evidence_reference or "").strip():
        raise HTTPException(422, "Record the independent evidence used to resolve this conflict.")
    # Do not hold a claim lock while waiting for its advisory identity lock.
    change = db.get(CompanyFleetChange, target_id) if kind == "CHANGE" else None
    claim = db.get(VesselRoleClaim, change.claim_id if change else target_id) if kind == "CLAIM" or change else None
    if claim is None:
        raise HTTPException(404, "Fleet conflict not found.")
    company = db.get(ShippingCompany, claim.company_id)
    if company.status != "VERIFIED":
        raise HTTPException(409, "The company must be verified before this review can proceed.")
    key = db.query(identity_key()).filter(VesselRoleClaim.id == claim.id).scalar()
    proposed = change.payload.get("imo_number") if change and change.action == "REGISTRY" else None
    for value in sorted({key, fleet.imo(proposed) if proposed else key}):
        lock_identity(db, value)
    if change:
        change = db.query(CompanyFleetChange).filter_by(id=change.id).populate_existing().with_for_update().one()
        if change.status != "REQUESTED":
            raise HTTPException(409, "This fleet change was already decided.")
        before = {"status": change.status, "payload": change.payload,
                  "source_claim": registry.snapshot(claim)}
        if body.decision == "MORE_EVIDENCE":
            change.decision_reason = body.reason
            event = review_audit(db, actor, company.id, change.id, kind, "registry_conflict_evidence_requested",
                                 before=before, after={"status": change.status, "evidence_requested": True},
                                 reason=body.reason, evidence_reference=body.evidence_reference)
        else:
            if body.decision == "ACCEPT":
                if change.action == "REGISTRY":
                    try:
                        registry.apply_edit(db, company, actor, change.claim_id, registry.RegistryEdit(**change.payload), reviewed=True, ignored_change_id=change.id)
                    except IntegrityError as exc:
                        db.rollback()
                        if getattr(exc.orig, "pgcode", None) == "23P01":
                            raise HTTPException(409, "A competing role period is still active. Refresh the conflict before accepting this change.") from None
                        raise
                else:
                    # Preserve the legacy evidence and history workflow.
                    from app.api.v1.routes_company_fleet import decide_change, ChangeDecisionIn
                    result = decide_change(change.id, ChangeDecisionIn(approve=True, reason=body.reason, evidence_reference=body.evidence_reference), db, actor)
                    event = review_audit(db, actor, company.id, change.id, kind, "registry_conflict_accepted",
                                         before=before, after={"status": "APPROVED", "payload": change.payload},
                                         reason=body.reason, evidence_reference=body.evidence_reference)
                    result["notification"] = _notify(db, actor, company.id, target_id, body.reason, "accepted", kind, event.id)
                    return result
            change.status = "APPROVED" if body.decision == "ACCEPT" else "REJECTED"
            change.decided_by_user_id = actor.id
            change.decided_at = datetime.now(timezone.utc)
            change.decision_reason = body.reason
            event = review_audit(db, actor, company.id, change.id, kind,
                                 "registry_conflict_accepted" if body.decision == "ACCEPT" else "registry_conflict_rejected",
                                 before=before, after={"status": change.status, "payload": change.payload},
                                 reason=body.reason, evidence_reference=body.evidence_reference)
    else:
        claim = db.query(VesselRoleClaim).filter_by(id=claim.id).populate_existing().with_for_update().one()
        if claim.status not in ("SUBMITTED", "UNDER_REVIEW", "MORE_EVIDENCE") or claim.revoked_at:
            raise HTTPException(409, "This vessel role was already decided.")
        before = registry.snapshot(claim)
        if body.decision == "ACCEPT":
            try:
                fleet.resolve(db, fleet.imo(claim.imo_number or key))
            except ValueError:
                raise HTTPException(409, "Resolve the ambiguous operational identity before accepting this declaration.") from None
        try:
            with db.begin_nested():
                claim.status = {"ACCEPT": "AUTO_ACCEPTED", "REJECT": "REJECTED", "MORE_EVIDENCE": "MORE_EVIDENCE"}[body.decision]
                claim.review_reason = body.reason
                if body.decision == "ACCEPT":
                    claim.evidence_type = "fleet_conflict_resolution"
                    claim.evidence_reference = body.evidence_reference
                db.flush()
        except IntegrityError as exc:
            if getattr(exc.orig, "pgcode", None) == "23P01":
                raise HTTPException(409, "A competing role period is still active. Resolve it before accepting this declaration.") from None
            raise
        event = review_audit(db, actor, company.id, claim.id, kind,
                             {"ACCEPT": "aggregate_conflict_accepted", "REJECT": "rejected", "MORE_EVIDENCE": "more_evidence_requested"}[body.decision],
                             before=before, after=registry.snapshot(claim), reason=body.reason,
                             evidence_reference=body.evidence_reference)
    db.commit()
    notification = _notify(db, actor, company.id, target_id, body.reason, body.decision.replace("_", " ").lower(), kind, event.id)
    return {"status": "APPLIED", "decision": body.decision, "notification": notification}
