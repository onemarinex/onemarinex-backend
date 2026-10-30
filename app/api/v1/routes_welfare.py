"""Optional crew survey, historic context, and privacy-bound welfare statistics."""

from typing import Optional, Literal
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_shipping_company import current_company_member
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.vessel_call import VesselCall
from app.db.models.welfare_feedback import WelfareFeedback
from app.db.models.incident import Incident
from app.services.company_aggregates import tenure_scoped
from app.services.welfare_scope import filter_country
from app.services.welfare_disclosure import threshold, month_window, eligible_rows, screened_text, safe_answers, FIELD_MAP, QUESTIONS, status, screened_facility
from app.db.models.port_geography import PortGeography
from app.db.models.port import Port
from app.db.models.shipping_company import CompanyAuditEvent

crew_router = APIRouter()
company_router = APIRouter()
admin_router = APIRouter()
BARRIERS = {
    "Transportation": {
        "transport_unavailable": "Transport not available",
        "transport_delayed": "Transport delayed",
        "transport_expensive": "Transport too expensive",
        "return_unavailable": "Return transport unavailable",
    },
    "Immigration and Port Access": {
        "immigration_delay": "Immigration or shore-pass processing delay",
        "pass_denied": "Immigration or port authority denied pass",
        "gate_restriction": "Port gate delay or restriction",
    },
    "Shipping Agent": {
        "agent_extra_money": "Agent requested extra money for shore leave or pass",
        "agent_per_crew_payment": "Agent requested payment per crew member",
        "agent_pass_refused": "Agent refused or did not arrange a pass",
        "agent_pass_delayed": "Agent delayed pass arrangements",
        "agent_unavailable": "Agent unavailable or unresponsive",
        "agent_obstruction": "Agent discouraged or obstructed shore leave",
        "agent_transport_missing": "Agent did not arrange transport",
    },
    "Vessel Operations": {
        "short_port_stay": "Cargo operations or port stay too short",
        "work_duties": "Work duties prevented leave",
        "sailing_soon": "Insufficient time before sailing",
    },
    "Welfare Facilities": {
        "facilities_closed": "Desired facilities closed or unavailable",
        "facilities_distant": "Facilities too far or difficult to access",
    },
    "Other": {"other": "Other reason"},
}
CODES = {code for group in BARRIERS.values() for code in group}


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


def require_crew(actor):
    if actor.role != "crew":
        raise HTTPException(403, "Crew access is required.")


def require_admin(actor):
    if actor.role != "superadmin":
        raise HTTPException(403, "Super Admin access is required.")


class SurveyIn(BaseModel):
    assignment_id: Optional[int] = Field(default=None, gt=0)
    experience_at: Optional[datetime] = None
    went_ashore: Optional[Literal["YES", "NO", "DID_NOT_ATTEMPT"]] = None
    difficulty: Optional[Literal["NONE", "DELAYED", "PREVENTED"]] = None
    barriers: list[str] = Field(default_factory=list, max_length=30)
    facility_access: Optional[Literal["YES", "PARTIALLY", "NO", "DID_NOT_TRY"]] = None
    outcome: Optional[
        Literal[
            "DELAYED_SHORTENED",
            "COULD_NOT_GO",
            "NO_SIGNIFICANT_IMPACT",
            "PERSONAL_CHOICE",
        ]
    ] = None
    nonparticipation_reason: Optional[
        Literal["WORK", "REST_CHOICE", "SHORT_STAY", "ACCESS", "TRANSPORT", "OTHER"]
    ] = None
    satisfaction: Optional[int] = Field(default=None, ge=1, le=5)
    extra_charge: Optional[
        Literal["NOT_REQUESTED", "REQUESTED_NOT_PAID", "PAID", "PREFER_NOT_TO_SAY"]
    ] = None
    amount_paid: Optional[float] = Field(
        default=None, ge=0, le=1000000000, allow_inf_nan=False
    )
    currency: Optional[str] = Field(default=None, pattern="^[A-Z]{3}$")
    contact_opt_in: bool = False
    message: str = Field(default="", max_length=5000)

    @model_validator(mode="after")
    def check(self):
        if any(b not in CODES for b in self.barriers):
            raise ValueError("Select a known barrier.")
        if self.difficulty == "NONE" and self.barriers:
            raise ValueError("No difficulties cannot be combined with barriers.")
        if self.amount_paid is not None and (
            self.extra_charge != "PAID" or not self.currency
        ):
            raise ValueError("A paid amount requires a paid response and currency.")
        if (
            self.went_ashore in ("NO", "DID_NOT_ATTEMPT")
            and self.satisfaction is not None
        ):
            raise ValueError(
                "Satisfaction is not applicable when no shore leave was taken."
            )
        if self.assignment_id is None and (
            not self.message.strip()
            or any(
                v is not None
                for v in (
                    self.went_ashore,
                    self.difficulty,
                    self.facility_access,
                    self.outcome,
                    self.extra_charge,
                    self.satisfaction,
                    self.nonparticipation_reason,
                    self.amount_paid,
                    self.currency,
                    self.experience_at,
                )
            )
            or self.barriers
        ):
            raise ValueError(
                "Choose a port call for survey answers. General feedback must contain a message."
            )
        if self.went_ashore == "YES" and self.nonparticipation_reason is not None:
            raise ValueError(
                "A non-participation reason is not applicable when shore leave was taken."
            )
        if self.assignment_id is not None and self.experience_at is None:
            raise ValueError("Enter when the experience occurred.")
        return self


