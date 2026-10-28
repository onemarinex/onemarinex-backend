"""The shipping company's own API surface — Document 2 section 10.

Route names there are proposals, and the document requires inspecting existing
registrations first. `/api/v1/shipping-company` collides with nothing: the
existing prefixes are agents, aggregators, crew, drivers, vessels, incidents,
superadmin and so on.

The document also says "Company APIs must not expose existing operational Agent
API DTOs". Nothing here imports one. The response models are defined in this
module and carry only what a company is entitled to -- a company is not a port
agent and must never receive an agent's payload by accident.

What is here is the claim loop: who am I, submit a claim, see my fleet. The
eight data sections need aggregates, which need the metric source dictionary in
section 22, and are deliberately not stubbed -- an endpoint returning an empty
list reads as "no data" rather than "not built".
"""

from app.services.collection_pages import page_all

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.models.shipping_company import (
    CompanyAuditEvent,
    CompanyUserMembership,
    ShippingCompany,
    VesselRoleClaim,
)
from app.db.models.user import User
from app.db.session import get_db
from app.services import company_claims as claims
from app.services.company_claims import ClaimConflict, ClaimError
from app.services import company_aggregates as aggregates
from app.services.company_metrics import (
    Availability,
    SECTIONS,
    SECTIONS_WITHOUT_METRICS,
    metrics_for,
)
from app.services.company_roles import COMPANY_ROLES, UnknownCompanyRole, validate_role
from app.api.v1.routes_auth import get_current_user

router = APIRouter()

# Owner answer to Q6: the same eight sections for all three company roles.
# "Architecture should support future role-specific policies without
# introducing them now", so this is a constant rather than a per-role lookup.
COMPANY_SECTIONS = (
    "overview", "vessels_and_port_calls", "crew_shore_leave", "crew_movements",
    "safety_and_incident_oversight", "welfare_intelligence",
    "reports_and_analytics", "settings_and_access",
)


# --- responses ---------------------------------------------------------------

class CompanyRoleOut(BaseModel):
    code: str
    label: str
    evidence: str


class CompanyMeOut(BaseModel):
    company_id: int
    legal_name: str
    status: str
    membership_role: str
    permitted_sections: List[str]
    claimable_roles: List[CompanyRoleOut]


class FleetClaimOut(BaseModel):
    """What a company may see about its own claim.

    Note what is absent: nothing about any other company. A claim blocked by a
    competitor shows a generic status, which is section 21's rule -- "never
    competitor identity, evidence or effective dates".
    """

    id: int
    vessel_id: int
    imo_number: Optional[str] = None
    role: str
    effective_start: datetime
    effective_end: Optional[datetime] = None
    status: str
    submitted_at: datetime
    # Only ever the company's own review note, never a competitor's detail.
    review_note: Optional[str] = None


class ClaimSubmissionIn(BaseModel):
    vessel_id: int = Field(gt=0)
    role: str
    effective_start: datetime
    effective_end: Optional[datetime] = None
    imo_number: Optional[str] = None
    evidence_type: Optional[str] = None
    evidence_reference: Optional[str] = None


# --- who is asking -----------------------------------------------------------

