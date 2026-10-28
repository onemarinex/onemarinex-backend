"""Explicit safety escalation and exact field/item disclosure. Fail closed."""

from typing import Literal, Optional
from datetime import datetime, timezone, timedelta
import hashlib
import json
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from sqlalchemy import or_, and_, literal, func
from app.db.session import get_db
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_company_onboarding import admin
from app.api.v1.routes_shipping_company import current_company_member
from app.api.v1.routes_company_intelligence import (
    selected_call,
    booking_query,
)
from app.db.models.company_disclosure import (
    CompanySafetyEscalation as Escalation,
    CompanyDisclosureRequest as Request,
)
from app.db.models.shipping_company import ShippingCompany, CompanyAuditEvent
from app.db.models.incident import Incident, IncidentNote, IncidentTimelineEvent
from app.db.models.crew_sos import CrewSos, CrewSosNote, CrewSosTimelineEvent
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.models.vessel_call import VesselCall
from app.db.models.cab_booking import CabBooking
from app.db.models.booking_review import BookingReview
from app.services.company_aggregates import tenure_scoped
from app.services.event_origin import resolved_origin
from app.services.company_access import covering_claim
from app.services.event_origin import classify, CONFIRMED
from app.services.company_pdf import render

company_router = APIRouter()
admin_router = APIRouter()
CASE_FIELDS = {
    "case.title",
    "case.description",
    "case.category",
    "case.severity",
    "case.status",
    "case.reported_at",
    "case.resolved_at",
    "crew.name",
    "crew.rank",
    "crew.nationality",
    "crew.phone",
    "location.latitude",
    "location.longitude",
    "trip.reference",
    "trip.driver",
    "trip.vehicle",
    "vessel.name",
    "vessel.imo",
    "port.name",
    "call.eta",
    "call.etd",
    "timeline",
    "notes",
}
CALL_FIELDS = {
    "reviews.text",
    "reviews.rating",
    "reviews.type",
    "reviews.created_at",
    "rides.reference",
    "rides.pickup",
    "rides.destination",
    "rides.status",
    "rides.started_at",
    "rides.completed_at",
}


def now():
    return datetime.now(timezone.utc)


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def scalar(v):
    return (
        v.value
        if hasattr(v, "value")
        else v.isoformat() if isinstance(v, datetime) else v
    )


def audit(db, actor, company_id, target_id, action, scope=None):
    db.add(
        CompanyAuditEvent(
            company_id=company_id,
            actor_user_id=actor.id,
            target_type="company_disclosure",
            target_id=target_id,
            action=action,
            after_state=scope,
        )
    )


def case_source(db, kind, source_id):
    row = db.get(Incident if kind == "INCIDENT" else CrewSos, source_id)
    if row is None or (kind == "INCIDENT" and row.title == "Crew Feedback"):
        raise HTTPException(404, "Safety case not found.")
    return row


SAFE_LIFECYCLE_EVENTS = {
    "created",
    "acknowledged",
    "closed",
    "cancelled",
    "resolved",
    "status_changed",
    "reported",
    "received",
    "investigating",
    "reopened",
}


def case_items(db, source, group):
    incident = isinstance(source, Incident)
    model = (
        (IncidentNote if group == "notes" else IncidentTimelineEvent)
        if incident
        else (CrewSosNote if group == "notes" else CrewSosTimelineEvent)
    )
    fk = model.incident_id if incident else model.sos_id
    return db.query(model).filter(fk == source.id), model


def case_time(row):
    return aware(getattr(row, "occurred_at", None) or row.created_at)


def escalation_for(db, company_id, escalation_id):
    row = (
        db.query(Escalation).filter_by(id=escalation_id, company_id=company_id).first()
    )
    if row is None or row.revoked_at:
        raise HTTPException(404, "Escalation not found.")
    source = case_source(db, row.source_kind, row.source_id)
    call = selected_call(db, company_id, row.vessel_call_id)
    if (
        source.vessel_call_id != call.id
        or classify(source, call) not in CONFIRMED
        or not covering_claim(
            db,
            company_id=company_id,
            vessel_id=call.vessel_id,
            occurred_at=case_time(source),
        )
    ):
        raise HTTPException(404, "Escalation not found.")
    return row, source, call


