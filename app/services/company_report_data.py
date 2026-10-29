"""Presentation payloads from authorized shared sources, never raw case exports."""

import hashlib
import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import or_

from app.db.models.company_disclosure import CompanySafetyEscalation, CompanyDisclosureRequest
from app.db.models.port_geography import PortGeography
from app.db.models.shipping_company import VesselRoleClaim
from app.db.models.vessel import Vessel
from app.db.models.welfare_feedback import WelfareFeedback
from app.services.company_fleet_identity import claim_matches_vessel, identity_key
from app.services.company_roles import COMPANY_ROLES


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def scope_digest(scope):
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def safety_payload(db, company_id, body, call_ids):
    from app.api.v1.routes_company_disclosure import escalation_for, case_time, permitted_data, request_out

    cases, lineage, grant_lineage = [], [], []
    permitted = {"case.status", "case.severity", "case.resolved_at"}
    now = datetime.now(timezone.utc)
    query = db.query(CompanySafetyEscalation).filter(
        CompanySafetyEscalation.company_id == company_id,
        CompanySafetyEscalation.revoked_at.is_(None),
        CompanySafetyEscalation.vessel_call_id.in_(call_ids),
    ).order_by(CompanySafetyEscalation.id)
    for escalation in query:
        try:
            _, source, call = escalation_for(db, company_id, escalation.id)
        except HTTPException:
            continue
        if not body.start <= case_time(source) < body.end:
            continue
        # The explicit escalation releases a non-sensitive progress contract.
        # Narratives, people, evidence and arbitrary timeline text stay protected.
        from app.services.company_safety_oversight import progress, STATUS
        oversight = progress(db, escalation, source, call)
        case = {"reference": getattr(source, "incident_id", None) or f"SOS-{source.id}",
                "source_kind": escalation.source_kind, "vessel_name": call.vessel_name,
                "imo_number": call.imo_number, "port_name": call.port_name,
                "reported_at": case_time(source), "urgency": escalation.urgency,
                "status": oversight["status"], "severity": oversight["severity"], "resolved_at": None}
        grants = db.query(CompanyDisclosureRequest).filter_by(
            company_id=company_id, source_kind="CASE", source_id=escalation.id,
        ).filter(CompanyDisclosureRequest.status.in_(("APPROVED", "PARTIALLY_APPROVED")),
                 CompanyDisclosureRequest.revoked_at.is_(None),
                 CompanyDisclosureRequest.access_expires_at > now).order_by(CompanyDisclosureRequest.id)
        for grant in grants:
            if request_out(grant)["status"] not in ("APPROVED", "PARTIALLY_APPROVED"):
                continue
            fields = permitted & set((grant.approved_scope or {}).get("fields", []))
            if not fields:
                continue
            scope = dict(grant.approved_scope, fields=sorted(fields))
            scope["approved_values"] = {k: v for k, v in scope.get("approved_values", {}).items() if k in fields}
            try:
                released = permitted_data(db, grant, scope)
            except HTTPException:
                continue
            for field in fields:
                value = released.get(field)
                case[field.split(".")[1]] = STATUS.get(str(value).upper(), value) if field == "case.status" else value
            grant_lineage.append({"request_id": grant.id, "scope_sha256": scope_digest(grant.approved_scope), "fields": sorted(fields)})
        cases.append(case)
        lineage.append(escalation.id)
    statuses = [str(c["status"]).upper() for c in cases if c["status"] is not None]
    known = {"OPEN", "REPORTED", "ACKNOWLEDGED", "INVESTIGATING", "IN_PROGRESS", "ESCALATED", "PENDING", "RESOLVED", "CLOSED", "CANCELLED", "COMPLETED"}
    complete = bool(cases) and len(statuses) == len(cases) and set(statuses) <= known
    terminal = {"RESOLVED", "CLOSED", "CANCELLED", "COMPLETED"}
    return {"incident_count": sum(c["source_kind"] == "INCIDENT" for c in cases),
            "escalated_cases": len(cases),
            "unresolved_count": sum(s not in terminal for s in statuses) if complete else None,
            "resolved_count": sum(s in {"RESOLVED", "CLOSED", "COMPLETED"} for s in statuses) if complete else None,
            "average_resolution_hours": None, "cases": cases,
            "coverage": "Only cases explicitly escalated to this company are counted. Non-sensitive progress is released by the explicit escalation. Narratives, people and evidence still require field approval. Missing progress is unavailable, not zero. Welfare feedback is excluded."}, lineage, grant_lineage


