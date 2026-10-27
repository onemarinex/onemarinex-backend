from typing import List, Optional
from fastapi import Response
from app.services.result_limits import capped_response
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, cast, false, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from datetime import datetime
from types import SimpleNamespace

from app.db.session import get_db
from app.db.models.notification import Notification
from app.db.models.notification_read import NotificationRead
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_sos import CrewSos
from app.db.models.user import User
from app.api.v1.routes_auth import get_current_user

router = APIRouter()


class NotificationCreateIn(BaseModel):
    title: str
    message: str
    port_name: Optional[str] = None
    vessel: Optional[str] = None
    vessel_id: Optional[int] = None
    audience_type: Optional[str] = None


class NotificationOut(BaseModel):
    id: int
    title: str
    message: str
    port_name: Optional[str] = None
    vessel: Optional[str] = None
    audience_type: Optional[str] = None
    target_vessel_ids: List[int] = Field(default_factory=list)
    target_vessels: List[str] = Field(default_factory=list)
    sos_id: Optional[int] = None
    created_by: Optional[int] = None
    created_at: datetime

    class Config:
        from_attributes = True


class NotificationUpdateIn(BaseModel):
    title: Optional[str] = None
    message: Optional[str] = None
    port_name: Optional[str] = None
    vessel: Optional[str] = None
    vessel_id: Optional[int] = None
    audience_type: Optional[str] = None


class NotificationCrewOut(NotificationOut):
    is_read: bool
    sos_status: Optional[str] = None


def _agent_vessel(db: Session, agent_user_id: int, *, vessel: Optional[str] = None,
                  vessel_id: Optional[int] = None):
    """Resolve a vessel the agent actually operates, or refuse.

    Matched case-insensitively on name, and on IMO number too, so picking from
    a dropdown or typing the name both work. Returns the canonical stored name
    so the crew feed — which matches `crew_profiles.vessel` on an exact string —
    lines up.
    """
    from app.db.models.vessel import Vessel

    wanted = (vessel or "").strip()
    owned = db.query(Vessel).filter(Vessel.agent_id == agent_user_id).all()
    for v in owned:
        if vessel_id is not None and v.id == vessel_id:
            return v
        if v.name and v.name.strip().lower() == wanted.lower():
            return v
        if v.imo_number and v.imo_number.strip().lower() == wanted.lower():
            return v

    raise HTTPException(
        status_code=403,
        detail="You can only send notifications to your own vessels.",
    )


def _resolve_agent_audience(db: Session, agent_user_id: int, audience_type: Optional[str],
                            vessel: Optional[str], vessel_id: Optional[int]):
    from app.db.models.vessel import Vessel

    audience = (audience_type or "single_vessel").strip().lower()
    if audience == "all_my_vessels":
        audience = "all_agent_vessels"
    if audience not in {"single_vessel", "all_agent_vessels"}:
        raise HTTPException(status_code=400, detail="Choose Single vessel or All my vessels.")

    if audience == "single_vessel":
        if vessel_id is None and not (vessel or "").strip():
            raise HTTPException(status_code=400, detail="Choose which vessel this notification is for.")
        target = _agent_vessel(
            db, agent_user_id, vessel=vessel, vessel_id=vessel_id,
        )
        return audience, [target.id], target.name

    owned = db.query(Vessel).filter(Vessel.agent_id == agent_user_id).order_by(Vessel.id).all()
    if not owned:
        raise HTTPException(status_code=400, detail="No assigned vessels are available.")
    return audience, [item.id for item in owned], None


def _target_names(db: Session, notification: Notification) -> List[str]:
    from app.db.models.vessel import Vessel

    ids = [int(item) for item in (notification.target_vessel_ids or []) if str(item).isdigit()]
    if ids:
        rows = db.query(Vessel.id, Vessel.name).filter(Vessel.id.in_(ids)).all()
        names = {row.id: row.name for row in rows}
        return [names[item] for item in ids if item in names]
    return [notification.vessel] if notification.vessel else []


def _serialize_notification(db: Session, notification: Notification, **extra):
    return SimpleNamespace(**{
        "id": notification.id,
        "title": notification.title,
        "message": notification.message,
        "port_name": notification.port_name,
        "vessel": notification.vessel,
        "audience_type": notification.audience_type,
        "target_vessel_ids": notification.target_vessel_ids or [],
        "target_vessels": _target_names(db, notification),
        "sos_id": notification.sos_id,
        "created_by": notification.created_by,
        "created_at": notification.created_at,
        **extra,
    })