class EscalateIn(BaseModel):
    model_config = {"str_strip_whitespace": True}
    source_kind: Literal["INCIDENT", "SOS"]
    source_id: int = Field(gt=0)
    company_id: int = Field(gt=0)
    summary: str = Field(min_length=10, max_length=4000)
    urgency: Literal["ROUTINE", "HIGH", "URGENT"] = "HIGH"


@admin_router.post("/safety-escalations", status_code=201)
def escalate(body: EscalateIn, db: Session = Depends(get_db), actor=Depends(admin)):
    company = db.get(ShippingCompany, body.company_id)
    if company is None or company.status != "VERIFIED":
        raise HTTPException(422, "Select a verified company.")
    source = case_source(db, body.source_kind, body.source_id)
    call = selected_call(db, company.id, source.vessel_call_id)
    if classify(source, call) not in CONFIRMED or not covering_claim(
        db,
        company_id=company.id,
        vessel_id=call.vessel_id,
        occurred_at=case_time(source),
    ):
        raise HTTPException(
            422, "Reconcile the case origin and company tenure before escalation."
        )
    row = Escalation(
        **body.model_dump(), vessel_call_id=call.id, created_by_user_id=actor.id
    )
    try:
        db.add(row)
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "This case has already been escalated to this company."
        ) from None
    audit(
        db,
        actor,
        company.id,
        row.id,
        "case_escalated",
        {"source_kind": body.source_kind, "source_id": body.source_id},
    )
    db.commit()
    return {"id": row.id, "status": "ESCALATED"}


@admin_router.delete("/safety-escalations/{escalation_id}", status_code=204)
def revoke_escalation(
    escalation_id: int, db: Session = Depends(get_db), actor=Depends(admin)
):
    row = db.query(Escalation).filter_by(id=escalation_id).with_for_update().first()
    if row is None:
        raise HTTPException(404, "Escalation not found.")
    row.revoked_at = now()
    audit(db, actor, row.company_id, row.id, "escalation_revoked")
    db.commit()


@company_router.get("/safety-escalations")
def escalations(
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, _ = context
    query = db.query(Escalation).filter_by(company_id=company.id, revoked_at=None)
    # Authorization can change after escalation. Filter before page assembly.
    items = []
    for row in query.order_by(
        Escalation.created_at.desc(), Escalation.id.desc()
    ).yield_per(100):
        try:
            e, s, c = escalation_for(db, company.id, row.id)
        except HTTPException:
            continue
        items.append(
            {
                "id": e.id,
                "source_kind": e.source_kind,
                "reference": getattr(s, "incident_id", None) or f"SOS-{s.id}",
                "vessel_name": c.vessel_name,
                "port_name": c.port_name,
                "created_at": e.created_at,
                "urgency": e.urgency,
            }
        )
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "offset": offset,
        "limit": limit,
    }


@company_router.get("/safety-escalations/{escalation_id}")
def summary(
    escalation_id: int,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, _ = context
    e, s, c = escalation_for(db, company.id, escalation_id)
    return {
        "id": e.id,
        "source_kind": e.source_kind,
        "summary": e.summary,
        "urgency": e.urgency,
        "vessel_name": c.vessel_name,
        "port_name": c.port_name,
        "call_id": c.id,
        "reported_at": s.created_at,
        "occurred_at": case_time(s),
        "started_at": c.started_at,
        "ended_at": c.ended_at,
        "request_end": c.ended_at or now() + timedelta(days=6),
        "available_fields": sorted(CASE_FIELDS),
    }


class AskIn(BaseModel):
    model_config = {"str_strip_whitespace": True}
    source_kind: Literal["CALL", "CASE"]
    source_id: int = Field(gt=0)
    fields: list[str] = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=10, max_length=4000)
    event_start: datetime
    event_end: datetime