def current_company_member(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The company this user may act for, or 403.

    Membership is not a `User.role`: a person is a member of a company through
    CompanyUserMembership, and the same person could in principle be a member
    of none. Both halves are checked here so no endpoint has to remember.
    """
    membership = (
        db.query(CompanyUserMembership)
        .filter(
            CompanyUserMembership.user_id == current_user.id,
            CompanyUserMembership.enabled.is_(True),
        )
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=403, detail="Not a shipping company member")

    company = (
        db.query(ShippingCompany)
        .filter(ShippingCompany.id == membership.company_id)
        .first()
    )
    if company is None or company.status != "VERIFIED":
        # Same 403 either way. Saying "your company is suspended" to someone
        # who is not a member at all would confirm the company exists.
        raise HTTPException(status_code=403, detail="Not a shipping company member")
    return company, membership


def _require_company_admin(membership: CompanyUserMembership) -> None:
    if membership.role != "ADMIN":
        raise HTTPException(
            status_code=403, detail="Only a company administrator may do this"
        )


def _serialize(claim: VesselRoleClaim) -> FleetClaimOut:
    return FleetClaimOut(
        id=claim.id, vessel_id=claim.vessel_id, imo_number=claim.imo_number,
        role=claim.role, effective_start=claim.effective_start,
        effective_end=claim.effective_end, status=claim.status,
        submitted_at=claim.created_at, review_note=claim.review_reason,
    )


# --- endpoints ---------------------------------------------------------------

@router.get("/me", response_model=CompanyMeOut)
def company_me(context=Depends(current_company_member)):
    """Verified profile, this member's role, and the sections they may open."""
    company, membership = context
    return CompanyMeOut(
        company_id=company.id,
        legal_name=company.legal_name,
        status=company.status,
        membership_role=membership.role,
        permitted_sections=list(COMPANY_SECTIONS),
        claimable_roles=[
            CompanyRoleOut(code=r.code, label=r.label, evidence=r.evidence)
            for r in COMPANY_ROLES
        ],
    )


@router.post("/fleet/claims", response_model=FleetClaimOut, status_code=201)
def submit_fleet_claim(
    body: ClaimSubmissionIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    context=Depends(current_company_member),
):
    """Submit a claim. It is never approved by submitting it.

    Section 21 prohibits auto-approval for a non-conflicting IMO, so this
    returns a SUBMITTED claim and nothing else happens until a Super Admin
    reviews it.
    """
    company, membership = context
    _require_company_admin(membership)

    try:
        validate_role(body.role)
    except UnknownCompanyRole as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    try:
        claim = claims.submit_claim(
            db, company_id=company.id, vessel_id=body.vessel_id, role=body.role,
            effective_start=body.effective_start, effective_end=body.effective_end,
            submitted_by=current_user, imo_number=body.imo_number,
            evidence_type=body.evidence_type,
            evidence_reference=body.evidence_reference,
        )
    except ClaimError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    db.commit()
    db.refresh(claim)
    return _serialize(claim)


@router.get("/fleet", response_model=List[FleetClaimOut])
def company_fleet(
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    """This company's claims, approved and pending. Nobody else's.

    Scoped by company_id, so a competitor's claim on the same vessel is not
    merely hidden from the response -- it is never selected.
    """
    company, _ = context
    rows = page_all(
        db.query(VesselRoleClaim)
        .filter(VesselRoleClaim.company_id == company.id)
        .order_by(VesselRoleClaim.vessel_id, VesselRoleClaim.effective_start),
        collection="routes_shipping_company.company_fleet.0",
        label="Company fleet",
    )
    return [_serialize(claim) for claim in rows]


# --- the data sections -------------------------------------------------------
#
# Document 2 section 10 lists overview, shore-leave, movements, safety and
# welfare as separate endpoint families. They are the same shape: a set of
# figures from the metric dictionary, each carrying its caveat and its
# availability, scoped by the policy before anything is aggregated.
#
# One shape rather than five hand-written handlers, because the thing that must
# never vary between them is exactly that scoping.


class FigureOut(BaseModel):
    """One number, with everything a screen needs to show it honestly.

    `display` is the string to render. It is "Not available" when no source
    exists and "Withheld" when the cohort is under the suppression threshold,
    which are different answers from each other and from "0" -- and a screen
    that renders `value` instead will show a blank for two of the three.
    """

    key: str
    label: str
    display: str
    value: Optional[int] = None
    available: bool
    suppressed: bool
    caveat: str
    # So a renderer can style a caveated figure differently without parsing it.
    needs_caveat: bool
    source_version: str
    event_time: str
    aggregation_grain: str
    suppression_threshold: int


class SectionOut(BaseModel):
    section: str
    figures: List[FigureOut]
    # True when every figure in the section is unavailable. The screen must say
    # so rather than draw an empty chart -- Gate G6's no-go is invented
    # movement and welfare metrics, and an empty chart is the easiest way in.
    nothing_to_show: bool


def _figure_out(key: str, figure) -> FigureOut:
    return FigureOut(
        key=key,
        label=figure.metric.label,
        display=figure.display,
        value=figure.value,
        available=figure.available,
        suppressed=figure.suppressed,
        caveat=figure.caveat,
        needs_caveat=figure.metric.availability == Availability.LABELLED,
        source_version=figure.metric.source_version,
        event_time=figure.metric.event_time,
        aggregation_grain=figure.metric.aggregation_grain,
        suppression_threshold=figure.metric.suppression_threshold,
    )


def _section(
    db: Session, company_id: int, section: str,
    period: Optional[aggregates.Period] = None,
) -> SectionOut:
    from datetime import datetime, timezone

    figures: List[FigureOut] = []
    ashore_snapshot = None
    for definition in metrics_for(section):
        computer = aggregates.COMPUTABLE_FIGURES.get(definition.key)
        if computer is None:
            # No source. Reported as unavailable rather than omitted: a missing
            # row reads as "nothing happened", which is a different claim.
            figures.append(_figure_out(
                definition.key,
                aggregates.Figure(metric=definition, value=None, available=False),
            ))
            continue
        if definition.key in ("estimated_ashore_seats", "ashore_uncertain_seats"):
            if ashore_snapshot is None:
                ashore_snapshot = aggregates._ashore_snapshot(db, company_id, period)
            index = 0 if definition.key == "estimated_ashore_seats" else 1
            figures.append(
                _figure_out(
                    definition.key,
                    aggregates.Figure(definition, ashore_snapshot[index]),
                )
            )
            continue
        kwargs = {}
        if definition.key == "verified_vessels":
            # An instant, not a period: "how many vessels do you hold" has no
            # meaning over a range. In a report it is the period's end, so an
            # annual report answers it as at the close of the year.
            from datetime import timedelta

            kwargs["at"] = (
                min(period.end - timedelta(microseconds=1), datetime.now(timezone.utc))
                if period
                else datetime.now(timezone.utc)
            )
        else:
            kwargs["period"] = period
        figures.append(_figure_out(
            definition.key, computer(db, company_id=company_id, **kwargs)))

    return SectionOut(
        section=section, figures=figures,
        nothing_to_show=bool(figures) and not any(f.available for f in figures),
    )


def _section_endpoint(section: str):
    def handler(db: Session = Depends(get_db), context=Depends(current_company_member)):
        company, _ = context
        return _section(db, company.id, section)
    return handler


for _name, _section_key in (
    ("overview", "overview"),
    ("shore-leave", "crew_shore_leave"),
    ("movements", "crew_movements"),
    ("safety", "safety_and_incident_oversight"),
    ("welfare", "welfare_intelligence"),
):
    router.add_api_route(
        f"/{_name}", _section_endpoint(_section_key),
        methods=["GET"], response_model=SectionOut, name=f"company_{_name}",
    )


# --- vessels and their calls -------------------------------------------------

class CompanyCallOut(BaseModel):
    vessel_call_id: int
    vessel_name: str
    imo_number: Optional[str] = None
    port_name: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # Section 10 asks for "event-time coverage flags": whether this company
    # held the vessel for the whole call or only part of it.
    fully_within_tenure: bool


@router.get("/vessels/{imo_number}/calls", response_model=List[CompanyCallOut])
def vessel_calls(
    imo_number: str,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    """Calls on one vessel, for the periods this company held it.

    A guessed IMO returns an empty list rather than 404: a 404 for an unheld
    vessel and a 404 for a non-existent one would differ, and the difference
    tells a company which IMOs are on the platform (TEN-11).
    """
    from app.db.models.vessel import Vessel
    from app.db.models.vessel_call import VesselCall

    company, _ = context
    vessel = (
        db.query(Vessel)
        .filter(func.upper(func.trim(Vessel.imo_number))
                == (imo_number or "").strip().upper())
        .first()
    )
    if vessel is None:
        return []

    claims_held = (
        db.query(VesselRoleClaim)
        .filter(
            VesselRoleClaim.company_id == company.id,
            VesselRoleClaim.vessel_id == vessel.id,
            VesselRoleClaim.status == "APPROVED",
            VesselRoleClaim.revoked_at.is_(None),
        )
        .all()
    )
    if not claims_held:
        return []

    out: List[CompanyCallOut] = []
    from sqlalchemy import and_, or_

    overlap = or_(
        *[
            and_(
                c.effective_start < func.coalesce(VesselCall.ended_at, func.now()),
                (
                    True
                    if c.effective_end is None
                    else VesselCall.started_at < c.effective_end
                ),
            )
            for c in claims_held
        ]
    )
    for call in page_all(
        db.query(VesselCall)
        .filter(VesselCall.vessel_id == vessel.id, overlap)
        .order_by(VesselCall.started_at.desc()),
        collection="company_calls",
        label="Port calls",
    ):
        start, end = call.started_at, call.ended_at
        if start is None:
            continue
        covering = [
            c
            for c in claims_held
            if (end is None or c.effective_start < end)
            and (c.effective_end is None or c.effective_end > start)
        ]
        if not covering:
            continue
        from app.services.company_claims import claim_intervals_cover

        whole = claim_intervals_cover(covering, start, end)
        out.append(
            CompanyCallOut(
                vessel_call_id=call.id,
                vessel_name=call.vessel_name,
                imo_number=call.imo_number,
                port_name=call.port_name,
                started_at=start,
                ended_at=end,
                fully_within_tenure=whole,
            )
        )
    return out


# --- members -----------------------------------------------------------------

class MemberOut(BaseModel):
    user_id: int
    email: str
    role: str
    enabled: bool


class MemberIn(BaseModel):
    email: str
    role: str = Field(default="VIEWER")


@router.get("/members", response_model=List[MemberOut])
def list_members(db: Session = Depends(get_db), context=Depends(current_company_member)):
    company, membership = context
    _require_company_admin(membership)
    rows = page_all(
        db.query(CompanyUserMembership, User.email)
        .join(User, User.id == CompanyUserMembership.user_id)
        .filter(CompanyUserMembership.company_id == company.id)
        .order_by(User.email),
        collection="routes_shipping_company.list_members.0",
        label="Members",
    )
    return [
        MemberOut(user_id=m.user_id, email=email, role=m.role, enabled=m.enabled)
        for m, email in rows
    ]


@router.post("/members", response_model=MemberOut, status_code=201)
def add_member(
    body: MemberIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    current_user: User = Depends(get_current_user),
):
    """Invite an existing account to this company.

    Section 21: "Invitation cannot attach an unrelated email to company without
    verification." So the account must already exist -- this attaches a known
    user rather than creating one from an address anyone could type.
    """
    company, membership = context
    from app.api.v1.routes_company_staff import lock_admin

    lock_admin(db, company, membership)
    if body.role not in ("ADMIN", "ANALYST", "VIEWER"):
        raise HTTPException(
            status_code=400, detail="role must be ADMIN, ANALYST or VIEWER"
        )

    user = (
        db.query(User)
        .filter(User.email == body.email.strip().lower())
        .with_for_update()
        .first()
    )
    if user is None:
        raise HTTPException(
            status_code=404,
            detail="No account with that address. The person must register first.",
        )
    if (
        db.query(CompanyUserMembership)
        .filter(
            CompanyUserMembership.user_id == user.id,
            CompanyUserMembership.enabled.is_(True),
            CompanyUserMembership.company_id != company.id,
        )
        .first()
        is not None
    ):
        raise HTTPException(409, "Account already belongs to another company")
    existing = (
        db.query(CompanyUserMembership)
        .filter(
            CompanyUserMembership.company_id == company.id,
            CompanyUserMembership.user_id == user.id,
        )
        .first()
    )
    if existing is not None:
        from app.api.v1.routes_company_staff import change_member

        change_member(db, company, current_user, existing, body.role, True)
        db.commit()
        return MemberOut(
            user_id=user.id,
            email=user.email,
            role=existing.role,
            enabled=existing.enabled,
        )

    created = CompanyUserMembership(
        company_id=company.id, user_id=user.id, role=body.role,
        enabled=True, invited_by_user_id=current_user.id,
    )
    db.add(created)
    db.commit()
    return MemberOut(user_id=user.id, email=user.email, role=created.role, enabled=True)


# --- fleet CSV preview -------------------------------------------------------

class FleetRowIn(BaseModel):
    imo_number: str
    role: str
    effective_start: Optional[datetime] = None
    effective_end: Optional[datetime] = None


class FleetPreviewIn(BaseModel):
    rows: List[FleetRowIn] = Field(max_length=500)
    filename: Optional[str] = None


class FleetRowFinding(BaseModel):
    row: int
    imo_number: str
    ok: bool
    problem: Optional[str] = None
    vessel_id: Optional[int] = None


class FleetPreviewOut(BaseModel):
    submission_id: int
    total: int
    accepted: int
    findings: List[FleetRowFinding]


@router.post("/fleet/preview", response_model=FleetPreviewOut)
def preview_fleet(
    body: FleetPreviewIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    current_user: User = Depends(get_current_user),
):
    """Validate rows and change no access — Document 2 section 10.

    TEN-15 lists what this must catch: duplicate IMOs, invalid roles,
    malformed dates and same-role conflicts, with row-level review and no
    access granted. Nothing here creates a claim; the findings are recorded
    against a submission so a rejected upload can be explained later without
    re-reading a file nobody stored.

    A conflict with another company is reported as a conflict and nothing more
    -- section 21 again: never competitor identity, evidence or dates.
    """
    from app.db.models.shipping_company import FleetClaimSubmission
    from app.db.models.vessel import Vessel

    company, membership = context
    _require_company_admin(membership)

    findings: List[FleetRowFinding] = []
    seen: dict[str, int] = {}
    for index, row in enumerate(body.rows, start=1):
        imo = (row.imo_number or "").strip().upper()
        problem = None
        vessel_id = None

        if not imo:
            problem = "No IMO number"
        elif imo in seen:
            problem = f"Duplicate IMO, first seen on row {seen[imo]}"
        else:
            seen[imo] = index
            try:
                validate_role(row.role)
            except UnknownCompanyRole as exc:
                problem = str(exc)
            if problem is None:
                if row.effective_start is None:
                    problem = "No effective start date"
                elif row.effective_end is not None and row.effective_end <= row.effective_start:
                    problem = "The end date is not after the start date"
            if problem is None:
                # Compared case-insensitively on both sides. Uppercasing only
                # the input silently failed to find any vessel whose stored
                # IMO carries a lowercase character, which a test caught.
                vessel = (
                    db.query(Vessel)
                    .filter(func.upper(func.trim(Vessel.imo_number)) == imo)
                    .first()
                )
                if vessel is None:
                    problem = "No vessel with that IMO on the platform"
                else:
                    vessel_id = vessel.id
                    clash = (
                        db.query(VesselRoleClaim)
                        .filter(
                            VesselRoleClaim.vessel_id == vessel.id,
                            VesselRoleClaim.role == validate_role(row.role),
                            VesselRoleClaim.status == "APPROVED",
                            VesselRoleClaim.revoked_at.is_(None),
                        )
                        .first()
                    )
                    if clash is not None:
                        problem = (
                            "This vessel and role already has an approved claim "
                            "for part of that period. It will be reviewed."
                        )

        findings.append(FleetRowFinding(
            row=index, imo_number=imo, ok=problem is None,
            problem=problem, vessel_id=vessel_id,
        ))

    submission = FleetClaimSubmission(
        company_id=company.id, submitted_by_user_id=current_user.id,
        source="CSV", state="DRAFT", original_filename=body.filename,
        row_count=len(body.rows),
        findings=[f.model_dump() for f in findings],
    )
    db.add(submission)
    db.commit()
    db.refresh(submission)

    return FleetPreviewOut(
        submission_id=submission.id, total=len(findings),
        accepted=sum(1 for f in findings if f.ok), findings=findings,
    )


# --- sensitive access requests -----------------------------------------------

class AccessRequestIn(BaseModel):
    vessel_call_id: int = Field(gt=0)
    category: str
    requested_start: datetime
    requested_end: datetime
    reason: str = Field(min_length=1)


class AccessRequestOut(BaseModel):
    id: int
    vessel_call_id: int
    category: str
    requested_start: datetime
    requested_end: datetime
    status: str
    decision_reason: Optional[str] = None
    created_at: datetime


@router.post("/access-requests", response_model=AccessRequestOut, status_code=201)
def request_access(
    body: AccessRequestIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    current_user: User = Depends(get_current_user),
):
    """Ask Super Admin for detail on one call. Never self-granting.

    Section 21: "Company admin cannot grant itself detail." So this creates a
    REQUESTED row and nothing else -- no grant, and no change to what the
    policy allows until a Super Admin decides.

    The call must be one the company already holds at the requested time. A
    request for a call it never held is refused the same way a guess at an
    unrelated id is, so the response does not distinguish the two (TEN-11).
    """
    from app.db.models.shipping_company import SensitiveAccessRequest
    from app.db.models.vessel_call import VesselCall
    from app.services.company_access import covering_claim

    company, _ = context
    if body.requested_end <= body.requested_start:
        raise HTTPException(status_code=400, detail="The end must come after the start")

    call = db.query(VesselCall).filter(VesselCall.id == body.vessel_call_id).first()
    held = call is not None and covering_claim(
        db, company_id=company.id, vessel_id=call.vessel_id,
        occurred_at=body.requested_start,
    ) is not None
    if not held:
        raise HTTPException(status_code=404, detail="No such port call")

    row = SensitiveAccessRequest(
        company_id=company.id, vessel_call_id=body.vessel_call_id,
        category=body.category, requested_start=body.requested_start,
        requested_end=body.requested_end, reason=body.reason,
        requested_by_user_id=current_user.id, status="REQUESTED",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return AccessRequestOut.model_validate(row, from_attributes=True)


@router.get("/access-requests", response_model=List[AccessRequestOut])
def my_access_requests(
    db: Session = Depends(get_db), context=Depends(current_company_member),
):
    from app.db.models.shipping_company import SensitiveAccessRequest

    company, _ = context
    rows = page_all(
        db.query(SensitiveAccessRequest)
        .filter(SensitiveAccessRequest.company_id == company.id)
        .order_by(SensitiveAccessRequest.created_at.desc()),
        collection="routes_shipping_company.my_access_requests.0",
        label="My access requests",
    )
    return [AccessRequestOut.model_validate(r, from_attributes=True) for r in rows]


# --- registration and verification -------------------------------------------
#
# Everything above requires a VERIFIED company, which left no way to become
# one: nothing in the application created a `ShippingCompany`, and
# `app/services/company_verification.py` had no caller. These are the routes
# before verification, so they use a dependency that accepts any status.
#
# Section 21's prohibition is kept where it belongs. A company may *ask* to be
# verified, which is why `submit_for_verification` takes the company's own
# actor; deciding happens only in routes_company_review.py, under Super Admin.


class RegistrationIn(BaseModel):
    legal_name: str = Field(min_length=2, max_length=255)


class RegistrationOut(BaseModel):
    company_id: int
    legal_name: str
    status: str
    membership_role: str
    # What the company has to do next, in its own words, so the screen does not
    # have to encode the state machine a second time.
    next_step: str


def _normalized(name: str) -> str:
    """Collapse whitespace and case, the way crew identity normalises a passport."""
    return " ".join(str(name).split()).upper()


_NEXT_STEP = {
    "DRAFT": "Submit the company for verification.",
    "PENDING_VERIFICATION": "Waiting for a HeyPorts administrator to review it.",
    "VERIFIED": "Nothing — the dashboard is open.",
    "REJECTED": "This registration was refused. Contact HeyPorts.",
    "SUSPENDED": "Access is suspended. Contact HeyPorts.",
    "CLOSED": "This company is closed.",
}


def current_company_any_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The user's company whatever its status, or 404.

    Deliberately separate from `current_company_member`, which refuses
    anything but VERIFIED. A company that is still DRAFT needs to see that it
    is DRAFT; it must not reach a single data endpoint.
    """
    membership = (
        db.query(CompanyUserMembership)
        .filter(
            CompanyUserMembership.user_id == current_user.id,
            CompanyUserMembership.enabled.is_(True),
        )
        .first()
    )
    if membership is None:
        raise HTTPException(status_code=404, detail="No company registration found")
    company = (
        db.query(ShippingCompany)
        .filter(ShippingCompany.id == membership.company_id)
        .first()
    )
    if company is None:
        raise HTTPException(status_code=404, detail="No company registration found")
    return company, membership


def _registration_out(company, membership) -> RegistrationOut:
    return RegistrationOut(
        company_id=company.id, legal_name=company.legal_name,
        status=company.status, membership_role=membership.role,
        next_step=_NEXT_STEP.get(company.status, ""),
    )


@router.get("/registration", response_model=RegistrationOut)
def my_registration(context=Depends(current_company_any_status)):
    """Where this user's company stands, including before it is verified."""
    company, membership = context
    return _registration_out(company, membership)


@router.post("/registration", response_model=RegistrationOut, status_code=201)
def register_company(
    body: RegistrationIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a DRAFT company and make this user its administrator.

    DRAFT, never VERIFIED: the status a self-registration may reach is the
    whole point of section 21's prohibition. The dashboard stays closed until
    a Super Admin decides.
    """
    existing = (
        db.query(CompanyUserMembership)
        .filter(
            CompanyUserMembership.user_id == current_user.id,
            CompanyUserMembership.enabled.is_(True),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="This account already belongs to a shipping company",
        )

    normalized = _normalized(body.legal_name)
    if not normalized:
        raise HTTPException(status_code=400, detail="Enter the company's legal name")
    clash = (
        db.query(ShippingCompany.id)
        .filter(ShippingCompany.normalized_name == normalized)
        .first()
    )
    if clash is not None:
        # Says that the name is taken, not who holds it. A company's existence
        # on the platform is not a fact a stranger gets to enumerate.
        raise HTTPException(
            status_code=409,
            detail="A company is already registered under this name. "
                   "If it is yours, ask its administrator to add you.",
        )

    company = ShippingCompany(
        legal_name=body.legal_name.strip(), normalized_name=normalized, status="DRAFT",
    )
    db.add(company)
    db.flush()
    membership = CompanyUserMembership(
        company_id=company.id, user_id=current_user.id, role="ADMIN", enabled=True,
    )
    db.add(membership)
    db.add(CompanyAuditEvent(
        actor_user_id=current_user.id, company_id=company.id,
        target_type="shipping_company", target_id=company.id,
        action="registered", before_state=None,
        after_state={"status": "DRAFT", "legal_name": company.legal_name},
    ))
    db.commit()
    db.refresh(company)
    return _registration_out(company, membership)


@router.post("/registration/submit", response_model=RegistrationOut)
def submit_for_verification(
    db: Session = Depends(get_db),
    context=Depends(current_company_any_status),
    current_user: User = Depends(get_current_user),
):
    """Ask to be verified. A request, never a decision."""
    from app.services import company_verification as verification

    company, membership = context
    _require_company_admin(membership)
    try:
        verification.submit_for_verification(db, company=company, actor=current_user)
    except verification.VerificationError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(company)
    return _registration_out(company, membership)


# --- reports and exports -----------------------------------------------------
#
# Document 2 lists `reports_and_analytics` as "a rendering of the others", and
# that is exactly what this is: the same figures, over a period, through the
# same policy. What it is *not* is a cache.
#
# TEN-07 is the reason. A snapshot taken while a company held a vessel until
# December keeps answering with December's data after the claim is
# retroactively shortened to June — the company's entitlement moved and the
# stored bytes did not. The agent reports snapshot deliberately, because an
# agent's report is about a port call that is over. A company's entitlement is
# a live interval that a Super Admin can shorten or revoke, so every report
# here is computed when it is asked for and nothing is stored.
#
# That also settles TEN-14: an export is this same computation in another
# format, so a revoked claim stops appearing in it on the next request rather
# than whenever a cache was due to expire.

class ReportFigureOut(FigureOut):
    section: str


class ReportOut(BaseModel):
    company_id: int
    period_start: datetime
    period_end: datetime
    generated_at: datetime
    # Named so a reader cannot mistake this for a stored document.
    computed_on_read: bool = True
    figures: List[ReportFigureOut]


def _as_utc(value: datetime) -> datetime:
    """A naive boundary means UTC, so a report cannot shift by the server's zone."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _report_period(period_start: datetime, period_end: datetime) -> aggregates.Period:
    start, end = _as_utc(period_start), _as_utc(period_end)
    if end <= start:
        raise HTTPException(
            status_code=422,
            detail="The report period must end after it starts.",
        )
    return aggregates.Period(start=start, end=end)


def _report_figures(db: Session, company_id: int, period: aggregates.Period):
    figures: List[ReportFigureOut] = []
    for section in SECTIONS:
        if section in SECTIONS_WITHOUT_METRICS:
            continue
        for figure in _section(db, company_id, section, period=period).figures:
            figures.append(ReportFigureOut(section=section, **figure.model_dump()))
    return figures


@router.get("/reports", response_model=ReportOut)
def company_report(
    period_start: datetime,
    period_end: datetime,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    """Every section's figures for one period, computed now.

    The period narrows the rows **in addition to** tenure, never instead of
    it: a company that held a vessel for one month of the year gets that
    month's events from an annual report, not the year's.
    """
    company, _ = context
    period = _report_period(period_start, period_end)
    return ReportOut(
        company_id=company.id,
        period_start=period.start, period_end=period.end,
        generated_at=datetime.now(timezone.utc),
        figures=_report_figures(db, company.id, period),
    )


@router.get("/reports/export")
def company_report_export(
    period_start: datetime,
    period_end: datetime,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    """The same report as CSV, recomputed for this request.

    Every row carries the figure's own availability and suppression, because a
    spreadsheet that writes a blank for "no source exists" and a blank for
    "withheld to protect a small cohort" has merged two different answers into
    one, and a reader will treat both as zero.
    """
    import csv
    import io as _io

    from fastapi import Response

    company, _ = context
    period = _report_period(period_start, period_end)

    buffer = _io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "section",
            "metric",
            "label",
            "value",
            "state",
            "caveat",
            "source_version",
            "event_time",
            "aggregation_grain",
            "suppression_threshold",
        ]
    )
    for figure in _report_figures(db, company.id, period):
        state = (
            "withheld" if figure.suppressed
            else "no_source" if not figure.available
            else "counted"
        )
        writer.writerow(
            [
                figure.section,
                figure.key,
                figure.label,
                "" if figure.value is None else figure.value,
                state,
                figure.caveat,
                figure.source_version,
                figure.event_time,
                figure.aggregation_grain,
                figure.suppression_threshold,
            ]
        )

    filename = (
        f"heyports-{company.id}-"
        f"{period.start.date().isoformat()}-to-{period.end.date().isoformat()}.csv"
    )
    # A plain Response rather than StreamingResponse: the whole report is
    # already in memory by the time the first byte could be sent, so streaming
    # would only make it harder to read back.
    return Response(
        content=buffer.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )
