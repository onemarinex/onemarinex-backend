"""Exclusive selected-provider transport. Completion never confirms vessel movement."""

from typing import Optional

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import json
from fastapi import APIRouter, Depends, HTTPException, Response, Query, Header
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_agents import _agent_vessel, _movement_out
from app.db.session import get_db
from app.db.models.agency_transfer import AgencyTransfer, AgencyTransferEvent
from app.db.models.agent_profile import AgentProfile
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel_call import VesselCall
from app.db.models.driver import Driver
from app.db.models.user import User
from app.services.result_limits import capped_response
from app.services import crew_movement

router = APIRouter()
public_router = APIRouter()


def now():
    return datetime.now(timezone.utc)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def agent(db, actor):
    if actor.role != "agent":
        raise HTTPException(403, "Agent access required")
    row = db.query(AgentProfile).filter_by(user_id=actor.id).first()
    if row is None:
        raise HTTPException(403, "Agency profile required")
    return row


def provider(db, actor):
    if actor.role != "aggregator":
        raise HTTPException(403, "Provider access required")
    row = db.query(AggregatorProfile).filter_by(user_id=actor.id).first()
    if row is None or (row.status or "").lower() != "active":
        raise HTTPException(403, "Active provider profile required")
    return row


def release_driver(db, t):
    if t.driver_id:
        driver = db.query(Driver).filter_by(id=t.driver_id).with_for_update().first()
        if driver is not None and driver.status == "Busy":
            driver.status = "Available"


def selected_provider(db, provider_id, call):
    row = (
        db.query(AggregatorProfile)
        .filter_by(id=provider_id, operating_port_id=call.port_id)
        .first()
    )
    if row is None or (row.status or "").lower() != "active":
        raise HTTPException(422, "Select an active provider at this call's port")
    return row


def state(t):
    return {"status": t.status, "provider_id": t.provider_id, "driver_id": t.driver_id}


def record(db, t, actor, action, before=None, reason=None, driver=None):
    db.add(
        AgencyTransferEvent(
            transfer_id=t.id,
            actor_user_id=actor.id if actor else None,
            actor_driver_id=driver,
            action=action,
            previous_state=before,
            next_state=state(t),
            reason=reason,
        )
    )


def serialize(t):
    return {
        "id": t.id,
        "vessel_call_id": t.vessel_call_id,
        "provider_id": t.provider_id,
        "assignment_ids": t.assignment_ids,
        "movement_type": t.movement_type,
        "pickup": t.pickup,
        "destination": t.destination,
        "travel_at": t.travel_at,
        "requirements": t.requirements,
        "status": t.status,
        "driver_id": t.driver_id,
        "started_at": t.started_at,
        "completed_at": t.completed_at,
        "billing": "Agency settles shipping company and provider outside HeyPorts",
    }


def scoped(db, transfer_id, actor, *, lock=False):
    query = db.query(AgencyTransfer).filter_by(id=transfer_id)
    if actor.role == "agent":
        query = query.filter_by(agency_id=agent(db, actor).id)
    elif actor.role == "aggregator":
        query = query.filter_by(provider_id=provider(db, actor).id)
    else:
        raise HTTPException(403, "Transfer access required")
    if lock:
        query = query.with_for_update()
    row = query.first()
    if row is None:
        raise HTTPException(404, "Transfer not found")
    return row