@company_router.post("/disclosure-requests", status_code=201)
def ask(
    body: AskIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor=Depends(get_current_user),
):
    company, _ = context
    if body.source_kind == "CASE":
        _, _, call = escalation_for(db, company.id, body.source_id)
    else:
        call = selected_call(db, company.id, body.source_id)
    allowed = CASE_FIELDS if body.source_kind == "CASE" else CALL_FIELDS
    if not set(body.fields) <= allowed:
        raise HTTPException(
            422, "The requested fields are not supported for this source."
        )
    if (
        body.event_start.tzinfo is None
        or body.event_end.tzinfo is None
        or not aware(call.started_at)
        <= body.event_start
        < body.event_end
        <= aware(call.ended_at or now() + timedelta(days=7))
    ):
        raise HTTPException(422, "Select an event window inside this call.")
    # Serialize duplicate submissions on the company row.
    db.query(ShippingCompany).filter_by(id=company.id).with_for_update().one()
    if (
        db.query(Request.id)
        .filter_by(
            company_id=company.id,
            source_kind=body.source_kind,
            source_id=body.source_id,
            status="REQUESTED",
        )
        .first()
    ):
        raise HTTPException(409, "A request for this source is already pending.")
    row = Request(
        company_id=company.id,
        vessel_call_id=call.id,
        requested_by_user_id=actor.id,
        **body.model_dump(),
    )
    db.add(row)
    db.flush()
    audit(
        db, actor, company.id, row.id, "disclosure_requested", {"fields": body.fields}
    )
    db.commit()
    return {"id": row.id, "status": row.status}


def request_out(row, db=None):
    status = row.status
    if status in ("APPROVED", "PARTIALLY_APPROVED") and (
        row.revoked_at
        or not row.access_expires_at
        or aware(row.access_expires_at) <= now()
    ):
        status = "REVOKED" if row.revoked_at else "EXPIRED"
    call = db.get(VesselCall, row.vessel_call_id) if db else None
    return {
        "vessel_name": call.vessel_name if call else None,
        "port_name": call.port_name if call else None,
        "id": row.id,
        "company_id": row.company_id,
        "company_name": (
            db.get(ShippingCompany, row.company_id).legal_name if db else None
        ),
        "source_kind": row.source_kind,
        "source_id": row.source_id,
        "vessel_call_id": row.vessel_call_id,
        "fields": row.fields,
        "reason": row.reason,
        "event_start": row.event_start,
        "event_end": row.event_end,
        "status": status,
        "approved_fields": (row.approved_scope or {}).get("fields", []),
        "access_expires_at": row.access_expires_at,
        "decision_reason": row.decision_reason,
        "created_at": row.created_at,
    }


