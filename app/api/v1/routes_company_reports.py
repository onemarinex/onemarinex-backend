"""Company-scoped previews, immutable report snapshots and PDF/CSV history."""

from typing import Literal, Optional
from datetime import datetime, timezone
import base64, csv, hashlib, io, json
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.company_disclosure import CompanyManagementReport
from app.db.models.company_profile import CompanyProfile
from app.db.models.shipping_company import CompanyAuditEvent, VesselRoleClaim
from app.services.company_claims import claim_intervals_cover
from app.services.company_fleet_identity import claim_matches_vessel, operational_ids, claims_by_vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.welfare_feedback import WelfareFeedback
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_shipping_company import current_company_member
from app.api.v1.routes_company_intelligence import (
    calls_query,
    call_out,
    call_metrics,
    call_metrics_many,
    selected_call,
    PORTAL_METRIC_VERSION,
    aware,
)
from app.api.v1.routes_welfare import selected_rows, statistics, month_window
from app.services.company_metrics import DICTIONARY_VERSION
from app.services.company_aggregates import Period
from app.services.company_pdf import render_report
from app.services.company_pdf_components import period as display_period
from app.services.company_report_templates import REPORT_TYPES, SECTIONS, report_options, report_catalogue
from app.services.welfare_scope import filter_country
from app.services.welfare_analytics import operational, review_summary

router = APIRouter()
TYPES = REPORT_TYPES


class ReportIn(BaseModel):
    model_config = {"extra": "forbid"}
    report_type: Literal["EXECUTIVE", "FLEET", "PORT", "CALL", "WELFARE", "SAFETY"]
    start: datetime
    end: datetime
    vessel_id: Optional[int] = None
    port_id: Optional[int] = None
    call_id: Optional[int] = None
    country_code: Optional[str] = Field(default=None, max_length=7)
    configuration: Optional[dict] = None


