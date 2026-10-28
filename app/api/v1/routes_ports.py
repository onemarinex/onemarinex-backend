from app.services.collection_pages import page_all
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
import json

def safe_parse_json(val, default_val):
    if isinstance(val, list) or isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            import json
            return json.loads(val)
        except Exception:
            pass
    return default_val

from pydantic import BaseModel, EmailStr, Field

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.port import Port
from app.db.models.port_rule import PortRule
from app.db.models.port_service_request import PortServiceRequest
from app.api.v1.routes_auth import get_current_user, get_current_user_optional
from app.services.port_time import (
    minutes_from_hhmm,
    port_clock_snapshot,
    validate_timezone_name,
)
from app.services.port_identity import canonical_port_key, matching_port_values

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
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(min_length=1, max_length=5000)
    icon_type: str # e.g., 'time', 'policy', 'doc', 'alert'

class PortRulesIn(BaseModel):
    rules: Optional[List[RuleItem]] = None
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    working_days: Optional[List[str]] = None
    timezone: Optional[str] = None
    advance_booking_buffer_minutes: Optional[int] = None
    contact_email: Optional[EmailStr] = None
    helpline_number: Optional[str] = None

class PortRulesOut(BaseModel):
    port_name: str
    rules: List[RuleItem]
    port_rules: List[RuleItem] = Field(default_factory=list)
    opening_time: Optional[str] = None
    closing_time: Optional[str] = None
    working_days: Optional[List[str]] = None
    timezone: str
    server_time: str
    port_date: str
    port_time: str
    port_day: str
    advance_booking_buffer_minutes: Optional[int] = 30
    contact_email: Optional[str] = None
    helpline_number: Optional[str] = None

    class Config:
        from_attributes = True

class PortOut(BaseModel):
    id: int
    name: str
    code: str

    class Config:
        from_attributes = True


def _rule_candidates(db: Session, value: str) -> list[str]:
    return matching_port_values(db.query(Port).all(), value)