@company_router.get("/disclosure-requests")
def requests(
    status: Optional[
        Literal[
            "REQUESTED",
            "APPROVED",
            "PARTIALLY_APPROVED",
            "REJECTED",
            "EXPIRED",
            "REVOKED",
        ]
    ] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, _ = context
    q = db.query(Request).filter_by(company_id=company.id)
    if status == "EXPIRED":
        q = q.filter(
            Request.status.in_(["APPROVED", "PARTIALLY_APPROVED"]),
            Request.revoked_at.is_(None),
            Request.access_expires_at <= now(),
        )
    elif status == "REVOKED":
        q = q.filter(or_(Request.revoked_at.isnot(None), Request.status == "REVOKED"))
    elif status in ("APPROVED", "PARTIALLY_APPROVED"):
        q = q.filter(
            Request.status == status,
            Request.revoked_at.is_(None),
            Request.access_expires_at > now(),
        )
    elif status:
        q = q.filter(Request.status == status)
    return {
        "items": [
            request_out(r, db)
            for r in q.order_by(Request.id.desc()).offset(offset).limit(limit).all()
        ],
        "total": q.count(),
        "offset": offset,
        "limit": limit,
    }


@admin_router.get("/disclosure-requests")
def review_queue(
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    actor=Depends(admin),
):
    q = db.query(Request)
    return {
        "items": [
            request_out(r, db)
            for r in q.order_by(Request.id.desc()).offset(offset).limit(limit).all()
        ],
        "total": q.count(),
        "offset": offset,
        "limit": limit,
    }


class ReviewIn(BaseModel):
    model_config = {"str_strip_whitespace": True}
    action: Literal["APPROVE", "REJECT", "REVOKE"]
    fields: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(min_length=10, max_length=4000)
    event_start: Optional[datetime] = None
    event_end: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    note_ids: list[int] = Field(default_factory=list, max_length=500)
    timeline_ids: list[int] = Field(default_factory=list, max_length=500)
    review_ids: list[int] = Field(default_factory=list, max_length=500)
    redact_terms: list[str] = Field(default_factory=list, max_length=100)
    preview_sha256: Optional[str] = Field(default=None, pattern="^[0-9a-f]{64}$")


def scrub(text, terms):
    for term in terms:
        if term:
            text = text.replace(term, "[redacted]")
    return text


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def scope_for(db, row, body):
    if not set(body.fields) <= set(row.fields):
        raise HTTPException(422, "Approval cannot add fields that were not requested.")
    start = body.event_start or row.event_start
    end = body.event_end or row.event_end
    if (
        start.tzinfo is None
        or end.tzinfo is None
        or not aware(row.event_start) <= start < end <= aware(row.event_end)
    ):
        raise HTTPException(
            422, "Approval must stay inside the requested event window."
        )
    expiry = body.expires_at or now() + timedelta(days=7)
    if expiry.tzinfo is None or not now() < expiry <= now() + timedelta(days=7):
        raise HTTPException(422, "Access must expire within seven days.")
    if any(len(t) > 1000 for t in body.redact_terms):
        raise HTTPException(422, "A redaction term is too long.")
    scope = {
        "fields": sorted(set(body.fields)),
        "event_start": start.isoformat(),
        "event_end": end.isoformat(),
        "redact_terms": body.redact_terms,
        "notes": {},
        "timeline": {},
        "reviews": {},
    }
    if row.source_kind == "CASE":
        if body.review_ids:
            raise HTTPException(422, "Case requests cannot release unrelated reviews.")
        e, s, c = escalation_for(db, row.company_id, row.source_id)
        for group, ids in [("notes", body.note_ids), ("timeline", body.timeline_ids)]:
            if ids and group not in body.fields:
                raise HTTPException(
                    422, "Selected items require approval of their field group."
                )
            query, model = case_items(db, s, group)
            items = {i.id: i for i in query.filter(model.id.in_(ids))}
            if not set(ids) <= set(items):
                raise HTTPException(422, "An item does not belong to this case.")
            for item_id in ids:
                item = items[item_id]
                text = (
                    item.note
                    if group == "notes"
                    else (item.label or "") + "\n" + (item.detail or "")
                )
                scope[group][str(item_id)] = {
                    "hash": digest(text),
                    "text": scrub(text, body.redact_terms),
                }
    elif body.note_ids or body.timeline_ids:
        raise HTTPException(422, "Operational requests cannot release case notes.")
    if row.source_kind == "CALL" and body.review_ids:
        if not any(f.startswith("reviews.") for f in body.fields):
            raise HTTPException(422, "Select at least one review field.")
        reviews = {
            r.id: r
            for r in review_query(db, row, start, end).filter(
                BookingReview.id.in_(body.review_ids)
            )
        }
        if not set(body.review_ids) <= set(reviews):
            raise HTTPException(
                422, "A review is outside this call, time window or company tenure."
            )
        scope["reviews"] = {
            str(i): digest(review_signature(r)) for i, r in reviews.items()
        }
    initial = permitted_data(db, row, scope)
    scope["approved_values"] = {
        k: v
        for k, v in initial.items()
        if k
        not in (
            "timeline",
            "notes",
            "rides",
            "reviews",
            "case.status",
            "case.resolved_at",
        )
    }
    return scope, expiry


def review_query(db, row, start, end):
    call = selected_call(db, row.company_id, row.vessel_call_id)
    query = (
        db.query(BookingReview)
        .join(CabBooking, CabBooking.id == BookingReview.booking_id)
        .filter(
            CabBooking.vessel_call_id == call.id,
            CabBooking.vessel_id == call.vessel_id,
            resolved_origin(CabBooking),
            BookingReview.created_at >= start,
            BookingReview.created_at < end,
        )
    )
    return tenure_scoped(
        db,
        query,
        company_id=row.company_id,
        vessel_column=CabBooking.vessel_id,
        occurred_column=BookingReview.created_at,
    )


def review_signature(review):
    return repr(
        (
            review.review_text,
            review.rating,
            review.review_type,
            aware(review.created_at).isoformat(),
        )
    )


def permitted_data(db, row, scope):
    start = datetime.fromisoformat(scope["event_start"])
    end = datetime.fromisoformat(scope["event_end"])
    fields = set(scope["fields"])
    data = {}
    if row.source_kind == "CALL":
        call = selected_call(db, row.company_id, row.source_id)
        query = booking_query(db, row.company_id, call, CabBooking.created_at).filter(
            CabBooking.created_at >= start, CabBooking.created_at < end
        )
        names = {
            "rides.reference": "booking_id",
            "rides.pickup": "pickup_address",
            "rides.destination": "drop_address",
            "rides.status": "status",
            "rides.started_at": "trip_started_at",
            "rides.completed_at": "trip_completed_at",
        }
        if fields.intersection(names):
            data["rides"] = [
                {
                    key.split(".")[1]: (
                        scalar(getattr(b, attr))
                        if not isinstance(getattr(b, attr), datetime)
                        or (
                            start <= aware(getattr(b, attr)) < end
                            and covering_claim(
                                db,
                                company_id=row.company_id,
                                vessel_id=call.vessel_id,
                                occurred_at=getattr(b, attr),
                            )
                        )
                        else None
                    )
                    for key, attr in names.items()
                    if key in fields
                }
                for b in query.order_by(CabBooking.id).all()
            ]
        if any(f.startswith("reviews.") for f in fields):
            data["reviews"] = []
            approved_reviews = scope.get("reviews", {})
            if approved_reviews:
                for review in review_query(db, row, start, end).filter(
                    BookingReview.id.in_([int(i) for i in approved_reviews])
                ):
                    if approved_reviews[str(review.id)] != digest(
                        review_signature(review)
                    ):
                        continue
                    values = {
                        "reviews.text": review.review_text,
                        "reviews.rating": review.rating,
                        "reviews.type": review.review_type,
                        "reviews.created_at": review.created_at,
                    }
                    data["reviews"].append(
                        {
                            key.split(".")[1]: scalar(value)
                            for key, value in values.items()
                            if key in fields
                        }
                    )
    else:
        escalation, source, call = escalation_for(db, row.company_id, row.source_id)
        case_at = case_time(source)
        if not start <= case_at < end:
            raise HTTPException(403, "The case is outside the approved event window.")
        values = {
            "case.title": getattr(source, "title", "SOS alert"),
            "case.description": getattr(source, "description", None),
            "case.category": getattr(source, "category", None),
            "case.severity": getattr(source, "severity", None),
            "case.status": source.status,
            "case.reported_at": source.created_at,
            "case.resolved_at": getattr(source, "resolved_at", None)
            or getattr(source, "closed_at", None),
            "vessel.name": call.vessel_name,
            "vessel.imo": call.imo_number,
            "port.name": call.port_name,
            "call.eta": call.eta,
            "call.etd": call.etd,
            "location.latitude": getattr(source, "lat", None),
            "location.longitude": getattr(source, "lng", None),
        }
        assignment = (
            db.get(CrewAssignment, source.crew_assignment_id)
            if source.crew_assignment_id
            else None
        )
        profile = (
            db.get(CrewProfile, source.crew_profile_id)
            if source.crew_profile_id
            else None
        )
        values.update(
            {
                "crew.name": (
                    assignment.crew_name
                    if assignment
                    else profile.full_name if profile else None
                ),
                "crew.rank": (
                    assignment.rank if assignment else profile.rank if profile else None
                ),
                "crew.nationality": (
                    assignment.nationality
                    if assignment
                    else profile.nationality if profile else None
                ),
            }
        )
        user = db.get(User, profile.user_id) if profile else None
        values["crew.phone"] = user.mobile_number if user else None
        booking = (
            db.query(CabBooking).filter_by(booking_id=source.trip_id).first()
            if source.trip_id
            else None
        )
        if booking and booking.vessel_call_id == call.id:
            values.update(
                {
                    "trip.reference": booking.booking_id,
                    "trip.driver": booking.driver_name,
                    "trip.vehicle": booking.vehicle_name,
                }
            )
        data = {
            key: (
                scrub(str(scalar(value)), scope.get("redact_terms", []))
                if isinstance(scalar(value), str)
                else scalar(value)
            )
            for key, value in values.items()
            if key in fields
        }
        for group in ("notes", "timeline"):
            if group not in fields:
                continue
            items = []
            query, model = case_items(db, source, group)
            approved_ids = [int(i) for i in scope[group]]
            if group == "notes":
                query = query.filter(model.id.in_(approved_ids))
            else:
                query = query.filter(
                    or_(
                        model.id.in_(approved_ids),
                        and_(
                            model.source == "system",
                            func.lower(model.event_type).in_(SAFE_LIFECYCLE_EVENTS),
                        ),
                    )
                )
            timestamp = model.created_at if group == "notes" else model.event_time
            query = query.filter(timestamp >= start, timestamp < end)
            query = tenure_scoped(
                db,
                query,
                company_id=row.company_id,
                vessel_column=literal(call.vessel_id),
                occurred_column=timestamp,
            )
            for item in query.yield_per(500):
                at = aware(item.created_at if group == "notes" else item.event_time)
                if not start <= at < end or not covering_claim(
                    db,
                    company_id=row.company_id,
                    vessel_id=call.vessel_id,
                    occurred_at=at,
                ):
                    continue
                text = (
                    item.note
                    if group == "notes"
                    else (item.label or "") + "\n" + (item.detail or "")
                )
                approved = scope[group].get(str(item.id))
                if approved and approved["hash"] == digest(text):
                    items.append(
                        {"id": item.id, "at": at.isoformat(), "text": approved["text"]}
                    )
                elif (
                    group == "timeline"
                    and item.source == "system"
                    and item.event_type.lower() in SAFE_LIFECYCLE_EVENTS
                ):
                    items.append(
                        {
                            "id": item.id,
                            "at": at.isoformat(),
                            "text": item.event_type.replace("_", " ").capitalize(),
                        }
                    )
            data[group] = sorted(items, key=lambda i: (i["at"], i["id"]))
    data.update(scope.get("approved_values", {}))

    def redact(value):
        if isinstance(value, str):
            return scrub(value, scope.get("redact_terms", []))
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, dict):
            return {k: redact(v) for k, v in value.items()}
        return value

    return redact(data)