@crew_router.get("/welfare-feedback/options")
def options(actor=Depends(get_current_user)):
    require_crew(actor)
    return {
        "barriers": BARRIERS,
        "privacy_notice": "Answers describe your reported experience. Company views use privacy-protected totals. Identifying comments are not sent automatically to an agent. Contact is optional.",
    }


@crew_router.get("/welfare-feedback/calls")
def available_calls(
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_crew(actor)
    query = (
        db.query(CrewAssignment, VesselCall)
        .join(CrewProfile, CrewProfile.id == CrewAssignment.crew_profile_id)
        .join(VesselCall, VesselCall.id == CrewAssignment.vessel_call_id)
        .filter(CrewProfile.user_id == actor.id)
    )
    return {
        "items": [
            {
                "assignment_id": a.id,
                "vessel_name": c.vessel_name,
                "port_name": c.port_name
                or (
                    db.get(Port, c.port_id).name
                    if c.port_id and db.get(Port, c.port_id)
                    else None
                ),
                "started_at": a.started_at,
                "ended_at": a.ended_at,
                "call_id": c.id,
            }
            for a, c in query.order_by(
                CrewAssignment.started_at.desc(), CrewAssignment.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        ],
        "total": query.count(),
        "offset": offset,
        "limit": limit,
    }


@crew_router.post("/welfare-feedback", status_code=201)
def submit(
    body: SurveyIn, db: Session = Depends(get_db), actor=Depends(get_current_user)
):
    require_crew(actor)
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    assignment = None
    call = None
    if body.assignment_id:
        assignment = (
            db.query(CrewAssignment)
            .join(CrewProfile)
            .filter(
                CrewAssignment.id == body.assignment_id, CrewProfile.user_id == actor.id
            )
            .first()
        )
        if assignment is None:
            raise HTTPException(404, "Crew assignment not found.")
        call = db.get(VesselCall, assignment.vessel_call_id)
        at = body.experience_at
        if at.tzinfo is None:
            raise HTTPException(422, "Experience time must include a time zone.")
        ends = [
            aware(x)
            for x in (assignment.ended_at, call.ended_at, datetime.now(timezone.utc))
            if x
        ]
        if (
            not max(aware(assignment.started_at), aware(call.started_at))
            <= at
            <= min(ends)
        ):
            raise HTTPException(
                422,
                "The experience time must be inside the selected assignment and call.",
            )
    row = (
        db.query(WelfareFeedback)
        .filter_by(user_id=actor.id, assignment_id=body.assignment_id)
        .first()
        if assignment
        else None
    )
    if row is None:
        row = WelfareFeedback(user_id=actor.id, assignment_id=body.assignment_id)
        db.add(row)
    row.answers = body.model_dump(
        mode="json",
        exclude={"assignment_id", "experience_at", "message", "contact_opt_in"},
    )
    row.message = body.message.strip()
    row.contact_opt_in = body.contact_opt_in
    row.experience_at = body.experience_at
    if call:
        row.vessel_call_id = call.id
        row.vessel_id = call.vessel_id
        row.port_id = call.port_id
    db.commit()
    return {
        "id": row.id,
        "status": "received",
        "message": "Feedback received. You can update the survey for this call.",
    }


def selected_rows(
    db, start, end, company_id=None, port_id=None, vessel_id=None, country_code=None
):
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise HTTPException(422, "Use a valid time range with time zones.")
    if (end - start).days > 1096:
        raise HTTPException(422, "Choose a range of at most three years.")
    if company_id:
        start, end = month_window(start, end)
    query = db.query(WelfareFeedback).filter(
        WelfareFeedback.experience_at >= start, WelfareFeedback.experience_at < end
    )
    if port_id:
        query = query.filter(WelfareFeedback.port_id == port_id)
    if vessel_id:
        query = query.filter(WelfareFeedback.vessel_id == vessel_id)
    if company_id:
        query = tenure_scoped(
            db,
            query,
            company_id=company_id,
            vessel_column=WelfareFeedback.vessel_id,
            occurred_column=WelfareFeedback.experience_at,
            include_declared=True,
        )
    query = filter_country(query, WelfareFeedback.port_id, country_code)
    return query


def survey_publication_ids(db, company_id):
    """Request-local global eligibility makes port/date/vessel unions composable.

    Population and each question release independently. Rare details never hide
    an unrelated safe question; the detailed explorer additionally screens the
    joint signature to prevent recombining fields into an individual profile.
    """
    import json
    cache = db.info.setdefault("welfare_survey_publication", {})
    if company_id not in cache:
        query = tenure_scoped(db, db.query(WelfareFeedback).filter(WelfareFeedback.experience_at.isnot(None)), company_id=company_id, vessel_column=WelfareFeedback.vessel_id, occurred_column=WelfareFeedback.experience_at, include_declared=True)
        universe = query.all()
        variants = {"population": lambda r:"respondent", "detail": None}
        for key in QUESTIONS:
            variants["question:"+key] = lambda r,key=key:json.dumps((r.answers or {}).get(key),sort_keys=True)
        positives = {"difficulty": {"DELAYED", "PREVENTED"}, "extra_charge": {"REQUESTED_NOT_PAID", "PAID"}, "went_ashore": {"NO"}, "satisfaction": {4,5}}
        for key, values in positives.items():
            variants["rate:"+key] = lambda r,key=key,values=values:"missing" if (r.answers or {}).get(key) is None else str((r.answers or {}).get(key) in values)
        cache[company_id] = {key:{r.id for r in eligible_rows(universe, variant=variant)} for key, variant in variants.items()}
    return cache[company_id]


def statistics(rows, suppress=False, publication_ids=None):
    # Coverage is deliberately constant for company publication: a conditional
    # partial/complete flag would itself disclose excluded cohort existence.
    publication_coverage = {
        "coverage_status": "partial_withheld" if suppress else "all_recorded",
        "coverage_note": (
            "Privacy-protected survey coverage. Figures describe published cohorts only and may exclude restricted responses; they are not totals for all source responses."
            if suppress else
            "All recorded survey responses in the authorized source scope. Missing answers are excluded per question."
        ),
    }
    # Canonical publication leaves prevent totals disclosing excluded cells.
    had_rows = bool(rows)
    if suppress:
        rows = [r for r in rows if r.id in publication_ids["population"]] if publication_ids is not None else eligible_rows(rows)
    # One respondent per question. For repeated calls, use the latest answer.
    ordered = sorted(rows, key=lambda r: (aware(r.updated_at), r.id), reverse=True)
    people = {r.user_id for r in rows}
    if suppress and len(people) < threshold():
        return {
            **publication_coverage,
            "suppressed": True,
            "status": "insufficient_sample" if had_rows else "no_responses",
            "respondents": None if had_rows else 0,
            "field_map": FIELD_MAP,
            "questions": {key: {"status": "insufficient_sample" if had_rows else "no_responses", "distribution": []} for key in QUESTIONS},
            "obstacles": None,
            "extra_charge_requested": None,
            "unable_to_go_ashore": None,
            "barriers": [],
            "coverage": "Too few respondents to show.",
        }

    def share(key, positive):
        answers = {}
        for r in ordered:
            value = (r.answers or {}).get(key)
            if value is not None and r.user_id not in answers:
                answers[r.user_id] = value
        # Each question also has its own privacy boundary.
        globally_hidden = suppress and publication_ids is not None and any(r.id not in publication_ids["rate:"+key] for r in rows if (r.answers or {}).get(key) is not None)
        if suppress and (len(answers) < threshold() or globally_hidden):
            return {
                "numerator": None,
                "denominator": None,
                "percent": None,
                "suppressed": True,
                "status": "insufficient_sample" if answers else "no_responses",
            }
        count = sum(v in positive for v in answers.values())
        n = len(answers)
        if suppress and any(0 < size < threshold() for size in (count, n-count)):
            return {"numerator": None, "denominator": None, "percent": None, "suppressed": True, "status": "insufficient_sample"}
        return {
            "numerator": count,
            "denominator": n,
            "percent": round(count * 100 / n, 1) if n else None,
            "suppressed": False,
            "status": status(count, empty=not n),
        }

    barriers = []
    for code in sorted(CODES):
        n = len(
            {r.user_id for r in rows if code in (r.answers or {}).get("barriers", [])}
        )
        if suppress and ((publication_ids is not None and any(r.id not in publication_ids["question:barriers"] for r in rows)) or 0 < n < threshold() or 0 < len(people)-n < threshold()):
            continue
        barriers.append(
            {
                "code": code,
                "label": next(
                    group[code] for group in BARRIERS.values() if code in group
                ),
                "respondents": n,
            }
        )
    questions = {}
    for key in QUESTIONS:
        latest = {}
        for r in ordered:
            value = (r.answers or {}).get(key)
            if value is not None and r.user_id not in latest:
                latest[r.user_id] = value
        counts = {}
        for value in latest.values():
            for item in (value if isinstance(value, list) else [value]):
                counts[str(item)] = counts.get(str(item), 0) + 1
        hidden = suppress and ((publication_ids is not None and any(r.id not in publication_ids["question:"+key] for r in rows if (r.answers or {}).get(key) is not None)) or len(latest) < threshold() or any(0 < n < threshold() or 0 < len(latest)-n < threshold() for n in counts.values()))
        questions[key] = {"status": status(len(latest), suppressed=hidden, empty=not latest), "applicable_respondents": None if hidden else len(latest), "distribution": [] if hidden else [{"answer": key, "respondents": n} for key, n in sorted(counts.items())]}
    return {
        **publication_coverage,
        "suppressed": False,
        "status": status(len(people), empty=not people),
        "questions": questions,
        "field_map": FIELD_MAP,
        "respondents": len(people),
        "responses": len(rows),
        "obstacles": share("difficulty", {"DELAYED", "PREVENTED"}),
        "extra_charge_requested": share("extra_charge", {"REQUESTED_NOT_PAID", "PAID"}),
        "unable_to_go_ashore": share("went_ashore", {"NO"}),
        "satisfaction": share("satisfaction", {4, 5}),
        "barriers": sorted((row for row in barriers if row["respondents"] > 0), key=lambda row: (-row["respondents"], row["code"])),
        "coverage": "Survey respondents only. Missing answers are excluded per question. Allegations are unverified. Category selections can overlap.",
    }


@company_router.get("/summary")
def company_summary(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, _ = context
    selected_rows(db, start, end, company.id, port_id, vessel_id, country_code)
    start, end = month_window(start, end)
    rows = list(
        selected_rows(
            db, start, end, company.id, port_id, vessel_id, country_code
        ).yield_per(500)
    )
    result = statistics(rows, True, survey_publication_ids(db, company.id))
    result.update(start=start, end=end)
    return result


@admin_router.get("/summary")
def admin_summary(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    rows = list(
        selected_rows(db, start, end, None, port_id, vessel_id, country_code).yield_per(
            500
        )
    )
    result = statistics(rows)
    result.update(start=start, end=end)
    return result


@admin_router.get("/feedback")
def admin_feedback(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    search: str = Query("", max_length=120),
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    query = db.query(WelfareFeedback)
    if (start is None) != (end is None):
        raise HTTPException(422, "Supply both period boundaries.")
    if start is not None:
        selected_rows(db, start, end)
        at = func.coalesce(WelfareFeedback.experience_at, WelfareFeedback.created_at)
        query = query.filter(at >= start, at < end)
    if port_id:
        query = query.filter_by(port_id=port_id)
    if vessel_id:
        query = query.filter_by(vessel_id=vessel_id)
    query = filter_country(query, WelfareFeedback.port_id, country_code)
    if search:
        term="%"+search.replace("%", "\\%").replace("_", "\\_")+"%"
        from sqlalchemy import cast, String
        query=query.filter(or_(*(cast(WelfareFeedback.answers[key],String).ilike(term, escape="\\") for key in QUESTIONS)))
    return {
        "items": [
            {
                "id": r.id,
                "call_id": r.vessel_call_id,
                "port_id": r.port_id,
                "vessel_id": r.vessel_id,
                "created_at": r.created_at,
                "experience_at": r.experience_at,
                "updated_at": r.updated_at,
                "message": "Original text requires audited investigation.",
                "answers": {k:v for k,v in (r.answers or {}).items() if k not in ("amount_paid", "currency")},
                "field_map": FIELD_MAP,
                "port_name": db.get(Port, r.port_id).name if r.port_id and db.get(Port, r.port_id) else None,
                "vessel_name": db.get(VesselCall, r.vessel_call_id).vessel_name if r.vessel_call_id and db.get(VesselCall, r.vessel_call_id) else None,
            }
            for r in query.order_by(
                WelfareFeedback.created_at.desc(), WelfareFeedback.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        ],
        "total": query.count(),
        "offset": offset,
        "limit": limit,
    }


@admin_router.get("/legacy-feedback")
def legacy_feedback(
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    query = db.query(Incident).filter(Incident.title == "Crew Feedback")
    if (start is None) != (end is None):
        raise HTTPException(422, "Supply both period boundaries.")
    if start is not None:
        selected_rows(db, start, end)
        query = query.filter(Incident.created_at >= start, Incident.created_at < end)
    if port_id is not None:
        query = query.filter(Incident.port_id == port_id)
    if vessel_id is not None:
        query = query.filter(Incident.vessel_id == vessel_id)
    query = filter_country(query, Incident.port_id, country_code)
    return {
        "items": [
            {
                "id": r.id,
                "message": "Original text requires audited investigation.",
                "created_at": r.created_at,
                "source": "legacy_unstructured",
            }
            for r in query.order_by(Incident.created_at.desc(), Incident.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        ],
        "total": query.count(),
        "offset": offset,
        "limit": limit,
    }


@company_router.get("/operations")
def company_operations(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    review_type: Optional[Literal["driver", "facility_stop"]] = None,
    provider_id: Optional[int] = Query(None, ge=1),
    call_offset: int = Query(0, ge=0),
    call_limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    from app.services.welfare_analytics import operational, review_summary

    selected_rows(
        db, start, end, context[0].id, port_id, vessel_id, country_code
    )  # Validate the shared period.
    return {
        "operations": operational(
            db,
            start,
            end,
            context[0].id,
            port_id,
            vessel_id,
            country_code,
            call_offset,
            call_limit,
        ),
        "reviews": review_summary(
            db,
            start,
            end,
            context[0].id,
            port_id,
            vessel_id,
            country_code,
            review_type,
            provider_id,
        ),
    }


@admin_router.get("/operations")
def admin_operations(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    review_type: Optional[Literal["driver", "facility_stop"]] = None,
    provider_id: Optional[int] = Query(None, ge=1),
    call_offset: int = Query(0, ge=0),
    call_limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    from app.services.welfare_analytics import operational, review_summary

    require_admin(actor)
    selected_rows(db, start, end, None, port_id, vessel_id, country_code)
    return {
        "operations": operational(
            db,
            start,
            end,
            None,
            port_id,
            vessel_id,
            country_code,
            call_offset,
            call_limit,
        ),
        "reviews": review_summary(
            db,
            start,
            end,
            None,
            port_id,
            vessel_id,
            country_code,
            review_type,
            provider_id,
        ),
    }


def scope_options(db, company_id=None):
    from app.db.models.port import Port
    from app.db.models.vessel import Vessel

    if company_id:
        from app.api.v1.routes_company_intelligence import calls_query

        query = calls_query(db, company_id)
    else:
        query = db.query(VesselCall)
    port_ids = query.with_entities(VesselCall.port_id).distinct().subquery()
    vessel_ids = query.with_entities(VesselCall.vessel_id).distinct().subquery()
    ports = (
        db.query(Port)
        .filter(Port.id.in_(db.query(port_ids.c.port_id)))
        .order_by(Port.name, Port.id)
    )
    vessels = (
        db.query(Vessel)
        .filter(Vessel.id.in_(db.query(vessel_ids.c.vessel_id)))
        .order_by(Vessel.name, Vessel.id)
    )
    return {
        "ports": [
            {
                "id": p.id,
                "name": p.name,
                "country_code": (
                    db.get(PortGeography, p.id).country_code
                    if db.get(PortGeography, p.id)
                    else None
                ),
            }
            for p in ports.limit(500).all()
        ],
        "countries": sorted(
            {
                r.country_code
                for r in db.query(PortGeography).filter(
                    PortGeography.port_id.in_(db.query(port_ids.c.port_id))
                )
            }
        ),
        "vessels": [{"id": v.id, "name": v.name} for v in vessels.limit(500).all()],
        "ports_truncated": ports.count() > 500,
        "vessels_truncated": vessels.count() > 500,
        "country_coverage": "Country filters use reviewed port metadata. Unmapped ports remain in the Country not recorded group.",
    }


@company_router.get("/scope")
def company_scope(
    db: Session = Depends(get_db), context=Depends(current_company_member)
):
    return scope_options(db, context[0].id)


@admin_router.get("/scope")
def admin_scope(db: Session = Depends(get_db), actor=Depends(get_current_user)):
    require_admin(actor)
    return scope_options(db)


@admin_router.get("/reviews")
def admin_reviews(
    start: datetime,
    end: datetime,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    review_type: Optional[Literal["driver", "facility_stop"]] = None,
    provider_id: Optional[int] = Query(None, ge=1),
    search: str = Query("", max_length=120),
    rating: Optional[int] = Query(None, ge=1, le=5),
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    from app.db.models.booking_review import BookingReview
    from app.db.models.cab_booking import CabBooking

    require_admin(actor)
    selected_rows(db, start, end)
    query = (
        db.query(BookingReview, CabBooking, VesselCall)
        .join(CabBooking, CabBooking.id == BookingReview.booking_id)
        .outerjoin(VesselCall, VesselCall.id == CabBooking.vessel_call_id)
        .filter(BookingReview.created_at >= start, BookingReview.created_at < end)
    )
    if port_id:
        query = query.filter(VesselCall.port_id == port_id)
    if vessel_id:
        query = query.filter(CabBooking.vessel_id == vessel_id)
    query = filter_country(query, VesselCall.port_id, country_code)
    if review_type:
        query = query.filter(BookingReview.review_type == review_type)
    if provider_id is not None:
        query = query.filter(
            func.coalesce(CabBooking.provider_id, CabBooking.aggregator_id)
            == provider_id
        )
    if rating is not None:
        query=query.filter(func.round(BookingReview.rating)==rating)
    if search:
        term="%"+search.replace("%", "\\%").replace("_", "\\_")+"%"
        query=query.filter(or_(BookingReview.review_type.ilike(term, escape="\\"), VesselCall.vessel_name.ilike(term, escape="\\"), VesselCall.port_name.ilike(term, escape="\\")))
    return {
        "items": [
            {
                "id": r.id,
                "type": r.review_type,
                "rating": r.rating,
                "comment": "Original text requires audited investigation.",
                "facility": screened_facility(r.facility_name),
                "call_id": b.vessel_call_id,
                "provider": (
                    b.provider.company_name
                    if b.provider
                    else b.aggregator.company_name if b.aggregator else None
                ),
                "port_name": c.port_name if c else b.port,
                "vessel_name": c.vessel_name if c else None,
                "created_at": r.created_at,
            }
            for r, b, c in query.order_by(
                BookingReview.created_at.desc(), BookingReview.id.desc()
            )
            .offset(offset)
            .limit(limit)
            .all()
        ],
        "total": query.count(),
        "offset": offset,
        "limit": limit,
    }


class GeographyIn(BaseModel):
    model_config = {"str_strip_whitespace": True}
    country_code: str = Field(pattern="^[A-Z]{2}$")
    evidence_reference: str = Field(min_length=5, max_length=1000)


@admin_router.put("/port-geography/{port_id}")
def update_geography(
    port_id: int,
    body: GeographyIn,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    port = db.query(Port).filter_by(id=port_id).with_for_update().first()
    if port is None:
        raise HTTPException(404, "Port not found.")
    row = db.get(PortGeography, port_id)
    before = {"country_code": row.country_code} if row else None
    if row is None:
        row = PortGeography(port_id=port_id)
        db.add(row)
    row.country_code = body.country_code
    row.evidence_reference = body.evidence_reference.strip()
    row.updated_by_user_id = actor.id
    db.add(
        CompanyAuditEvent(
            actor_user_id=actor.id,
            target_type="port_geography",
            target_id=port_id,
            action="port_country_recorded",
            before_state=before,
            after_state={"country_code": body.country_code},
            evidence_reference=row.evidence_reference,
        )
    )
    db.commit()
    return {"port_id": port_id, "country_code": row.country_code}


def comparisons(
    db, start, end, company_id, port_id, vessel_id, country_code, offset, limit
):
    from app.api.v1.routes_company_intelligence import calls_query
    from app.services.welfare_analytics import operational, review_summary, operational_query
    from app.db.models.cab_booking import CabBooking
    from sqlalchemy import or_

    selected_rows(db, start, end, company_id, port_id, vessel_id, country_code)
    if company_id:
        start, end = month_window(start, end)
    calls = calls_query(db, company_id) if company_id else db.query(VesselCall)
    calls = calls.filter(
        VesselCall.started_at < end,
        or_(VesselCall.ended_at.is_(None), VesselCall.ended_at > start),
    )
    if port_id:
        calls = calls.filter(VesselCall.port_id == port_id)
    if vessel_id:
        calls = calls.filter(VesselCall.vessel_id == vessel_id)
    calls = filter_country(calls, VesselCall.port_id, country_code)
    ports = db.query(Port).filter(Port.id.in_(calls.with_entities(VesselCall.port_id)))
    activity = operational_query(db,start,end,company_id,port_id,vessel_id,country_code).with_entities(VesselCall.port_id.label("port_id"),func.sum(CabBooking.num_passengers).label("activity")).group_by(VesselCall.port_id).subquery()
    ports = ports.outerjoin(activity,activity.c.port_id==Port.id)
    rows = []
    for port in ports.order_by(func.coalesce(activity.c.activity,0).desc(),Port.name, Port.id).offset(offset).limit(limit):
        rows.append(
            {
                "port_id": port.id,
                "port_name": port.name,
                "operations": operational(
                    db, start, end, company_id, port.id, vessel_id, country_code
                ),
                "survey": statistics(
                    list(
                        selected_rows(
                            db, start, end, company_id, port.id, vessel_id, country_code
                        ).yield_per(500)
                    ),
                    bool(company_id),
                    survey_publication_ids(db, company_id) if company_id else None,
                ),
                "reviews": review_summary(
                    db, start, end, company_id, port.id, vessel_id, country_code
                ),
            }
        )
    return {"items": rows, "total": ports.count(), "offset": offset, "limit": limit}


@company_router.get("/comparisons")
def company_comparisons(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=50),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    return comparisons(
        db, start, end, context[0].id, port_id, vessel_id, country_code, offset, limit
    )


@admin_router.get("/comparisons")
def admin_comparisons(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=50),
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    return comparisons(
        db, start, end, None, port_id, vessel_id, country_code, offset, limit
    )


def monthly_trends(db, start, end, company_id, port_id, vessel_id, country_code):
    from app.services.welfare_analytics import operational

    selected_rows(db, start, end, company_id, port_id, vessel_id, country_code)
    if (end - start).days > 366:
        raise HTTPException(422, "Narrow monthly trends to one year.")
    if company_id:
        start, end = month_window(start, end)
    cursor = start
    rows = []
    while cursor < end:
        boundary = datetime(
            cursor.year + (cursor.month == 12),
            (cursor.month % 12) + 1,
            1,
            tzinfo=timezone.utc,
        )
        stop = min(boundary, end)
        operations = operational(
            db, cursor, stop, company_id, port_id, vessel_id, country_code
        )
        survey = statistics(
            list(
                selected_rows(
                    db, cursor, stop, company_id, port_id, vessel_id, country_code
                ).yield_per(500)
            ),
            bool(company_id),
            survey_publication_ids(db, company_id) if company_id else None,
        )
        rows.append(
            {
                "start": cursor,
                "end": stop,
                "participation_seats": operations["crew_participation"],
                "distinct_crew": operations.get("distinct_crew"),
                "completed_journeys": operations.get("completed_journeys"),
                "calls_with_shore_leave": operations["calls_with_shore_leave"],
                "recorded_calls": operations["recorded_calls"],
                "average_time_ashore_minutes": operations[
                    "average_time_ashore_minutes"
                ],
                "survey": survey,
            }
        )
        cursor = stop
    return {
        "items": rows,
        "coverage": "UTC calendar-month slices. Seats and completed journeys are additive. People, survey respondents and calls are distinct across the whole period and must not be summed from monthly values.",
    }


@company_router.get("/trends")
def company_trends(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    return monthly_trends(
        db, start, end, context[0].id, port_id, vessel_id, country_code
    )


@admin_router.get("/trends")
def admin_trends(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    return monthly_trends(db, start, end, None, port_id, vessel_id, country_code)


def provider_options(db, start, end, company_id, port_id, vessel_id, country_code):
    from app.services.welfare_analytics import reviews_query
    from app.db.models.cab_booking import CabBooking
    from app.db.models.aggregator_profile import AggregatorProfile

    selected_rows(db, start, end, company_id, port_id, vessel_id, country_code)
    query = reviews_query(db, start, end, company_id, port_id, vessel_id, country_code)
    identities = query.with_entities(
        func.coalesce(CabBooking.provider_id, CabBooking.aggregator_id)
    ).distinct()
    providers = (
        db.query(AggregatorProfile)
        .filter(AggregatorProfile.id.in_(identities))
        .order_by(AggregatorProfile.company_name, AggregatorProfile.id)
    )
    return {
        "items": [{"id": p.id, "name": p.company_name} for p in providers.limit(500)],
        "truncated": providers.count() > 500,
    }


@company_router.get("/review-providers")
def company_review_providers(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    return provider_options(
        db, start, end, context[0].id, port_id, vessel_id, country_code
    )


@admin_router.get("/review-providers")
def admin_review_providers(
    start: datetime,
    end: datetime,
    port_id: Optional[int] = None,
    vessel_id: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    actor=Depends(get_current_user),
):
    require_admin(actor)
    return provider_options(db, start, end, None, port_id, vessel_id, country_code)


@company_router.get("/vessels")
def company_vessel_comparison(start: datetime, end: datetime, port_id: Optional[int] = None, vessel_id: Optional[int] = None, country_code: Optional[str] = None, search: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db), context=Depends(current_company_member)):
    from app.api.v1.routes_company_intelligence import calls_query
    from app.db.models.vessel import Vessel
    from app.services.welfare_analytics import operational, operational_query
    from app.db.models.cab_booking import CabBooking
    selected_rows(db,start,end,context[0].id,port_id,vessel_id,country_code)
    calls=calls_query(db,context[0].id).filter(VesselCall.started_at < end,or_(VesselCall.ended_at.is_(None),VesselCall.ended_at > start))
    if port_id: calls=calls.filter(VesselCall.port_id==port_id)
    if vessel_id: calls=calls.filter(VesselCall.vessel_id==vessel_id)
    calls=filter_country(calls,VesselCall.port_id,country_code)
    query=db.query(Vessel).filter(Vessel.id.in_(calls.with_entities(VesselCall.vessel_id)))
    if search:
        term="%"+search.replace("%","\\%").replace("_","\\_")+"%"
        query=query.filter(or_(Vessel.name.ilike(term,escape="\\"),Vessel.imo_number.ilike(term,escape="\\")))
    activity=operational_query(db,start,end,context[0].id,port_id,vessel_id,country_code).with_entities(CabBooking.vessel_id.label("vessel_id"),func.sum(CabBooking.num_passengers).label("activity")).group_by(CabBooking.vessel_id).subquery()
    query=query.outerjoin(activity,activity.c.vessel_id==Vessel.id)
    return {"items":[{"vessel_id":v.id,"vessel_name":v.name,"imo_number":v.imo_number,"operations":operational(db,start,end,context[0].id,port_id,v.id,country_code),"survey":statistics(list(selected_rows(db,start,end,context[0].id,port_id,v.id,country_code).yield_per(500)),True,survey_publication_ids(db,context[0].id))} for v in query.order_by(func.coalesce(activity.c.activity,0).desc(),Vessel.name,Vessel.id).offset(offset).limit(limit)],"total":query.count(),"offset":offset,"limit":limit}


@company_router.get("/feedback")
def company_feedback(start: datetime, end: datetime, port_id: Optional[int] = None, vessel_id: Optional[int] = None, country_code: Optional[str] = None, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), search: str = Query("", max_length=120), db: Session = Depends(get_db), context=Depends(current_company_member)):
    from collections import defaultdict
    source = selected_rows(db, start, end, context[0].id, port_id, vessel_id, country_code).all()
    ids = survey_publication_ids(db, context[0].id)
    safe = [r for r in source if r.id in ids["detail"]]
    groups = defaultdict(list)
    from app.services.welfare_disclosure import signature
    for row in safe:
        groups[(row.experience_at.strftime("%Y-%m"), row.port_id, row.vessel_id, signature(row))].append(row)
    items = []
    for (month, port, vessel, _), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        call = db.get(VesselCall, rows[0].vessel_call_id)
        record = {"month": month, "port_name": db.get(Port, port).name if port and db.get(Port, port) else "Not recorded", "vessel_name": call.vessel_name if call else "Not recorded", "respondents": len({r.user_id for r in rows}), "answers": safe_answers(rows[0]), "message": screened_text(rows[0].message), "text_status": "available" if screened_text(rows[0].message) else "withheld"}
        if not search or search.casefold() in str(record).casefold():
            items.append(record)
    return {"items": items[offset:offset+limit], "total": len(items), "offset": offset, "limit": limit, "status": "available" if items else "insufficient_sample" if source else "no_responses", "field_map": FIELD_MAP, "coverage": "Grouped screened responses, not individual records. Each entire month/port/vessel publication cell and answer signature requires the minimum distinct respondents. Reported allegations are unverified; identifying and unreviewed text is withheld."}


@company_router.get("/reviews")
def company_reviews(start: datetime, end: datetime, port_id: Optional[int] = None, vessel_id: Optional[int] = None, country_code: Optional[str] = None, review_type: Optional[Literal["driver", "facility_stop"]] = None, provider_id: Optional[int] = Query(None, ge=1), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), search: str = Query("", max_length=120), db: Session = Depends(get_db), context=Depends(current_company_member)):
    from app.services.welfare_analytics import reviews_query, company_review_publication, review_variant
    from collections import defaultdict
    selected_rows(db, start, end, context[0].id)
    start, end = month_window(start, end)
    source = reviews_query(db, start, end, context[0].id, port_id, vessel_id, country_code).all()
    safe = company_review_publication(db, source, context[0].id)
    groups = defaultdict(list)
    for row in safe:
        if review_type and row.review_type != review_type:
            continue
        booking = row.booking
        provider = booking.provider_id or booking.aggregator_id
        if provider_id is not None and provider != provider_id:
            continue
        groups[(row.created_at.strftime("%Y-%m"), booking.vessel_call_id and db.get(VesselCall, booking.vessel_call_id).port_id, booking.vessel_id, review_variant(row))].append(row)
    items = []
    for (month, port, vessel, _), rows in sorted(groups.items(), key=lambda item: str(item[0])):
        row = rows[0]; booking = row.booking; call = db.get(VesselCall, booking.vessel_call_id)
        record = {"month": month, "port_name": db.get(Port, port).name if port and db.get(Port, port) else "Not recorded", "vessel_name": call.vessel_name if call else "Not recorded", "provider": booking.provider.company_name if booking.provider else booking.aggregator.company_name if booking.aggregator else "Not recorded", "type": row.review_type, "facility": screened_facility(row.facility_name) if row.review_type == "facility_stop" else None, "rating": row.rating, "review": screened_text(row.review_text), "text_status": "available" if screened_text(row.review_text) else "withheld", "respondents": len({r.crew_id for r in rows})}
        if not search or search.casefold() in str(record).casefold():
            items.append(record)
    return {"items": items[offset:offset+limit], "total": len(items), "offset": offset, "limit": limit, "status": "available" if items else "insufficient_sample" if source else "no_responses", "coverage": "Grouped screened service reviews. Drivers' personal names, precise dates, identifiers and unreviewed prose remain restricted. Ratings measure service, not overall shore-leave satisfaction."}


class InvestigationIn(BaseModel):
    purpose: str = Field(min_length=15, max_length=1000)
    evidence_reference: str = Field(min_length=5, max_length=250)


@admin_router.post("/investigation/{kind}/{record_id}")
def investigate(kind: Literal["feedback", "review", "legacy"], record_id: int, body: InvestigationIn, response: Response, db: Session = Depends(get_db), actor=Depends(get_current_user)):
    """Purpose-bound reveal; never put original text or identity into a GET list/export."""
    from app.db.models.booking_review import BookingReview
    model = WelfareFeedback if kind == "feedback" else BookingReview if kind == "review" else Incident
    require_admin(actor)
    row = db.get(model, record_id)
    if row is None or (kind == "legacy" and row.title != "Crew Feedback"):
        raise HTTPException(404, "Evidence record not found.")
    if not body.purpose.strip() or not body.evidence_reference.strip():
        raise HTTPException(422, "Record an investigation purpose and case reference.")
    payload = {"id": row.id, "kind": kind, "message": row.message, "answers": row.answers, "contact_opt_in": row.contact_opt_in, "user_id": row.user_id, "experience_at": row.experience_at, "created_at": row.created_at, "call_id": row.vessel_call_id} if kind == "feedback" else {"id": row.id, "kind": kind, "message": row.review_text, "crew_id": row.crew_id, "booking_id": row.booking_id, "driver_id": row.driver_id, "facility": row.facility_name} if kind == "review" else {"id": row.id, "kind": kind, "message": row.description}
    if kind == "feedback":
        user = db.get(User, row.user_id)
        payload["identity"] = {"name": user.name, "email": user.email} if user else None
        payload["updated_at"] = row.updated_at
    elif kind == "review":
        payload.update(rating=row.rating, type=row.review_type, created_at=row.created_at, facility_stop_id=row.facility_stop_id)
        payload["driver"] = {"name": row.driver.name, "phone": row.driver.phone} if row.driver else None
    db.add(CompanyAuditEvent(actor_user_id=actor.id, target_type="welfare_"+kind, target_id=row.id, action="welfare_sensitive_evidence_revealed", after_state={"purpose": body.purpose.strip(), "fields": list(payload)}, evidence_reference=body.evidence_reference.strip()))
    db.commit()
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    return {"evidence": payload, "notice": "Reported account; not proven misconduct. Use only for the recorded investigation purpose."}


@admin_router.get("/vessels")
def admin_vessels(start: datetime, end: datetime, port_id: Optional[int] = None, vessel_id: Optional[int] = None, country_code: Optional[str] = None, search: str = Query("", max_length=120), offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db), actor=Depends(get_current_user)):
    from app.db.models.vessel import Vessel
    from app.services.welfare_analytics import operational
    require_admin(actor)
    selected_rows(db, start, end)
    query = db.query(Vessel)
    if vessel_id:
        query = query.filter(Vessel.id == vessel_id)
    if search:
        query = query.filter(Vessel.name.ilike("%"+search.replace("%", "\\%").replace("_", "\\_")+"%", escape="\\"))
    return {"items": [{"vessel_id": v.id, "vessel_name": v.name, "imo_number": v.imo_number, "operations": operational(db, start, end, None, port_id, v.id, country_code), "survey": statistics(selected_rows(db, start, end, None, port_id, v.id, country_code).all())} for v in query.order_by(Vessel.name, Vessel.id).offset(offset).limit(limit)], "total": query.count(), "offset": offset, "limit": limit}


@company_router.get("/monthly-ports")
def company_monthly_ports(start: datetime, end: datetime, port_id: Optional[int] = None, vessel_id: Optional[int] = None, country_code: Optional[str] = None, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=50), db: Session = Depends(get_db), context=Depends(current_company_member)):
    return monthly_ports(db, start, end, context[0].id, port_id, vessel_id, country_code, offset, limit)


@admin_router.get("/monthly-ports")
def admin_monthly_ports(start: datetime, end: datetime, port_id: Optional[int] = None, vessel_id: Optional[int] = None, country_code: Optional[str] = None, offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=50), db: Session = Depends(get_db), actor=Depends(get_current_user)):
    require_admin(actor)
    return monthly_ports(db, start, end, None, port_id, vessel_id, country_code, offset, limit)


def monthly_ports(db, start, end, company_id, port_id, vessel_id, country_code, offset, limit):
    page = comparisons(db, start, end, company_id, port_id, vessel_id, country_code, offset, limit)
    return {**page, "items": [{**row, "months": monthly_trends(db, start, end, company_id, row["port_id"], vessel_id, country_code)["items"]} for row in page["items"]], "coverage": "Monthly seats and completed journeys sum to period totals. Distinct participants, calls, survey respondents and satisfaction denominators are recalculated for the period; never sum distinct monthly counts."}