def _validate_support_number(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    compact = "".join(ch for ch in cleaned if ch.isdigit())
    known_placeholders = {
        "9118004251234",
        "919876542064",
        "9198765403251",
        "919876543251",
    }
    if compact in known_placeholders or "HEYPORTS" in cleaned.upper():
        raise HTTPException(status_code=422, detail="Replace the placeholder with a verified support number")
    if len(compact) < 7 or len(compact) > 15:
        raise HTTPException(status_code=422, detail="Support number must contain 7 to 15 digits")
    return cleaned


@router.get("/quality-report")
def port_quality_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    ports = db.query(Port).all()
    rules = db.query(PortRule).all()
    groups = {}
    for port in ports:
        groups.setdefault(
            port.canonical_key or canonical_port_key(port.code or port.name), []
        ).append(port)
    missing_contacts = []
    for port in ports:
        if not port.is_active:
            continue
        candidates = matching_port_values(ports, port.code)
        rule = next((item for item in rules if item.port_name in candidates), None)
        if not rule or not rule.helpline_number or not rule.contact_email:
            missing_contacts.append({
                "port_id": port.id,
                "name": port.name,
                "missing_helpline": not bool(rule and rule.helpline_number),
                "missing_email": not bool(rule and rule.contact_email),
            })
    return {
        "missing_support_contacts": missing_contacts,
        "duplicate_port_groups": [
            [{"id": p.id, "name": p.name, "code": p.code} for p in group]
            for group in groups.values() if len(group) > 1
        ],
    }

@router.get("/", response_model=List[PortOut])
def get_ports(db: Session = Depends(get_db)):
    """Get list of active ports"""
    ports = page_all(
        db.query(Port).filter(Port.is_active == True),
        collection="routes_ports.get_ports.0",
        label="Ports",
    )
    return ports

def _visible_rules(
    db: Session,
    viewer,
    port_rules: list,
    *,
    crew_assignment_id: Optional[int] = None,
) -> list:
    """The rules this reader should see.

    Three different answers, because two different people own rules here:

    - an **agent** gets only their agency's, because this is also what the
      editor loads. Handing them the port's rules too would mean saving swept
      the superadmin's wording into their agency and showed it back as theirs.
    - **crew** get the port's rules *and* their agency's — both genuinely apply
      to them, and neither can overwrite the other.
    - anyone else, signed in or not, gets the port's rules alone.
    """
    if viewer is not None and viewer.role == "agent":
        return _agency_rules_for(db, viewer)
    return list(port_rules) + _agency_rules_for(
        db, viewer, crew_assignment_id=crew_assignment_id
    )


def _agency_rules_for(
    db: Session, viewer, *, crew_assignment_id: Optional[int] = None
) -> list:
    """Rules authored by the agency responsible for `viewer`.

    Empty for everyone else — these belong to one agency, not to the port.
    """
    if viewer is None:
        return []

    if viewer.role == "agent":
        profile = getattr(viewer, "agent_profile", None)
        return safe_parse_json(getattr(profile, "agency_rules", None), []) if profile else []

    if viewer.role == "crew":
        from app.db.models.agent_profile import AgentProfile
        from app.db.models.crew_profile import CrewProfile
        from app.services.historical_context import (
            selected_assignment_for_profile,
        )

        crew = db.query(CrewProfile).filter(CrewProfile.user_id == viewer.id).first()
        if crew is None:
            return []
        try:
            assignment = selected_assignment_for_profile(
                db, crew, crew_assignment_id
            )
        except ValueError:
            return []
        call = assignment.vessel_call if assignment else None
        if call is None or call.agency_id is None:
            return []
        profile = db.query(AgentProfile).filter(
            AgentProfile.id == call.agency_id
        ).first()
        return safe_parse_json(profile.agency_rules, []) if profile else []

    return []


@router.get("/{port_name}/rules", response_model=PortRulesOut)
def get_port_rules(
    port_name: str,
    crew_assignment_id: Optional[int] = None,
    db: Session = Depends(get_db),
    viewer: Optional[User] = Depends(get_current_user_optional),
):
    port = (
        db.query(Port)
        .filter((Port.code == port_name) | (Port.name == port_name))
        .first()
    )
    candidates = _rule_candidates(db, port_name)
    rules = (
        db.query(PortRule)
        .filter(PortRule.port_name.in_(candidates))
        .first()
    )
    if not rules:
        canonical_port = port.code if port else port_name
        clock = port_clock_snapshot(canonical_port)
        return {
            "port_name": canonical_port,
            "rules": _visible_rules(
                db,
                viewer,
                [],
                crew_assignment_id=crew_assignment_id,
            ),
            "port_rules": [],
            "opening_time": None,
            "closing_time": None,
            "working_days": None,
            "timezone": clock["timezone"],
            "server_time": clock["server_time"].isoformat(),
            "port_date": clock["port_date"],
            "port_time": clock["port_time"],
            "port_day": clock["port_day"],
            "advance_booking_buffer_minutes": 30,
            "contact_email": None,
            "helpline_number": None,
        }
    working_days = rules.working_days
    if isinstance(working_days, str):
        try:
            parsed = json.loads(working_days)
            if isinstance(parsed, list):
                working_days = parsed
            else:
                working_days = [d.strip() for d in working_days.split(",") if d.strip()]
        except Exception:
            working_days = [d.strip() for d in working_days.split(",") if d.strip()]

    clock = port_clock_snapshot(rules.port_name, rules.timezone)
    port_rule_items = safe_parse_json(rules.rules, [])
    return {
        "port_name": rules.port_name,
        "rules": _visible_rules(
            db,
            viewer,
            port_rule_items,
            crew_assignment_id=crew_assignment_id,
        ),
        # Keep the port-wide rules separate so an agent can display them
        # without loading them into the agency-owned editor and saving them
        # back as agency rules.
        "port_rules": port_rule_items,
        "opening_time": rules.opening_time,
        "closing_time": rules.closing_time,
        "working_days": working_days,
        "timezone": clock["timezone"],
        "server_time": clock["server_time"].isoformat(),
        "port_date": clock["port_date"],
        "port_time": clock["port_time"],
        "port_day": clock["port_day"],
        "advance_booking_buffer_minutes": rules.advance_booking_buffer_minutes if hasattr(rules, 'advance_booking_buffer_minutes') else 30,
        "contact_email": rules.contact_email if hasattr(rules, 'contact_email') else None,
        "helpline_number": rules.helpline_number if hasattr(rules, 'helpline_number') else None,
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

    is_agent = current_user.role == "agent"
    agent_support_number = None
    agent_rules = None
    if is_agent:
        assigned = (
            current_user.agent_profile.assigned_port
            if current_user.agent_profile else None
        )
        assigned_port = None
        if assigned:
            assigned_port = (
                db.query(Port)
                .filter((Port.code == assigned) | (Port.name == assigned))
                .first()
            )
        assigned_key = assigned_port.code if assigned_port else assigned
        if not assigned_key or canonical_port_key(canonical_port_name) != canonical_port_key(assigned_key):
            # Do not reveal whether a different port has configuration.
            raise HTTPException(status_code=404, detail="Port rules not found")

        allowed_agent_fields = {"rules", "helpline_number"}
        forbidden = set(body.model_fields_set) - allowed_agent_fields
        if forbidden:
            raise HTTPException(
                status_code=403,
                detail="Agents may update only the contact number and rules.",
            )

        # An agent's contact number is their agency's, not the port's.
        # port_rules.helpline_number is superadmin-owned and shared by every
        # agency berthed there, so one agent saving their number used to
        # replace the port helpline for all of them.
        if "helpline_number" in body.model_fields_set:
            profile = getattr(current_user, "agent_profile", None)
            if profile is None:
                raise HTTPException(status_code=403, detail="No agency profile on this account")
            profile.support_number = _validate_support_number(body.helpline_number)
            agent_support_number = profile.support_number

        # Rules the agent writes are their agency's, for the same reason. The
        # port holds one rules row shared by every agency berthed there, so an
        # agent saving guidance used to replace what all the others were showing
        # their crew.
        if "rules" in body.model_fields_set:
            profile = getattr(current_user, "agent_profile", None)
            if profile is None:
                raise HTTPException(status_code=403, detail="No agency profile on this account")
            profile.agency_rules = [
                {
                    "title": (rule.title or "").strip(),
                    "description": (rule.description or "").strip(),
                    "icon_type": (rule.icon_type or "time").strip() or "time",
                }
                for rule in (body.rules or [])
            ]
            agent_rules = profile.agency_rules

    port_rules = (
        db.query(PortRule)
        .filter(
            PortRule.port_name.in_(
                [item for item in [canonical_port_name, port_name, port.name if port else None] if item]
            )
        )
        .first()
    )

    update_data = body.model_dump(exclude_unset=True)
    if is_agent:
        # Both already went to the agency profile above; letting either through
        # here would write them onto the shared port row as well.
        update_data.pop("helpline_number", None)
        update_data.pop("rules", None)
    if "helpline_number" in update_data:
        update_data["helpline_number"] = _validate_support_number(update_data["helpline_number"])
    for field_name in ("opening_time", "closing_time"):
        configured_time = update_data.get(field_name)
        if configured_time:
            try:
                minutes_from_hhmm(configured_time)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field_name.replace('_', ' ').title()} must use HH:MM format",
                ) from exc
    if update_data.get("timezone"):
        try:
            update_data["timezone"] = validate_timezone_name(update_data["timezone"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    
    if port_rules:
        if "rules" in update_data and update_data["rules"] is not None:
            port_rules.rules = update_data["rules"]
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(port_rules, "rules")
        
        if "opening_time" in update_data:
            port_rules.opening_time = update_data["opening_time"]
        if "closing_time" in update_data:
            port_rules.closing_time = update_data["closing_time"]
        if "working_days" in update_data:
            port_rules.working_days = update_data["working_days"]
        if "timezone" in update_data:
            port_rules.timezone = update_data["timezone"]
        if "advance_booking_buffer_minutes" in update_data:
            port_rules.advance_booking_buffer_minutes = update_data["advance_booking_buffer_minutes"]
        if "contact_email" in update_data:
            port_rules.contact_email = update_data["contact_email"]
        if "helpline_number" in update_data:
            port_rules.helpline_number = update_data["helpline_number"]
    else:
        port_rules = PortRule(
            port_name=canonical_port_name,
            rules=update_data.get("rules") or [],
            opening_time=body.opening_time,
            closing_time=body.closing_time,
            working_days=body.working_days,
            timezone=update_data.get("timezone"),
            advance_booking_buffer_minutes=body.advance_booking_buffer_minutes if body.advance_booking_buffer_minutes is not None else 30,
            contact_email=update_data.get("contact_email"),
            helpline_number=update_data.get("helpline_number"),
        )
        db.add(port_rules)
    
    try:
        db.commit()
        db.refresh(port_rules)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))

    working_days = port_rules.working_days
    if isinstance(working_days, str):
        try:
            parsed = json.loads(working_days)
            if isinstance(parsed, list):
                working_days = parsed
            else:
                working_days = [d.strip() for d in working_days.split(",") if d.strip()]
        except Exception:
            working_days = [d.strip() for d in working_days.split(",") if d.strip()]

    clock = port_clock_snapshot(port_rules.port_name, port_rules.timezone)
    port_rule_items = safe_parse_json(port_rules.rules, [])
    response_rules = port_rule_items
    if is_agent:
        profile = getattr(current_user, "agent_profile", None)
        response_rules = (
            agent_rules
            if agent_rules is not None
            else safe_parse_json(getattr(profile, "agency_rules", None), [])
        )
    return {
        "port_name": port_rules.port_name,
        # An agent gets back the rules they just saved, not the port's. Echoing
        # the port row would make their own rules appear to vanish on save, the
        # way the contact number did.
        "rules": response_rules,
        "port_rules": port_rule_items,
        "opening_time": port_rules.opening_time,
        "closing_time": port_rules.closing_time,
        "working_days": working_days,
        "timezone": clock["timezone"],
        "server_time": clock["server_time"].isoformat(),
        "port_date": clock["port_date"],
        "port_time": clock["port_time"],
        "port_day": clock["port_day"],
        "advance_booking_buffer_minutes": port_rules.advance_booking_buffer_minutes,
        "contact_email": port_rules.contact_email,
        "helpline_number": (agent_support_number if is_agent
                            else port_rules.helpline_number),
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
