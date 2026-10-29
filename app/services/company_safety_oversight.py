"""Non-sensitive progress for explicitly escalated, independently authorized cases.

Narratives, people, coordinates, notes, evidence and arbitrary timeline text stay
behind the existing field grants. Company oversight never changes an incident.
"""
from datetime import datetime, timezone
from app.db.models.shipping_company import CompanyAuditEvent
from app.db.models.user import User
from app.services.company_access import covering_claim

STATUS = {"ACTIVE": "OPEN", "REPORTED": "OPEN", "ACKNOWLEDGED": "INVESTIGATING", "INVESTIGATING": "INVESTIGATING", "IN_PROGRESS": "INVESTIGATING", "RESOLVED": "RESOLVED", "CLOSED": "CLOSED", "CANCELLED": "CLOSED"}
EVENTS = {"created":"Report received", "reported":"Report received", "received":"Report received", "acknowledged":"Operator acknowledged report", "investigating":"Investigation started", "resolved":"Resolution recorded", "closed":"Operator closed case", "cancelled":"Operator cancelled case", "reopened":"Case reopened"}
ACTIONS = {"ACKNOWLEDGE":"Company acknowledged", "COMMENT":"Company comment", "CLARIFY":"Clarification requested", "FLAG":"Unresolved concern flagged"}

def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value

def progress(db, escalation, source, call, *, detail=False):
    def permitted(at):
        return bool(at and covering_claim(db, company_id=escalation.company_id, vessel_id=call.vessel_id, occurred_at=aware(at)))
    updated = getattr(source, "updated_at", None) or source.created_at
    current = permitted(updated)
    raw = getattr(source.status, "value", source.status)
    status = STATUS.get(str(raw).upper()) if current else None
    category = getattr(source, "category", None) if current else None
    severity = getattr(source, "severity", None) if current else None
    record = {"id":escalation.id,"source_kind":escalation.source_kind,
        "reference":getattr(source,"incident_id",None) or f"SOS-{source.id}",
        "vessel_name":call.vessel_name,"imo_number":call.imo_number,"port_name":call.port_name,
        "call_id":call.id,"started_at":call.started_at,"ended_at":call.ended_at,
        "occurred_at":getattr(source,"occurred_at",None),"reported_at":source.created_at,
        "occurrence_quality":"Occurred at" if getattr(source,"occurred_at",None) else "Reporting time only",
        "status":status,"category":category or ("Emergency assistance" if escalation.source_kind=="SOS" else None),
        "severity":severity,"urgency":escalation.urgency,"created_at":escalation.created_at,
        "updated_at":updated if current else None,"involved_crew":None,
        "responsible_party":call.agency_name or (call.agency.agency_name if call.agency else None),
        "summary":escalation.summary}
    if detail:
        from app.api.v1.routes_company_disclosure import case_items
        rows,_=case_items(db,source,"timeline")
        record["timeline"]=[{"label":EVENTS[row.event_type],"at":row.event_time} for row in rows.order_by("event_time") if row.event_type in EVENTS and permitted(row.event_time)]
        events=db.query(CompanyAuditEvent,User.name).outerjoin(User,User.id==CompanyAuditEvent.actor_user_id).filter(
            CompanyAuditEvent.company_id==escalation.company_id,CompanyAuditEvent.target_type=="company_safety_oversight",CompanyAuditEvent.target_id==escalation.id).order_by(CompanyAuditEvent.id).all()
        record["oversight"]=[{"action":ACTIONS.get(event.action,event.action),"message":(event.after_state or {}).get("message"),"actor":name or "Former member","at":event.created_at} for event,name in events]
    return record