def preview_hash(scope, data):
    return digest(
        json.dumps(jsonable_encoder({"scope": scope, "data": data}), sort_keys=True)
    )


@admin_router.post("/disclosure-requests/{request_id}/preview")
def preview(
    request_id: int, body: ReviewIn, db: Session = Depends(get_db), actor=Depends(admin)
):
    row = db.get(Request, request_id)
    if row is None:
        raise HTTPException(404, "Request not found.")
    scope, expiry = scope_for(db, row, body)
    data = permitted_data(db, row, scope)
    return {
        "fields": scope["fields"],
        "expires_at": expiry,
        "data": data,
        "preview_sha256": preview_hash(scope, data),
    }


@admin_router.post("/disclosure-requests/{request_id}/decision")
def decide(
    request_id: int, body: ReviewIn, db: Session = Depends(get_db), actor=Depends(admin)
):
    row = db.query(Request).filter_by(id=request_id).with_for_update().first()
    if row is None:
        raise HTTPException(404, "Request not found.")
    if body.action == "REVOKE":
        if row.status not in ("APPROVED", "PARTIALLY_APPROVED"):
            raise HTTPException(409, "No active approval can be revoked.")
        row.revoked_at = now()
        row.status = "REVOKED"
    elif row.status != "REQUESTED":
        raise HTTPException(409, "This request was already decided.")
    elif body.action == "REJECT":
        row.status = "REJECTED"
    else:
        if not body.fields:
            raise HTTPException(422, "Approve at least one field.")
        scope, expiry = scope_for(db, row, body)
        data = permitted_data(db, row, scope)
        if not body.preview_sha256 or body.preview_sha256 != preview_hash(scope, data):
            raise HTTPException(
                409,
                "Preview the current company view before approval. The source or review scope has changed.",
            )
        row.approved_scope = scope
        row.access_expires_at = expiry
        row.status = (
            "APPROVED" if set(body.fields) == set(row.fields) else "PARTIALLY_APPROVED"
        )
    row.decision_reason = body.reason
    row.decided_by_user_id = actor.id
    audit(
        db,
        actor,
        row.company_id,
        row.id,
        "disclosure_" + row.status.lower(),
        {"fields": (row.approved_scope or {}).get("fields", [])},
    )
    db.commit()
    return request_out(row)


