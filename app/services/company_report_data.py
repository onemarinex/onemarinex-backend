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


def publication_for_report(db, company_id, body):
    """Call filters must not split a published month/port/vessel cohort.

    The dashboard publishes complete canonical cells. Two calls of one vessel
    at the same port/month are not separate publication cells. Releasing one
    of them could expose a small complement through subtraction.
    """
    from app.api.v1.routes_welfare import selected_rows, survey_publication_ids
    publication = survey_publication_ids(db, company_id)
    if body.call_id is None:
        return publication
    cache = db.info.setdefault("company_report_publication", {})
    key = (company_id, body.start, body.end, body.port_id, body.vessel_id, body.country_code, body.call_id)
    if key not in cache:
        rows = list(selected_rows(db, body.start, body.end, company_id, body.port_id,
                                  body.vessel_id, body.country_code).yield_per(500))
        cells = {}
        def cell(row):
            return (aware(row.experience_at).strftime("%Y-%m"), row.port_id, row.vessel_id)
        for row in rows:
            cells.setdefault(cell(row), set()).add(row.vessel_call_id)
        whole_cells = {row.id for row in rows if cells[cell(row)] == {body.call_id}}
        cache[key] = {name: ids & whole_cells for name, ids in publication.items()}
    return cache[key]


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
                "name": "Port not recorded" if dimension == "port" and call["port_id"] is None else call[dimension + "_name"],
                "identity_recorded": call[dimension + "_id"] is not None, "recorded_calls": 0,
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
    publication_ids = publication_for_report(db, company.id, body)
    from app.services.welfare_analytics import operational
    if set(payload["configuration"]["sections"]) & {"port_comparison","port_details"}:
        for port_id, row in groups["port"].items():
            if port_id is None:
                row.update(access_percent=None, satisfaction_percent=None, obstacle_percent=None, respondents=None, average_time_ashore_minutes=None, facilities_visited=None, calls_with_shore_leave=None, coverage="Port identity not recorded")
                continue
            op = operational(db,body.start,body.end,company.id,port_id,body.vessel_id,body.country_code,call_id=body.call_id)
            surveys = selected_rows(db,body.start,body.end,company.id,port_id,body.vessel_id,body.country_code)
            if body.call_id is not None:
                surveys = surveys.filter(WelfareFeedback.vessel_call_id==body.call_id)
            survey = statistics(list(surveys.yield_per(500)), True, publication_ids)
            row.update(access_percent=None if (survey.get("unable_to_go_ashore") or {}).get("percent") is None else 100-(survey.get("unable_to_go_ashore") or {}).get("percent"),
                satisfaction_percent=(survey.get("satisfaction") or {}).get("percent"), obstacle_percent=(survey.get("obstacles") or {}).get("percent"),
                access_status=(survey.get("unable_to_go_ashore") or {}).get("status", survey["status"]),
                satisfaction_status=(survey.get("satisfaction") or {}).get("status", survey["status"]),
                obstacle_status=(survey.get("obstacles") or {}).get("status", survey["status"]),
                respondent_status=survey["status"],
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
            survey=statistics(list(surveys.yield_per(500)), True, publication_ids)
            monthly.append({"label":cursor.strftime("%b %Y"),"bookings":op["crew_participation"],"satisfaction":(survey.get("satisfaction") or {}).get("percent"),"obstacles":(survey.get("obstacles") or {}).get("percent"),
                            "satisfaction_status":(survey.get("satisfaction") or {}).get("status",survey["status"]),
                            "obstacles_status":(survey.get("obstacles") or {}).get("status",survey["status"])})
            cursor=stop
        for section,title,field,unit in [("shore_leave","Crew booking trend","bookings",""),("crew_experience","Satisfaction trend","satisfaction","%"),("obstacles","Reported obstacle trend","obstacles","%")]:
            payload["charts"].append({"section":section,"kind":"line","title":title,"unit":unit,"items":[{"label":row["label"],"value":row[field],"status":row.get(field+"_status"),"suppressed":row.get(field+"_status")=="insufficient_sample"} for row in monthly]})
    payload["findings"] = []
    unknown = payload["summary"].get("calls_without_port", 0)
    if unknown:
        payload["findings"].append(f"Port identity is not recorded for {unknown} calls. These calls are listed separately from named ports.")
    if payload.get("survey", {}).get("status") == "no_responses":
        payload["findings"].append("No crew survey responses are recorded for the survey coverage period.")
    if any(welfare_projection(payload).values()):
        scoped = selected_rows(db, body.start, body.end, company.id, body.port_id, body.vessel_id, body.country_code)
        if body.call_id is not None:
            scoped = scoped.filter(WelfareFeedback.vessel_call_id == body.call_id)
        source_ids = {row[0] for row in scoped.with_entities(WelfareFeedback.id)}
        # Only a digest leaves the server: never expose source or person IDs.
        # Counts alone cannot prove that the same people remain publishable.
        support = {key: sorted(ids & source_ids) for key, ids in publication_ids.items()}
        payload["survey_publication_fingerprint"] = scope_digest({"policy": 1, "support": support})


def revalidate(db, company_id, payload):
    revalidate_welfare(db, company_id, payload)
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


def welfare_projection(payload):
    """Publication-bearing values only; exclude operational and presentation changes."""
    fields = ("access_percent", "satisfaction_percent", "obstacle_percent", "respondents",
              "access_status", "satisfaction_status", "obstacle_status", "respondent_status")
    ports = sorted([(row.get("name"), {key: row.get(key) for key in fields})
                    for row in payload.get("port_comparison", []) if any(key in row for key in fields)], key=lambda r: str(r[0]))
    survey = dict(payload.get("survey") or {})
    for key in ("coverage", "coverage_note", "period", "period_note", "field_map"):
        survey.pop(key, None)
    comparison = payload.get("comparison") or {}
    comparison_rows = comparison.get("rows", []) if comparison.get("dimension") == "port" else []
    port_export = sorted([(row.get("name"), {key: row.get(key) for key in fields})
                          for row in comparison_rows if any(key in row for key in fields)], key=lambda r: str(r[0]))
    return {"survey": survey, "ports": ports, "port_export": port_export,
            "trends": [{"section": chart.get("section"), "items": chart.get("items")}
                       for chart in payload.get("charts", []) if chart.get("section") in ("crew_experience", "obstacles")]}


def revalidate_welfare(db, company_id, payload):
    """Never rewrite history. Refuse stale releases at all saved-report sinks.

    Recompute the same scope under today's global publication policy. If its
    welfare release changes, require a new report; an old snapshot cannot be
    combined with the current dashboard to recover a protected complement.
    Reports without survey-bearing data do not need this extra query.
    """
    original = welfare_projection(payload)
    if not any(original.values()):
        return
    from app.api.v1.routes_company_reports import prepare, ReportIn
    from app.db.models.shipping_company import ShippingCompany
    if not payload.get("filters") or not payload.get("survey_publication_fingerprint"):
        raise HTTPException(403, "Regenerate this report under current survey privacy rules.")
    company = db.get(ShippingCompany, company_id)
    body = ReportIn.model_validate(payload["filters"])
    current = prepare(db, company, body)
    if payload["survey_publication_fingerprint"] != current.get("survey_publication_fingerprint") or original != welfare_projection(current):
        raise HTTPException(403, "Survey publication coverage changed. Generate a new report.")