def prepare(db, company, body):
    try:
        configuration = report_options(body.report_type, body.configuration)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from None
    if (
        body.start.tzinfo is None
        or body.end.tzinfo is None
        or not body.start < body.end
        or (body.end - body.start).days > 1096
    ):
        raise HTTPException(
            422, "Choose a valid time range of at most three years, with time zones."
        )
    if body.report_type == "CALL" and body.call_id is None:
        raise HTTPException(422, "Select the exact port call.")
    if body.call_id is not None and body.report_type != "CALL":
        raise HTTPException(
            422,
            "An exact call filter is available only for the Vessel / Port Call template.",
        )
    query = calls_query(db, company.id).filter(
        VesselCall.started_at < min(body.end, datetime.now(timezone.utc)),
        func.coalesce(VesselCall.ended_at, func.now()) > body.start,
    )
    period_claims = db.query(VesselRoleClaim.id).filter(
        VesselRoleClaim.company_id == company.id,
        claim_matches_vessel(VesselCall.vessel_id),
        VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED")),
        VesselRoleClaim.revoked_at.is_(None),
        VesselRoleClaim.effective_start < body.end,
        or_(
            VesselRoleClaim.effective_end.is_(None),
            VesselRoleClaim.effective_end > body.start,
        ),
    )
    query = query.filter(period_claims.exists())
    if body.vessel_id is not None:
        query = query.filter_by(vessel_id=body.vessel_id)
    if body.port_id is not None:
        query = query.filter_by(port_id=body.port_id)
    if body.call_id is not None:
        selected_call(db, company.id, body.call_id)
        query = query.filter_by(id=body.call_id)
    query = filter_country(query, VesselCall.port_id, body.country_code)
    calls = query.order_by(VesselCall.started_at, VesselCall.id).all()
    if len(calls) > 2000:
        raise HTTPException(422, "Narrow the report scope to at most 2,000 calls.")
    profile = db.get(CompanyProfile, company.id)
    payload = {
        "report_type": body.report_type,
        "data_mode": "LIVE",
        "configuration": configuration,
        "title": TYPES[body.report_type],
        "company_id": company.id,
        "company_name": company.legal_name,
        "generated_at": datetime.now(timezone.utc),
        "period": {"start": body.start, "end": body.end},
        "filters": body.model_dump(),
        "metric_version": PORTAL_METRIC_VERSION,
        "source_dictionary_version": DICTIONARY_VERSION,
        "call_ids": [c.id for c in calls],
        "branding": {
            "name": company.legal_name,
            "logo": (
                "data:image/png;base64," + base64.b64encode(profile.logo_png).decode()
                if profile and profile.logo_png
                else None
            ),
        },
        "methodology": "Accepted company role periods and exact call records. Passenger-seat activity is not a distinct-person count. Missing sources are unavailable. No un-escalated safety records are included.",
        "coverage": {"recorded_calls": len(calls), "global_coverage_claim": False},
        "calls": [],
        "source_authorization": [],
    }
    period = Period(body.start, body.end)
    metrics_by_call = call_metrics_many(db, company.id, calls, period)
    accepted_claims = claims_by_vessel(db, company.id, [c.vessel_id for c in calls])
    for call in calls:
        cutoff = min(
            aware(call.ended_at) if call.ended_at else datetime.now(timezone.utc),
            body.end,
        )
        claims = [claim for claim in accepted_claims.get(call.vessel_id, [])
                  if aware(claim.effective_start) < cutoff and (
                      claim.effective_end is None or aware(claim.effective_end) > max(aware(call.started_at), body.start))]
        intervals = [
            {
                "start": max(aware(claim.effective_start), aware(call.started_at), body.start),
                "end": (
                    min(aware(claim.effective_end), cutoff)
                    if claim.effective_end
                    else cutoff
                ),
            }
            for claim in claims
        ]
        payload["source_authorization"].append(
            {"vessel_id": call.vessel_id, "intervals": intervals}
        )
        row = call_out(db, company.id, call, metrics=False)
        row["metrics"] = metrics_by_call[call.id]
        payload["calls"].append(row)
    payload["summary"] = {
        "recorded_vessels": len({c.vessel_id for c in calls}),
        "recorded_ports": len({c.port_id for c in calls if c.port_id is not None}),
        "calls_without_port": sum(c.port_id is None for c in calls),
        "recorded_calls": len(calls),
        "booked_rides": sum(
            c["metrics"]["total_booked_rides"] for c in payload["calls"]
        ),
        "started_passenger_seats": sum(
            c["metrics"]["crew_participation"] for c in payload["calls"]
        ),
    }
    payload["executive_summary"] = (
        f"Recorded calls: {len(calls)}. Vessels: {payload['summary']['recorded_vessels']}. "
        f"Ports: {payload['summary']['recorded_ports']}. "
        "Only recorded activity inside accepted company tenure is included. Automatic acceptance is a declaration, not independent verification. Zero activity does not prove no shore leave occurred."
    )
    if body.report_type in ("FLEET", "PORT"):
        key = "vessel" if body.report_type == "FLEET" else "port"
        groups = {}
        for call in payload["calls"]:
            identity = call[key + "_id"]
            group = groups.setdefault(
                identity,
                {
                    "name": call[key + "_name"],
                    "recorded_calls": 0,
                    "booked_rides": 0,
                    "started_passenger_seats": 0,
                },
            )
            group["recorded_calls"] += 1
            group["booked_rides"] += call["metrics"]["total_booked_rides"]
            group["started_passenger_seats"] += call["metrics"]["crew_participation"]
        payload["comparison"] = {
            "dimension": key,
            "rows": sorted(groups.values(), key=lambda r: r["name"] or ""),
        }
    needs_welfare = bool(set(configuration["sections"]) & {"executive_summary", "shore_leave", "crew_experience", "obstacles", "service_ratings"})
    from app.services.company_report_data import publication_for_report

    if needs_welfare:
        # Reviews use their own recorded timestamp and can follow a closed call.
        # Retain the original authorized report-period scope, even when that
        # source call no longer intersects the reporting calendar period.
        source_calls = calls_query(db, company.id)
        if body.port_id is not None:
            source_calls = source_calls.filter(VesselCall.port_id == body.port_id)
        if body.vessel_id is not None:
            source_calls = source_calls.filter(VesselCall.vessel_id == body.vessel_id)
        if body.call_id is not None:
            source_calls = source_calls.filter(VesselCall.id == body.call_id)
        source_calls = filter_country(source_calls, VesselCall.port_id, body.country_code)
        source_vessels = {call.vessel_id for call in source_calls}
        source_claims = db.query(VesselRoleClaim).filter(
            VesselRoleClaim.company_id == company.id,
            VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED")),
            VesselRoleClaim.revoked_at.is_(None),
            VesselRoleClaim.effective_start < body.end,
            or_(
                VesselRoleClaim.effective_end.is_(None),
                VesselRoleClaim.effective_end > body.start,
            ),
        )
        for claim in source_claims:
            for vessel_id in set(operational_ids(db, claim)) & source_vessels:
                payload["source_authorization"].append(
                  {
                    "vessel_id": vessel_id,
                    "intervals": [
                        {
                            "start": max(aware(claim.effective_start), body.start),
                            "end": (
                                min(aware(claim.effective_end), body.end)
                                if claim.effective_end
                                else body.end
                            ),
                        }
                    ],
                  }
                )
        payload["operations"] = operational(
            db, body.start, body.end, company.id, body.port_id, body.vessel_id, body.country_code, call_id=body.call_id
        )
        payload["operations"]["average_time_ashore_minutes"] = payload["operations"].get("average_journey_time_ashore_minutes")
        payload["operations"]["duration_coverage"] = payload["operations"].get("journey_duration_coverage")
        for display_key in ("calls", "calls_total", "call_offset", "call_limit"):
            payload["operations"].pop(display_key, None)
        payload["service_reviews"] = review_summary(
            db, body.start, body.end, company.id, body.port_id, body.vessel_id, body.country_code, call_id=body.call_id
        )
    if needs_welfare:
        surveys = selected_rows(
            db, body.start, body.end, company.id, body.port_id, body.vessel_id, body.country_code
        )
        if body.call_id is not None:
            surveys = surveys.filter(WelfareFeedback.vessel_call_id == body.call_id)
        payload["survey"] = statistics(list(surveys.yield_per(500)), True, publication_for_report(db, company.id, body))
        survey_start, survey_end = month_window(body.start, body.end)
        payload["survey"]["period"] = {"start": survey_start, "end": survey_end}
        payload["survey"]["period_note"] = "Survey figures use complete UTC calendar months that intersect the report period."
    from app.services.company_report_data import enrich

    enrich(db, company, body, payload, calls)
    return jsonable_encoder(payload)


