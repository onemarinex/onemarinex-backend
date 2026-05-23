from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.db.models.crew_sos import CrewSos
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter()


class SosStatusUpdateIn(BaseModel):
    status: str


class SosStatusOut(BaseModel):
    id: int
    status: str

    class Config:
        from_attributes = True


class SosAdminOut(BaseModel):
    id: int
    status: str
    port_name: Optional[str] = None
    vessel: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    created_at: datetime
    crew_name: Optional[str] = None
    crew_email: Optional[str] = None

    class Config:
        from_attributes = True


@router.get("/admin", response_model=List[SosAdminOut])
def list_sos_requests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can view SOS")

    sos_list = db.query(CrewSos).order_by(CrewSos.created_at.desc()).all()

    return [
        {
            "id": sos.id,
            "status": sos.status,
            "port_name": sos.port_name,
            "vessel": sos.vessel,
            "lat": sos.lat,
            "lng": sos.lng,
            "created_at": sos.created_at,
            "crew_name": sos.crew_profile.full_name if sos.crew_profile else None,
            "crew_email": sos.user.email if sos.user else None,
        }
        for sos in sos_list
    ]


@router.patch("/{sos_id}/status", response_model=SosStatusOut)
def update_sos_status(
    sos_id: int,
    body: SosStatusUpdateIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Only superadmins can update SOS")

    sos = db.query(CrewSos).filter(CrewSos.id == sos_id).first()
    if not sos:
        raise HTTPException(status_code=404, detail="SOS request not found")

    status_value = body.status.strip().upper()
    if status_value not in {"ACTIVE", "ACKNOWLEDGED", "CLOSED", "CANCELLED"}:
        raise HTTPException(status_code=400, detail="Invalid SOS status")

    sos.status = status_value
    if status_value == "ACKNOWLEDGED":
        sos.acknowledged_at = datetime.utcnow()
    if status_value == "CLOSED":
        sos.closed_at = datetime.utcnow()
    if status_value == "CANCELLED":
        sos.cancelled_at = datetime.utcnow()

    try:
        db.commit()
        db.refresh(sos)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return sos