@company_router.get("/disclosure-requests/{request_id}/data")
def read(
    request_id: int,
    format: Literal["json", "pdf"] = "json",
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor=Depends(get_current_user),
):
    company, _ = context
    row = db.query(Request).filter_by(id=request_id, company_id=company.id).first()
    if row is None:
        raise HTTPException(404, "Request not found.")
    if request_out(row)["status"] not in ("APPROVED", "PARTIALLY_APPROVED"):
        raise HTTPException(
            403, "Approved access is expired, revoked or not available."
        )
    data = permitted_data(db, row, row.approved_scope)
    audit(
        db,
        actor,
        company.id,
        row.id,
        "disclosure_downloaded" if format == "pdf" else "disclosure_viewed",
        {"fields": row.approved_scope["fields"]},
    )
    db.commit()
    generated = now().isoformat()
    if format == "pdf":
        return Response(
            render(
                "Approved case information",
                company.legal_name,
                [(k, v) for k, v in data.items()],
                f"Generated {generated} · Request {row.id}",
            ),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="approved-information-{row.id}.pdf"',
                "Cache-Control": "no-store",
            },
        )
    return JSONResponse(
        jsonable_encoder(
            {
                "request_id": row.id,
                "generated_at": generated,
                "expires_at": row.access_expires_at,
                "fields": row.approved_scope["fields"],
                "data": data,
            }
        ),
        headers={"Cache-Control": "private, no-store"},
    )