def enrich(db, company, body, payload, calls):
    groups = {"vessel": {}, "port": {}}
    for call in payload["calls"]:
        for dimension in groups:
            row = groups[dimension].setdefault(call[dimension + "_id"], {
                "name": call[dimension + "_name"], "recorded_calls": 0,
                "booked_rides": 0, "started_passenger_seats": 0,
            })
            row["recorded_calls"] += 1
            row["booked_rides"] += call["metrics"]["total_booked_rides"]
            row["started_passenger_seats"] += call["metrics"]["crew_participation"]
    payload["port_comparison"] = sorted(groups["port"].values(), key=lambda r: r["name"] or "")
    if body.report_type != "PORT":
        payload["comparison"] = {"dimension": "vessel", "rows": sorted(groups["vessel"].values(), key=lambda r: r["name"] or "")}
    payload["ports"] = []
    for port_id, row in groups["port"].items():
        geo = db.get(PortGeography, port_id) if port_id else None
        payload["ports"].append(dict(row, country_name=geo.country_code if geo else None))

    # Include independent declarations in fleet details without making fake
    # operational vessels. A port/country scope requires recorded call context.
    claims = db.query(VesselRoleClaim).filter(
        VesselRoleClaim.company_id == company.id,
        VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED")),
        VesselRoleClaim.revoked_at.is_(None), VesselRoleClaim.effective_start < body.end,
        or_(VesselRoleClaim.effective_end.is_(None), VesselRoleClaim.effective_end > body.start),
    )
    if body.vessel_id is not None:
        claims = claims.filter(claim_matches_vessel(body.vessel_id))
    if body.port_id is not None or body.country_code or body.call_id is not None:
        matches = [claim_matches_vessel(c.vessel_id) for c in calls if c.vessel_id is not None]
        claims = claims.filter(or_(*matches)) if matches else claims.filter(False)
    labels = {role.code: role.label for role in COMPANY_ROLES}
    payload["vessels"], payload["fleet_authorization"] = [], []
    for claim in claims.order_by(VesselRoleClaim.id):
        vessel = db.get(Vessel, claim.vessel_id) if claim.vessel_id else None
        key = db.query(identity_key()).filter(VesselRoleClaim.id == claim.id).scalar()
        start, end = max(aware(claim.effective_start), body.start), min(aware(claim.effective_end), body.end) if claim.effective_end else body.end
        payload["vessels"].append({"name": claim.declared_vessel_name or (vessel.name if vessel else "Not recorded"),
            "imo_number": claim.imo_number or (vessel.imo_number if vessel else None),
            "role_label": labels.get(claim.role, claim.role.replace("_", " ").title()),
            "period_label": f"{start:%d %b %Y %H:%M UTC} to {end:%d %b %Y %H:%M UTC} (end excluded)"})
        payload["fleet_authorization"].append({"claim_id": claim.id, "identity": key,
            "start": start.isoformat(), "end": end.isoformat()})

    payload["safety"], payload["safety_authorization"], payload["disclosure_authorization"] = safety_payload(db, company.id, body, [c.id for c in calls])
    # Chart coverage uses the saved authorized call intervals. Calls spanning
    # months can appear in each month; this is not a count of new arrivals.
    cursor = datetime(body.start.year, body.start.month, 1, tzinfo=timezone.utc)
    trend = []
    while cursor < body.end:
        next_month = datetime(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1, tzinfo=timezone.utc)
        count = sum(any(max(aware(interval["start"]), cursor, body.start) < min(aware(interval["end"]), next_month, body.end)
                        for interval in source["intervals"]) for source in payload["source_authorization"][:len(calls)])
        trend.append({"label": cursor.strftime("%b %Y"), "value": count})
        cursor = next_month
    payload["charts"] = [{"section": section, "kind": "line", "title": "Recorded call coverage by month",
        "unit": "Calls", "description": "Calls that intersect each month and an accepted role period. A call can appear in more than one month.", "items": trend}
        for section in ("executive_summary", "fleet_operations")]
    # The same reducers drive welfare pages and report comparisons. No raw
    # feedback or person identifiers are embedded in report snapshots.
    from app.api.v1.routes_welfare import selected_rows, statistics
    from app.services.welfare_analytics import operational
    if set(payload["configuration"]["sections"]) & {"port_comparison","port_details"}:
        for port_id, row in groups["port"].items():
            op = operational(db,body.start,body.end,company.id,port_id,body.vessel_id,body.country_code,call_id=body.call_id)
            surveys = selected_rows(db,body.start,body.end,company.id,port_id,body.vessel_id,body.country_code)
            if body.call_id is not None:
                surveys = surveys.filter(WelfareFeedback.vessel_call_id==body.call_id)
            survey = statistics(list(surveys.yield_per(500)),True)
            row.update(access_percent=None if (survey.get("unable_to_go_ashore") or {}).get("percent") is None else 100-(survey.get("unable_to_go_ashore") or {}).get("percent"),
                satisfaction_percent=(survey.get("satisfaction") or {}).get("percent"), obstacle_percent=(survey.get("obstacles") or {}).get("percent"),
                respondents=survey["respondents"], average_time_ashore_minutes=op.get("average_journey_time_ashore_minutes"),
                facilities_visited=op["facilities_visited"], calls_with_shore_leave=op["calls_with_shore_leave"])
        payload["port_comparison"] = sorted(groups["port"].values(),key=lambda row:row["name"] or "")
        if body.report_type == "PORT":
            payload["comparison"] = {"dimension":"port","rows":payload["port_comparison"]}
    if set(payload["configuration"]["sections"]) & {"shore_leave","crew_experience","obstacles"}:
        cursor = body.start
        monthly=[]
        while cursor<body.end:
            boundary=datetime(cursor.year+(cursor.month==12),cursor.month%12+1,1,tzinfo=timezone.utc)
            stop=min(boundary,body.end)
            op=operational(db,cursor,stop,company.id,body.port_id,body.vessel_id,body.country_code,call_id=body.call_id)
            surveys=selected_rows(db,cursor,stop,company.id,body.port_id,body.vessel_id,body.country_code)
            if body.call_id is not None:
                surveys=surveys.filter(WelfareFeedback.vessel_call_id==body.call_id)
            survey=statistics(list(surveys.yield_per(500)),True)
            monthly.append({"label":cursor.strftime("%b %Y"),"bookings":op["crew_participation"],"satisfaction":(survey.get("satisfaction") or {}).get("percent"),"obstacles":(survey.get("obstacles") or {}).get("percent")})
            cursor=stop
        for section,title,field,unit in [("shore_leave","Crew booking trend","bookings",""),("crew_experience","Satisfaction trend","satisfaction","%"),("obstacles","Reported obstacle trend","obstacles","%")]:
            payload["charts"].append({"section":section,"kind":"line","title":title,"unit":unit,"items":[{"label":row["label"],"value":row[field]} for row in monthly]})
    payload["findings"] = [f"The selected scope contains {len(calls)} recorded port calls across {len(groups['vessel'])} vessels and {len(groups['port'])} ports.",
        "Crew bookings count passenger seats on started rides. Repeat passengers can count again."]