@router.get("/types")
def types(context=Depends(current_company_member)):
    return report_catalogue()


@router.get("/options")
def options(db: Session = Depends(get_db), context=Depends(current_company_member)):
    from app.api.v1.routes_welfare import scope_options

    return {"types": report_catalogue(), "sections": [{"code": key, "label": label} for key, label in SECTIONS.items()],
            "scope": scope_options(db, context[0].id)}


@router.get("/calls")
def call_options(country_code: Optional[str] = None, port_id: Optional[int] = None,
                 vessel_id: Optional[int] = None, start: Optional[datetime] = None,
                 end: Optional[datetime] = None, search: str = Query("", max_length=120),
                 offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=100),
                 db: Session = Depends(get_db), context=Depends(current_company_member)):
    query = calls_query(db, context[0].id)
    if (start is None) != (end is None):
        raise HTTPException(422, "Supply both reporting period boundaries.")
    if start is not None:
        if start.tzinfo is None or end.tzinfo is None or not start < end or (end-start).days > 1096:
            raise HTTPException(422, "Choose a valid reporting period of at most three years, with time zones.")
        query = query.filter(VesselCall.started_at < end, func.coalesce(VesselCall.ended_at, func.now()) > start)
    if port_id is not None:
        query = query.filter(VesselCall.port_id == port_id)
    if vessel_id is not None:
        query = query.filter(VesselCall.vessel_id == vessel_id)
    query = filter_country(query, VesselCall.port_id, country_code)
    if search.strip():
        term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        query = query.filter(or_(VesselCall.vessel_name.ilike(term, escape="\\"),
                                 VesselCall.imo_number.ilike(term, escape="\\"),
                                 VesselCall.port_name.ilike(term, escape="\\")))
    return {"items": [{"id": call.id, "vessel_id": call.vessel_id, "vessel_name": call.vessel_name,
                       "imo_number": call.imo_number, "port_id": call.port_id, "port_name": call.port_name,
                       "started_at": call.started_at, "ended_at": call.ended_at}
                      for call in query.order_by(VesselCall.started_at.desc(), VesselCall.id.desc()).offset(offset).limit(limit)],
            "total": query.count(), "offset": offset, "limit": limit}