@admin_router.get("/disclosure-requests/{request_id}/source")
def review_source(request_id: int, db: Session = Depends(get_db), actor=Depends(admin)):
    row = db.get(Request, request_id)
    if row is None:
        raise HTTPException(404, "Request not found.")
    result = {
        "request": request_out(row, db),
        "notes": [],
        "timeline": [],
        "reviews": [],
    }
    if row.source_kind == "CALL":
        result["reviews"] = [
            {
                "id": r.id,
                "text": r.review_text or "No comment",
                "rating": r.rating,
                "type": r.review_type,
                "at": r.created_at,
            }
            for r in review_query(db, row, row.event_start, row.event_end)
            .order_by(BookingReview.id)
            .limit(501)
        ]
        if len(result["reviews"]) > 500:
            raise HTTPException(
                422, "Narrow the event window to review at most 500 source reviews."
            )
    if row.source_kind == "CASE":
        _, source, _ = escalation_for(db, row.company_id, row.source_id)
        for group in ("notes", "timeline"):
            query, model = case_items(db, source, group)
            timestamp = model.created_at if group == "notes" else model.event_time
            query = query.filter(
                timestamp >= row.event_start, timestamp < row.event_end
            ).order_by(timestamp, model.id)
            items = query.limit(501).all()
            if len(items) > 500:
                raise HTTPException(
                    422,
                    "Narrow the requested event window to at most 500 source items per group.",
                )
            result[group] = [
                {
                    "id": i.id,
                    "text": (
                        i.note
                        if group == "notes"
                        else (i.label or "") + "\n" + (i.detail or "")
                    ),
                    "at": getattr(
                        i, "created_at" if group == "notes" else "event_time"
                    ),
                }
                for i in items
            ]
    return result