@router.post("/", response_model=NotificationOut, status_code=status.HTTP_201_CREATED)
def create_notification(
    body: NotificationCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["superadmin", "agent"]:
        raise HTTPException(status_code=403, detail="Only superadmins or agents can create notifications")

    port_to_set = body.port_name or None
    vessel_to_set = (body.vessel or "").strip() or None
    audience_type = body.audience_type
    target_vessel_ids: List[int] = []

    if current_user.role == "agent":
        assigned_port = current_user.agent_profile.assigned_port if current_user.agent_profile else None
        if not assigned_port:
            raise HTTPException(status_code=400, detail="Agent has no assigned port configuration.")
        port_to_set = assigned_port

        audience_type, target_vessel_ids, vessel_to_set = _resolve_agent_audience(
            db, current_user.id, body.audience_type, vessel_to_set, body.vessel_id,
        )

    notification = Notification(
        title=body.title.strip(),
        message=body.message.strip(),
        port_name=port_to_set,
        vessel=vessel_to_set,
        audience_type=audience_type,
        target_vessel_ids=target_vessel_ids or None,
        created_by=current_user.id,
    )

    db.add(notification)
    db.commit()
    db.refresh(notification)
    return _serialize_notification(db, notification)


@router.get("/admin", response_model=List[NotificationOut])
def list_notifications_admin(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["superadmin", "agent"]:
        raise HTTPException(status_code=403, detail="Only superadmins or agents can view notifications")

    if current_user.role == "agent":
        # History is the agent's own outbox. Matching on port as well meant he
        # saw — and could edit — notifications raised by every other agency
        # berthed at the same port.
        rows = capped_response(
            response,
            db.query(Notification).filter(
                Notification.created_by == current_user.id
            ).order_by(Notification.created_at.desc()),
            limit=limit,
        )
        return [_serialize_notification(db, row) for row in rows]

    # A Super Admin asking for this gets every notification the platform has
    # ever sent. Newest first, so the ceiling drops the oldest.
    rows = capped_response(
        response,
        db.query(Notification).order_by(Notification.created_at.desc()),
        limit=limit,
    )
    return [_serialize_notification(db, row) for row in rows]


@router.put("/{notification_id}", response_model=NotificationOut)
def update_notification(
    notification_id: int,
    body: NotificationUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["superadmin", "agent"]:
        raise HTTPException(status_code=403, detail="Only superadmins or agents can update notifications")

    notification = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    if current_user.role == "agent":
        # Own notifications only. Matching on port let any agent edit the
        # notifications of every other agency berthed at the same port.
        if notification.created_by != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to edit this notification")

    if body.title is not None:
        notification.title = body.title.strip()
    if body.message is not None:
        notification.message = body.message.strip()
    if body.port_name is not None:
        if current_user.role == "agent":
            assigned_port = current_user.agent_profile.assigned_port if current_user.agent_profile else None
            notification.port_name = assigned_port
        else:
            notification.port_name = body.port_name or None
    audience_changed = bool(
        {"vessel", "vessel_id", "audience_type"} & set(body.model_fields_set)
    )
    if audience_changed:
        vessel = (body.vessel or "").strip() or None
        if current_user.role == "agent":
            audience, ids, vessel = _resolve_agent_audience(
                db,
                current_user.id,
                body.audience_type or notification.audience_type,
                vessel or notification.vessel,
                body.vessel_id,
            )
            notification.audience_type = audience
            notification.target_vessel_ids = ids
        else:
            notification.audience_type = body.audience_type
            notification.target_vessel_ids = [body.vessel_id] if body.vessel_id else None
        notification.vessel = vessel

    db.commit()
    db.refresh(notification)
    return _serialize_notification(db, notification)


@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_notification(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["superadmin", "agent"]:
        raise HTTPException(status_code=403, detail="Only superadmins or agents can delete notifications")

    notification = db.query(Notification).filter(Notification.id == notification_id).first()
    if not notification:
        raise HTTPException(status_code=404, detail="Notification not found")

    if current_user.role == "agent":
        # Own notifications only. Matching on port let any agent delete the
        # notifications of every other agency berthed at the same port.
        if notification.created_by != current_user.id:
            raise HTTPException(status_code=403, detail="Not authorized to delete this notification")

    db.delete(notification)
    db.commit()
    return None


def _recipient_context(db: Session, current_user):
    port_name = None
    vessel_name = None
    vessel_ids: set[int] = set()
    if current_user.role == "crew":
        from app.services.historical_context import (
            eligible_assignments_for_profile,
        )

        profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
        if profile:
            assignments = eligible_assignments_for_profile(db, profile)
            vessel_ids.update(
                row.vessel_call.vessel_id for row in assignments
                if row.vessel_call and row.vessel_call.vessel_id
            )
            ports = {
                row.vessel_call.port_name for row in assignments
                if row.vessel_call and row.vessel_call.port_name
            }
            names = {
                row.vessel_call.vessel_name for row in assignments
                if row.vessel_call and row.vessel_call.vessel_name
            }
            # Legacy free-string filtering is safe only when exactly one
            # assignment applies. Multi-vessel delivery uses target IDs.
            port_name = next(iter(ports)) if len(ports) == 1 else None
            vessel_name = next(iter(names)) if len(names) == 1 else None
    else:
        from app.db.models.agent_profile import AgentProfile
        profile = db.query(AgentProfile).filter(AgentProfile.user_id == current_user.id).first()
        if profile:
            port_name = profile.assigned_port
    return port_name, vessel_name, vessel_ids


def _visible_notifications_query(db: Session, current_user):
    port_name, vessel_name, vessel_ids = _recipient_context(db, current_user)
    query = db.query(Notification)
    targeted_audiences = {"single_vessel", "all_agent_vessels"}
    legacy_audience = or_(
        Notification.audience_type.is_(None),
        Notification.audience_type.notin_(targeted_audiences),
    )
    if current_user.role == "crew":
        query = query.filter(
            ~and_(
                Notification.sos_id.isnot(None),
                Notification.created_by == current_user.id,
            )
        )
        targeted_vessel = or_(*(
            cast(Notification.target_vessel_ids, JSONB).contains([vessel_id])
            for vessel_id in vessel_ids
        )) if vessel_ids else false()
        legacy_port = (
            or_(Notification.port_name.is_(None), Notification.port_name == port_name)
            if port_name
            else Notification.port_name.is_(None)
        )
        legacy_vessel = (
            or_(
                Notification.vessel.is_(None),
                func.lower(func.trim(Notification.vessel))
                == vessel_name.strip().lower(),
            )
            if vessel_name
            else Notification.vessel.is_(None)
        )
        query = query.filter(or_(
            and_(
                Notification.audience_type.in_(targeted_audiences),
                targeted_vessel,
            ),
            and_(legacy_audience, legacy_port, legacy_vessel),
        ))
    else:
        if port_name:
            query = query.filter(
                (Notification.port_name.is_(None)) | (Notification.port_name == port_name)
            )
        else:
            query = query.filter(Notification.port_name.is_(None))
        query = query.filter(legacy_audience, Notification.vessel.is_(None))

    return query


def _visible_notifications(db: Session, current_user) -> List[Notification]:
    return (
        _visible_notifications_query(db, current_user)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(500)
        .all()
    )


@router.get("/", response_model=List[NotificationCrewOut])
def list_notifications_for_crew(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["crew", "agent"]:
        raise HTTPException(status_code=403, detail="Only crew or agents can view notifications")

    notifications = _visible_notifications(db, current_user)
    ids = [n.id for n in notifications]
    sos_ids = [n.sos_id for n in notifications if n.sos_id]

    read_rows = []
    if ids:
        read_rows = db.query(NotificationRead).filter(
            NotificationRead.user_id == current_user.id,
            NotificationRead.notification_id.in_(ids),
        ).all()
    read_ids = {r.notification_id for r in read_rows}

    sos_status_map = {}
    if sos_ids:
        rows = db.query(CrewSos.id, CrewSos.status).filter(
            CrewSos.id.in_(sos_ids)
        ).all()
        sos_status_map = {row[0]: row[1] for row in rows}

    return [
        _serialize_notification(
            db, n,
            is_read=n.id in read_ids,
            sos_status=sos_status_map.get(n.sos_id),
        )
        for n in notifications
    ]


@router.get("/unread-count")
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["crew", "agent"]:
        raise HTTPException(status_code=403, detail="Only crew or agents can view notifications")

    count = (
        _visible_notifications_query(db, current_user)
        .outerjoin(
            NotificationRead,
            and_(
                NotificationRead.notification_id == Notification.id,
                NotificationRead.user_id == current_user.id,
            ),
        )
        .filter(NotificationRead.id.is_(None))
        .count()
    )
    return {"count": count}


@router.post("/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["crew", "agent"]:
        raise HTTPException(status_code=403, detail="Only crew or agents can mark notifications")

    visible = (
        _visible_notifications_query(db, current_user)
        .filter(Notification.id == notification_id)
        .first()
    )
    if visible is None:
        raise HTTPException(status_code=404, detail="Notification not found")

    existing = db.query(NotificationRead).filter(
        NotificationRead.notification_id == notification_id,
        NotificationRead.user_id == current_user.id,
    ).first()
    if existing:
        return {"status": "ok"}

    new_read = NotificationRead(
        notification_id=notification_id,
        user_id=current_user.id,
    )
    db.add(new_read)
    db.commit()
    return {"status": "ok"}
