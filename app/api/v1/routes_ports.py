from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, EmailStr

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.port import Port
from app.db.models.port_rule import PortRule
from app.db.models.port_service_request import PortServiceRequest
from app.api.v1.routes_auth import get_current_user

router = APIRouter()

class ServiceRequestIn(BaseModel):
    email: Optional[str] = None

class ServiceRequestOut(BaseModel):
    id: int
    port_code: str
    email: Optional[str]
    request_type: str

    class Config:
        from_attributes = True


class RuleItem(BaseModel):
    title: str
    description: str
    icon_type: str # e.g., 'time', 'policy', 'doc', 'alert'

class PortRulesIn(BaseModel):
    rules: Optional[List[RuleItem]] = None
    closing_time: Optional[str] = None

class PortRulesOut(BaseModel):
    port_name: str
    rules: List[RuleItem]
    closing_time: Optional[str] = None

    class Config:
        from_attributes = True

class PortOut(BaseModel):
    id: int
    name: str
    code: str

    class Config:
        from_attributes = True

@router.get("/", response_model=List[PortOut])
def get_ports(db: Session = Depends(get_db)):
    """Get list of active ports"""
    ports = db.query(Port).filter(Port.is_active == True).all()
    return ports

@router.get("/{port_name}/rules", response_model=PortRulesOut)
def get_port_rules(port_name: str, db: Session = Depends(get_db)):
    port = (
        db.query(Port)
        .filter((Port.code == port_name) | (Port.name == port_name))
        .first()
    )
    candidates = [port.code, port.name, port_name] if port else [port_name]
    candidates = [item for item in candidates if item]
    rules = (
        db.query(PortRule)
        .filter(PortRule.port_name.in_(candidates))
        .first()
    )
    if not rules:
        # Return empty rules instead of 404 to simplify frontend
        return {"port_name": port.code if port else port_name, "rules": [], "closing_time": None}
    return {
        "port_name": rules.port_name,
        "rules": rules.rules or [],
        "closing_time": rules.closing_time,
    }

@router.post("/{port_name}/rules", response_model=PortRulesOut)
def update_port_rules(
    port_name: str,
    body: PortRulesIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role not in ["agent", "aggregator", "superadmin"]:
        raise HTTPException(status_code=403, detail="Only port operators can update port rules")

    # In a real scenario, we'd also check if the agent belongs to this port
    port = (
        db.query(Port)
        .filter((Port.code == port_name) | (Port.name == port_name))
        .first()
    )
    canonical_port_name = port.code if port else port_name

    port_rules = (
        db.query(PortRule)
        .filter(
            PortRule.port_name.in_(
                [item for item in [canonical_port_name, port_name, port.name if port else None] if item]
            )
        )
        .first()
    )

    rule_data = [item.model_dump() for item in body.rules] if body.rules is not None else None
    
    if port_rules:
        if rule_data is not None:
            port_rules.rules = rule_data
        if body.closing_time is not None:
            port_rules.closing_time = body.closing_time
    else:
        port_rules = PortRule(
            port_name=canonical_port_name,
            rules=rule_data or [],
            closing_time=body.closing_time,
        )
        db.add(port_rules)
    
    try:
        db.commit()
        db.refresh(port_rules)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "port_name": port_rules.port_name,
        "rules": port_rules.rules or [],
        "closing_time": port_rules.closing_time,
    }


@router.post("/{port_code}/service-request", response_model=ServiceRequestOut)
def request_port_service(
    port_code: str,
    body: ServiceRequestIn,
    db: Session = Depends(get_db)
):
    """
    Submit a 'Request Heyport Service' for a port that is not yet active.
    No authentication required — open to all crew.
    """
    entry = PortServiceRequest(
        port_code=port_code.lower(),
        email=body.email or None,
        request_type="service_request"
    )
    db.add(entry)
    try:
        db.commit()
        db.refresh(entry)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return entry


@router.post("/{port_code}/notify-me", response_model=ServiceRequestOut)
def notify_me_port(
    port_code: str,
    body: ServiceRequestIn,
    db: Session = Depends(get_db)
):
    """
    Subscribe to launch notification for a port.
    No authentication required — open to all crew.
    """
    if not body.email:
        raise HTTPException(status_code=422, detail="Email is required for notifications.")
    entry = PortServiceRequest(
        port_code=port_code.lower(),
        email=body.email,
        request_type="notify_me"
    )
    db.add(entry)
    try:
        db.commit()
        db.refresh(entry)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    return entry


@router.get("/{port_code}/service-request/count")
def get_service_request_count(
    port_code: str,
    db: Session = Depends(get_db)
):
    """
    Get the total number of service requests and notify-me requests for a port.
    Returns 100 + actual count as per social proof requirement.
    """
    from sqlalchemy import func
    count = db.query(func.count(PortServiceRequest.id)).filter(
        PortServiceRequest.port_code == port_code.lower()
    ).scalar()
    
    return {
        "port_code": port_code,
        "count": 100 + (count or 0)
    }


class FacilityScanIn(BaseModel):
    scanned_data: str

class FacilityScanOut(BaseModel):
    id: int
    user_id: Optional[int]
    port_code: str
    scanned_data: str
    created_at: str

    class Config:
        from_attributes = True

@router.post("/{port_code}/facility-scan", response_model=FacilityScanOut)
def record_facility_scan(
    port_code: str,
    body: FacilityScanIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Record a facility QR scan for a crew member.
    """
    from app.db.models.facility_scan import FacilityScan
    
    scan = FacilityScan(
        user_id=current_user.id,
        port_code=port_code.lower(),
        scanned_data=body.scanned_data
    )
    db.add(scan)
    try:
        db.commit()
        db.refresh(scan)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    
    # Format created_at to string for response model
    return {
        "id": scan.id,
        "user_id": scan.user_id,
        "port_code": scan.port_code,
        "scanned_data": scan.scanned_data,
        "created_at": scan.created_at.isoformat() if scan.created_at else ""
    }