def revalidate(db, company_id, payload):
    from app.api.v1.routes_company_disclosure import escalation_for, permitted_data, request_out

    originals = payload.get("fleet_authorization", [])
    current_claims = {claim.id: (claim, key) for claim, key in db.query(VesselRoleClaim, identity_key())
                      .filter(VesselRoleClaim.company_id == company_id,
                              VesselRoleClaim.id.in_([o["claim_id"] for o in originals]))}
    for original in originals:
        claim, key = current_claims.get(original["claim_id"], (None, None))
        if claim is None or claim.status not in ("APPROVED", "AUTO_ACCEPTED") or claim.revoked_at:
            raise HTTPException(403, "Fleet report scope changed. Generate a new report.")
        if aware(claim.effective_start) > datetime.fromisoformat(original["start"]) or (
            claim.effective_end and aware(claim.effective_end) < datetime.fromisoformat(original["end"])
        ) or key != original["identity"]:
            raise HTTPException(403, "Fleet report scope changed. Generate a new report.")
    for escalation_id in payload.get("safety_authorization", []):
        escalation_for(db, company_id, escalation_id)
    for original in payload.get("disclosure_authorization", []):
        grant = db.query(CompanyDisclosureRequest).filter_by(id=original["request_id"], company_id=company_id).first()
        if grant is None or request_out(grant)["status"] not in ("APPROVED", "PARTIALLY_APPROVED") or scope_digest(grant.approved_scope) != original["scope_sha256"]:
            raise HTTPException(403, "Approved report information expired or changed. Generate a new report.")
        scope = dict(grant.approved_scope, fields=original["fields"])
        scope["approved_values"] = {k: v for k, v in scope.get("approved_values", {}).items() if k in original["fields"]}
        permitted_data(db, grant, scope)