@router.post("/preview")
def preview(
    body: ReportIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    return prepare(db, context[0], body)


@router.post("", status_code=201)
def generate(
    body: ReportIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor=Depends(get_current_user),
):
    payload = prepare(db, context[0], body)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    row = CompanyManagementReport(
        company_id=context[0].id,
        report_type=body.report_type,
        payload=payload,
        payload_sha256=digest,
        created_by_user_id=actor.id,
    )
    db.add(row)
    db.flush()
    db.add(
        CompanyAuditEvent(
            company_id=context[0].id,
            actor_user_id=actor.id,
            target_type="company_report",
            target_id=row.id,
            action="report_generated",
            after_state={"sha256": digest},
        )
    )
    db.commit()
    return {
        "id": row.id,
        "created_at": row.created_at,
        "payload_sha256": digest,
        "payload": payload,
    }


@router.get("")
def history(
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    q = db.query(CompanyManagementReport).filter_by(company_id=context[0].id)
    return {
        "items": [
            {
                "id": r.id,
                "title": TYPES[r.report_type],
                "created_at": r.created_at,
                "period": r.payload["period"],
            }
            for r in q.order_by(CompanyManagementReport.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        ],
        "total": q.count(),
        "offset": offset,
        "limit": limit,
    }


def load(db, company_id, report_id):
    row = (
        db.query(CompanyManagementReport)
        .filter_by(company_id=company_id, id=report_id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Report not found.")
    actual_digest = hashlib.sha256(
        json.dumps(row.payload, sort_keys=True).encode()
    ).hexdigest()
    if actual_digest != row.payload_sha256:
        raise HTTPException(
            409, "The saved report failed its integrity check. Regenerate it."
        )
    # A snapshot cannot bypass a subsequent relationship revocation.
    call_ids = set(row.payload["call_ids"])
    authorized_calls = {r[0] for r in calls_query(db, company_id).filter(
        VesselCall.id.in_(call_ids)).with_entities(VesselCall.id)}
    if authorized_calls != call_ids:
        raise HTTPException(404, "Port call not found")
    original_scope = row.payload.get("source_authorization")
    if original_scope is None:
        raise HTTPException(403, "Regenerate this report under current access.")
    current_claims = claims_by_vessel(db, company_id, (s["vessel_id"] for s in original_scope))
    for source in original_scope:
        claims = current_claims.get(source["vessel_id"], [])
        for interval in source["intervals"]:
            if not claim_intervals_cover(
                claims,
                datetime.fromisoformat(interval["start"]),
                datetime.fromisoformat(interval["end"]),
            ):
                raise HTTPException(
                    403,
                    "This saved report includes a period outside your current approved access.",
                )
    from app.services.company_report_data import revalidate

    revalidate(db, company_id, row.payload)
    return row


def rows_for(payload):
    def label(key):
        return key.replace("_", " ").replace(".", " ").capitalize()

    def show(value):
        return "Unavailable" if value is None else str(value)

    def stamp(value):
        if not value:
            return "Not recorded"
        return (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%d %b %Y %H:%M UTC")
        )

    rows = [
        ("Company", payload["company_name"]),
        ("Reporting dates (inclusive, UTC)", display_period(payload)),
        (
            "Reporting period",
            stamp(payload["period"]["start"])
            + " to "
            + stamp(payload["period"]["end"])
            + " (end excluded)",
        ),
        ("Metric version", payload["metric_version"]),
        (
            "Data coverage",
            str(payload["coverage"]["recorded_calls"]) + " recorded calls",
        ),
        ("Methodology", payload["methodology"]),
        ("Executive summary", payload.get("executive_summary", "Unavailable")),
    ]
    rows.extend((label(k), show(v)) for k, v in payload.get("summary", {}).items())
    comparison = payload.get("comparison")
    if comparison:
        for row in comparison["rows"]:
            rows.append((label(comparison["dimension"]) + " comparison", row["name"]))
            rows.extend((label(k), show(v)) for k, v in row.items() if k != "name")
    for call in payload["calls"]:
        rows += [
            ("Vessel / port", call["vessel_name"] + " / " + call["port_name"]),
            ("Handling agent", call["agent"]),
            ("ETA", stamp(call["eta"])),
            ("ETD", stamp(call["etd"])),
            ("Call status", call["status"]),
        ]
        rows.extend(
            (label(k), stamp(v) if k == "as_of" else show(v))
            for k, v in call["metrics"].items()
        )
    if "operations" in payload:
        rows.extend(
            (label(k), show(v))
            for k, v in payload["operations"].items()
            if k != "calls"
        )
    if "survey" in payload:
        survey = payload["survey"]
        if survey.get("period"):
            rows.append(("Survey coverage dates (UTC calendar months)", display_period({"period": survey["period"]})))
        if survey.get("status") == "no_responses":
            rows.append(("Crew survey", "No responses"))
        elif survey["suppressed"]:
            rows.append(
                ("Crew survey", "Withheld. Fewer than five eligible respondents.")
            )
        else:
            rows.append(("Survey respondents", survey["respondents"]))
            for key in (
                "obstacles",
                "extra_charge_requested",
                "unable_to_go_ashore",
                "satisfaction",
            ):
                rate = survey.get(key) or {}
                value = (
                    "No responses" if rate.get("status") == "no_responses" else
                    "Withheld. Fewer than five answers."
                    if rate.get("suppressed")
                    else (
                        f"{rate['numerator']} / {rate['denominator']} ({rate['percent']}%)"
                        if rate.get("percent") is not None
                        else "Unavailable. No eligible answers."
                    )
                )
                rows.append((label(key), value))
            for barrier in survey["barriers"]:
                rows.append(
                    (
                        "Reported barrier: "
                        + barrier.get("label", label(barrier["code"])),
                        barrier["respondents"],
                    )
                )
        rows.append(("Survey coverage", survey["coverage"]))
    for group in payload.get("service_reviews", {}).get("groups", []):
        rows.append(
            (
                label(group["type"]) + " service reviews",
                (
                    "Withheld. Fewer than five reviewers."
                    if group["suppressed"]
                    else f"{group['reviews']} reviews; average {group['average_rating']:.2f} / 5"
                ),
            )
        )
    return rows


@router.get("/{report_id}")
def stored(
    report_id: int,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor=Depends(get_current_user),
):
    row = load(db, context[0].id, report_id)
    db.add(
        CompanyAuditEvent(
            company_id=context[0].id,
            actor_user_id=actor.id,
            target_type="company_report",
            target_id=row.id,
            action="report_viewed",
        )
    )
    db.commit()
    return {"id": row.id, "payload_sha256": row.payload_sha256, "payload": row.payload}


@router.get("/{report_id}/export")
def export(
    report_id: int,
    format: Literal["pdf", "csv"] = "pdf",
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor=Depends(get_current_user),
    inline: bool = False,
):
    row = load(db, context[0].id, report_id)
    payload = row.payload
    rows = rows_for(payload)
    db.add(
        CompanyAuditEvent(
            company_id=context[0].id,
            actor_user_id=actor.id,
            target_type="company_report",
            target_id=row.id,
            action="report_exported",
            after_state={"format": format},
        )
    )
    db.commit()
    if format == "pdf":
        data = render_report(payload)
        media = "application/pdf"
    else:

        def safe(v):
            value = str(v)
            return (
                "'" + value
                if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r"))
                else value
            )

        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(["Field", "Value"])
        writer.writerows((safe(k), safe(v)) for k, v in rows)
        data = text.getvalue().encode("utf-8-sig")
        media = "text/csv"
    return Response(
        data,
        media_type=media,
        headers={
            "Content-Disposition": f'{"inline" if inline else "attachment"}; filename="company-report-{row.id}.{format}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/{report_id}/preview.pdf")
def pdf_preview(report_id: int, db: Session = Depends(get_db), context=Depends(current_company_member), actor=Depends(get_current_user)):
    # The exact saved snapshot and renderer are shared with the download.
    # Authorization is checked on each request, including a stale open preview.
    return export(report_id, "pdf", db, context, actor, inline=True)


from app.api.v1.routes_company_demo_reports import router as demo_report_router

router.include_router(demo_report_router)