@router.get("/providers")
def providers(
    vessel_id: int,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    vessel = _agent_vessel(db, actor, vessel_id)
    call = db.query(VesselCall).filter_by(vessel_id=vessel.id, ended_at=None).first()
    if call is None:
        raise HTTPException(409, "An active port call is required")
    return [
        {"id": p.id, "name": p.company_name}
        for p in db.query(AggregatorProfile)
        .filter(
            AggregatorProfile.operating_port_id == call.port_id,
            AggregatorProfile.status.ilike("active"),
        )
        .order_by(AggregatorProfile.id)
        .limit(500)
        .all()
    ]


@router.get("/assignments")
def assignments(
    vessel_id: int,
    response: Response,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    vessel = _agent_vessel(db, actor, vessel_id)
    query = (
        db.query(CrewAssignment)
        .join(VesselCall)
        .filter(VesselCall.vessel_id == vessel.id, VesselCall.ended_at.is_(None))
        .order_by(CrewAssignment.id.desc())
        .offset(offset)
    )
    rows = capped_response(response, query, limit=limit)
    return [_movement_out(a, reused=True) for a in rows]


class TransferIn(BaseModel):
    vessel_call_id: int = Field(gt=0)
    provider_id: int = Field(gt=0)
    assignment_ids: list[int] = Field(min_length=1, max_length=100)
    movement_type: str = Field(pattern="^(SIGN_ON|SIGN_OFF)$")
    pickup: str = Field(min_length=2, max_length=500)
    destination: str = Field(min_length=2, max_length=500)
    travel_at: datetime
    requirements: str = Field(default="", max_length=2000)
    client_request_id: Optional[str] = Field(default=None, min_length=8, max_length=64)


@router.post("", status_code=201)
def create(
    body: TransferIn,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    agency = agent(db, actor)
    if body.travel_at.tzinfo is None:
        raise HTTPException(422, "Travel time must include a timezone")
    db.query(AgentProfile).filter_by(id=agency.id).with_for_update().one()
    payload = body.model_dump(mode="json", exclude={"client_request_id"})
    payload["travel_at"] = aware(body.travel_at).astimezone(timezone.utc).isoformat()
    fingerprint = digest(json.dumps(payload, sort_keys=True))
    if body.client_request_id:
        previous = (
            db.query(AgencyTransfer)
            .filter_by(agency_id=agency.id, client_request_id=body.client_request_id)
            .first()
        )
        if previous is not None:
            if previous.request_fingerprint != fingerprint:
                raise HTTPException(
                    409, "Retry key already belongs to a different transfer request"
                )
            return serialize(previous)
    call = db.get(VesselCall, body.vessel_call_id)
    if call is None or call.agency_id != agency.id:
        raise HTTPException(404, "Port call not found")
    if call.ended_at:
        raise HTTPException(409, "Port call is closed")
    selected_provider(db, body.provider_id, call)
    ids = list(dict.fromkeys(body.assignment_ids))
    count = (
        db.query(CrewAssignment)
        .filter(CrewAssignment.id.in_(ids), CrewAssignment.vessel_call_id == call.id)
        .count()
    )
    if count != len(ids):
        raise HTTPException(422, "Every passenger must belong to this port call")
    if body.travel_at.tzinfo is None:
        raise HTTPException(422, "Travel time must include a timezone")
    if not body.pickup.strip() or not body.destination.strip():
        raise HTTPException(422, "Pickup and destination required")
    t = AgencyTransfer(
        agency_id=agency.id,
        created_by_user_id=actor.id,
        request_fingerprint=fingerprint,
        **body.model_dump(exclude={"assignment_ids"}),
        assignment_ids=ids,
    )
    db.add(t)
    db.flush()
    record(db, t, actor, "offered")
    db.commit()
    return serialize(t)


@router.get("")
def transfers(
    response: Response,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    query = db.query(AgencyTransfer)
    if actor.role == "agent":
        query = query.filter_by(agency_id=agent(db, actor).id)
    elif actor.role == "aggregator":
        query = query.filter_by(provider_id=provider(db, actor).id)
    else:
        raise HTTPException(403, "Transfer access required")
    return [
        serialize(t)
        for t in capped_response(
            response,
            query.order_by(AgencyTransfer.id.desc()).offset(offset),
            limit=limit,
        )
    ]


class ActionIn(BaseModel):
    action: str
    provider_id: Optional[int] = None
    driver_id: Optional[int] = None
    reason: str = Field(default="", max_length=1000)


@router.post("/{transfer_id}/actions")
def action(
    transfer_id: int,
    body: ActionIn,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    t = scoped(db, transfer_id, actor, lock=True)
    before = state(t)
    token = None
    if actor.role == "agent":
        if body.action not in ("cancel", "reassign"):
            raise HTTPException(403, "Agent cannot perform provider actions")
        if t.status in ("ON_TRIP", "COMPLETED", "CANCELLED"):
            raise HTTPException(
                409, "Transfer can no longer be withdrawn or reassigned"
            )
        if not body.reason.strip():
            raise HTTPException(422, "A reason is required")
        if body.action == "reassign":
            selected_provider(
                db, body.provider_id, db.get(VesselCall, t.vessel_call_id)
            )
            if body.provider_id == t.provider_id:
                raise HTTPException(422, "Select a different provider")
            t.provider_id = body.provider_id
            t.status = "OFFERED"
        else:
            t.status = "CANCELLED"
        release_driver(db, t)
        t.driver_token_hash = None
        t.driver_link_expires_at = None
        t.driver_id = None
    elif body.action in ("accept", "decline"):
        if t.status != "OFFERED":
            raise HTTPException(409, "Transfer is not awaiting a provider decision")
        t.status = "ACCEPTED" if body.action == "accept" else "DECLINED"
    elif body.action == "assign":
        if t.status not in ("ACCEPTED", "DRIVER_ASSIGNED"):
            raise HTTPException(409, "Accept the transfer before assigning a driver")
        driver = (
            db.query(Driver)
            .filter_by(id=body.driver_id, aggregator_id=t.provider_id)
            .with_for_update()
            .first()
        )
        if driver is None or (
            (driver.status or "").lower() != "available" and driver.id != t.driver_id
        ):
            raise HTTPException(422, "Select an available driver from your fleet")
        if t.driver_id != driver.id:
            release_driver(db, t)
        driver.status = "Busy"
        token = secrets.token_urlsafe(32)
        t.driver_token_hash = digest(token)
        t.driver_link_expires_at = now() + timedelta(hours=24)
        t.driver_id = driver.id
        t.status = "DRIVER_ASSIGNED"
    else:
        raise HTTPException(422, "Unknown transfer action")
    record(db, t, actor, body.action, before, body.reason)
    db.commit()
    result = serialize(t)
    if token:
        result["driver_token"] = token
    return result


@router.get("/{transfer_id}/audit")
def timeline(
    transfer_id: int,
    response: Response,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    t = scoped(db, transfer_id, actor)
    # Providers see only events during their current selection, never previous provider identities.
    if actor.role != "agent":
        raise HTTPException(403, "Only the agency can read transfer history")
    rows = capped_response(
        response,
        db.query(AgencyTransferEvent)
        .filter_by(transfer_id=t.id)
        .order_by(AgencyTransferEvent.id.desc())
        .offset(offset),
        limit=limit,
    )
    return [
        {
            "id": e.id,
            "action": e.action,
            "before": e.previous_state,
            "after": e.next_state,
            "reason": e.reason,
            "at": e.created_at,
        }
        for e in rows
    ]


def driver_transfer(db, token, *, lock=False):
    query = db.query(AgencyTransfer).filter_by(driver_token_hash=digest(token))
    if lock:
        query = query.with_for_update()
    t = query.first()
    if t is None or t.driver_id is None:
        raise HTTPException(404, "Trip link unavailable")
    if (
        t.driver_link_expires_at is None
        or aware(t.driver_link_expires_at) <= now()
        or t.status in ("CANCELLED", "DECLINED", "COMPLETED")
    ):
        raise HTTPException(410, "Trip link expired or closed")
    return t


def driver_output(t):
    return {
        "id": t.id,
        "pickup": t.pickup,
        "destination": t.destination,
        "travel_at": t.travel_at,
        "passenger_count": len(t.assignment_ids),
        "status": t.status,
    }


@public_router.get("")
def public_trip(
    response: Response,
    token: str = Header(..., alias="X-Transfer-Token", min_length=32, max_length=200),
    db: Session = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return driver_output(driver_transfer(db, token))


class DriverAction(BaseModel):
    action: str = Field(pattern="^(start|complete)$")


@public_router.post("")
def public_action(
    body: DriverAction,
    response: Response,
    token: str = Header(..., alias="X-Transfer-Token", min_length=32, max_length=200),
    db: Session = Depends(get_db),
):
    t = driver_transfer(db, token, lock=True)
    before = state(t)
    required = "DRIVER_ASSIGNED" if body.action == "start" else "ON_TRIP"
    if t.status != required:
        raise HTTPException(409, "Trip is not ready for this action")
    if body.action == "start":
        t.status = "ON_TRIP"
        t.started_at = now()
    else:
        t.status = "COMPLETED"
        t.completed_at = now()
        t.driver_token_hash = None
        release_driver(db, t)
    record(db, t, None, body.action, before, driver=t.driver_id)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return driver_output(t)


class PrepareIn(BaseModel):
    vessel_id: int = Field(gt=0)
    crew_name: str = Field(min_length=1, max_length=255)
    passport_number: str = Field(min_length=1, max_length=64)
    nationality: str = Field(min_length=2, max_length=80)


@router.post("/passengers", status_code=201)
def prepare_passenger(
    body: PrepareIn,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    vessel = _agent_vessel(db, actor, body.vessel_id)
    try:
        a = crew_movement.sign_on(
            db,
            vessel=vessel,
            crew_name=body.crew_name,
            passport_number=body.passport_number,
            nationality=body.nationality,
            actor=actor,
            confirmed=False,
        )
    except crew_movement.MovementError as e:
        raise HTTPException(422, str(e))
    db.commit()
    return _movement_out(a, reused=False)


class ConfirmIn(BaseModel):
    action: str = Field(pattern="^(SIGN_ON|SIGN_OFF)$")


@router.post("/assignments/{assignment_id}/confirm")
def confirm_movement(
    assignment_id: int,
    body: ConfirmIn,
    db: Session = Depends(get_db),
    actor: User = Depends(get_current_user),
):
    agency = agent(db, actor)
    a = (
        db.query(CrewAssignment)
        .join(VesselCall)
        .filter(
            CrewAssignment.id == assignment_id,
            VesselCall.agency_id == agency.id,
            VesselCall.ended_at.is_(None),
        )
        .with_for_update(of=CrewAssignment)
        .first()
    )
    if a is None:
        raise HTTPException(404, "Active assignment not found")
    if a.ended_at is not None:
        raise HTTPException(409, "Assignment is already closed")
    if body.action == "SIGN_ON":
        if a.signed_on_at is None:
            a.signed_on_at = now()
            crew_movement._audit_movement(db, a, actor, "SIGN_ON", a.signed_on_at)
    else:
        crew_movement.sign_off(db, assignment=a, actor=actor)
    db.commit()
    return _movement_out(a, reused=True)
