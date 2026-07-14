from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime

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


class NotificationOut(BaseModel):
    id: int
    title: str
    message: str
    port_name: Optional[str] = None
    vessel: Optional[str] = None
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


class NotificationCrewOut(NotificationOut):
    is_read: bool
    sos_status: Optional[str] = None


@router.post("/", response_model=NotificationOut, status_code=status.HTTP_201_CREATED)
def create_notification(
    body: NotificationCreateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["superadmin", "agent"]:
        raise HTTPException(status_code=403, detail="Only superadmins or agents can create notifications")

    port_to_set = body.port_name or None
    if current_user.role == "agent":
        assigned_port = current_user.agent_profile.assigned_port if current_user.agent_profile else None
        if not assigned_port:
            raise HTTPException(status_code=400, detail="Agent has no assigned port configuration.")
        port_to_set = assigned_port

    notification = Notification(
        title=body.title.strip(),
        message=body.message.strip(),
        port_name=port_to_set,
        vessel=(body.vessel or None),
        created_by=current_user.id,
    )

    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


@router.get("/admin", response_model=List[NotificationOut])
def list_notifications_admin(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["superadmin", "agent"]:
        raise HTTPException(status_code=403, detail="Only superadmins or agents can view notifications")

    if current_user.role == "agent":
        assigned_port = current_user.agent_profile.assigned_port if current_user.agent_profile else None
        if not assigned_port:
            return []
        return db.query(Notification).filter(
            (Notification.port_name == assigned_port) | (Notification.created_by == current_user.id)
        ).order_by(Notification.created_at.desc()).all()

    return db.query(Notification).order_by(Notification.created_at.desc()).all()


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
        assigned_port = current_user.agent_profile.assigned_port if current_user.agent_profile else None
        if not assigned_port or (notification.port_name != assigned_port and notification.created_by != current_user.id):
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
    if body.vessel is not None:
        notification.vessel = body.vessel or None

    db.commit()
    db.refresh(notification)
    return notification


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
        assigned_port = current_user.agent_profile.assigned_port if current_user.agent_profile else None
        if not assigned_port or (notification.port_name != assigned_port and notification.created_by != current_user.id):
            raise HTTPException(status_code=403, detail="Not authorized to delete this notification")

    db.delete(notification)
    db.commit()
    return None


@router.get("/", response_model=List[NotificationCrewOut])
def list_notifications_for_crew(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["crew", "agent"]:
        raise HTTPException(status_code=403, detail="Only crew or agents can view notifications")

    port_name = None
    vessel_name = None

    if current_user.role == "crew":
        profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
        if profile:
            port_name = profile.current_port
            vessel_name = profile.vessel
    elif current_user.role == "agent":
        from app.db.models.agent_profile import AgentProfile
        profile = db.query(AgentProfile).filter(AgentProfile.user_id == current_user.id).first()
        if profile:
            port_name = profile.assigned_port

    query = db.query(Notification)
    if current_user.role == "crew":
        query = query.filter(
            ~and_(
                Notification.sos_id.isnot(None),
                Notification.created_by == current_user.id,
            )
        )

    # Match port/vessel: null acts like "all"
    if port_name:
        query = query.filter(
            (Notification.port_name.is_(None))
            | (Notification.port_name == port_name)
        )
    else:
        query = query.filter(Notification.port_name.is_(None))

    if current_user.role == "crew" and vessel_name:
        query = query.filter(
            (Notification.vessel.is_(None)) | (Notification.vessel == vessel_name)
        )
    else:
        query = query.filter(Notification.vessel.is_(None))

    notifications = query.order_by(Notification.created_at.desc()).all()
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
        {
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "port_name": n.port_name,
            "vessel": n.vessel,
            "sos_id": n.sos_id,
            "created_by": n.created_by,
            "created_at": n.created_at,
            "is_read": n.id in read_ids,
            "sos_status": sos_status_map.get(n.sos_id),
        }
        for n in notifications
    ]


@router.get("/unread-count")
def get_unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["crew", "agent"]:
        raise HTTPException(status_code=403, detail="Only crew or agents can view notifications")

    port_name = None
    vessel_name = None

    if current_user.role == "crew":
        profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
        if profile:
            port_name = profile.current_port
            vessel_name = profile.vessel
    elif current_user.role == "agent":
        from app.db.models.agent_profile import AgentProfile
        profile = db.query(AgentProfile).filter(AgentProfile.user_id == current_user.id).first()
        if profile:
            port_name = profile.assigned_port

    query = db.query(Notification.id)
    if current_user.role == "crew":
        query = query.filter(
            ~and_(
                Notification.sos_id.isnot(None),
                Notification.created_by == current_user.id,
            )
        )
    if port_name:
        query = query.filter(
            (Notification.port_name.is_(None))
            | (Notification.port_name == port_name)
        )
    else:
        query = query.filter(Notification.port_name.is_(None))

    if current_user.role == "crew" and vessel_name:
        query = query.filter(
            (Notification.vessel.is_(None)) | (Notification.vessel == vessel_name)
        )
    else:
        query = query.filter(Notification.vessel.is_(None))

    notification_ids = [row[0] for row in query.all()]
    if not notification_ids:
        return {"count": 0}

    read_ids = db.query(NotificationRead.notification_id).filter(
        NotificationRead.user_id == current_user.id,
        NotificationRead.notification_id.in_(notification_ids),
    ).all()
    read_set = {row[0] for row in read_ids}
    return {"count": len(notification_ids) - len(read_set)}


@router.post("/{notification_id}/read")
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in ["crew", "agent"]:
        raise HTTPException(status_code=403, detail="Only crew or agents can mark notifications")

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
