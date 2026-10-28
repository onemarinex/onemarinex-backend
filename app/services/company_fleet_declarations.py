"""Company declarations grant aggregates only. Independent approval stays separate."""

from __future__ import annotations
from typing import Optional
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4
from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import or_, func, case
from sqlalchemy.exc import IntegrityError
from app.db.models.vessel import Vessel
from app.db.models.shipping_company import VesselRoleClaim, CompanyAuditEvent
from app.db.fleet_schema import require_fleet_constraint
from app.services.company_claims import _interval

ACCEPTED = ("APPROVED", "AUTO_ACCEPTED")
OPEN = ACCEPTED + ("SUBMITTED", "UNDER_REVIEW", "MORE_EVIDENCE")
ROLE_FIELDS = {
    "owner": "REGISTERED_OWNER",
    "commercial_manager": "COMMERCIAL_MANAGER",
    "ism_manager": "ISM_MANAGER",
}
FIELDS = ("imo_number", "vessel_name", *ROLE_FIELDS, "start_date", "end_date")


def imo(value):
    value = str(value or "").strip().upper().removeprefix("IMO").strip()
    if (
        not value.isascii()
        or not value.isdigit()
        or len(value) != 7
        or sum(int(value[i]) * (7 - i) for i in range(6)) % 10 != int(value[-1])
    ):
        raise ValueError("Use a seven-digit IMO with a valid check digit.")
    return value


def boundary(value):
    if value in (None, ""):
        return None
    if isinstance(value, str) and len(value.strip()) == 10:
        return datetime.combine(
            date.fromisoformat(value), time(), ZoneInfo("Asia/Kolkata")
        ).astimezone(timezone.utc)
    moment = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if moment.tzinfo is None:
        raise ValueError("Date-time values must include a time zone.")
    return moment.astimezone(timezone.utc)


class DeclarationIn(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    imo_number: str = Field(max_length=32)
    vessel_name: str = Field(min_length=1, max_length=255)
    owner: bool
    commercial_manager: bool
    ism_manager: bool
    start_date: str = Field(max_length=40)
    end_date: Optional[str] = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def validate_declaration(self):
        self.imo_number = imo(self.imo_number)
        start, end = boundary(self.start_date), boundary(self.end_date)
        if start is None or (end is not None and end <= start):
            raise ValueError("The end must be after the start.")
        if not any(getattr(self, field) for field in ROLE_FIELDS):
            raise ValueError("Select at least one company role.")
        return self

    def roles(self):
        return [code for field, code in ROLE_FIELDS.items() if getattr(self, field)]


def resolve(db, number, lock=False):
    # Ambiguous legacy identities must be reviewed; never pick an arbitrary row.
    q = db.query(Vessel).filter(
        func.upper(func.trim(Vessel.imo_number)).in_([number, "IMO " + number])
    )
    if lock:
        q = q.order_by(Vessel.id).with_for_update()
    rows = q.limit(2).all()
    if len(rows) != 1:
        raise ValueError(
            "Unknown or ambiguous IMO. Ask Super Admin to check the vessel master."
        )
    return rows[0]


def overlapping(db, vessel_id, roles, start, end):
    q = db.query(VesselRoleClaim).filter(
        VesselRoleClaim.vessel_id == vessel_id,
        VesselRoleClaim.role.in_(roles),
        VesselRoleClaim.status.in_(OPEN),
        VesselRoleClaim.revoked_at.is_(None),
        or_(
            VesselRoleClaim.effective_end.is_(None),
            VesselRoleClaim.effective_end > start,
        ),
    )
    return q.filter(VesselRoleClaim.effective_start < end) if end else q


def inspect_row(db, company_id, row):
    vessel = resolve(db, row.imo_number)
    if (
        " ".join(vessel.name.split()).casefold()
        != " ".join(row.vessel_name.split()).casefold()
    ):
        raise ValueError(
            "The name does not match the vessel master. Use the master name or request a name correction."
        )
    overlaps = overlapping(
        db, vessel.id, row.roles(), boundary(row.start_date), boundary(row.end_date)
    ).all()
    if any(c.company_id == company_id for c in overlaps):
        return vessel, "DUPLICATE", set()
    conflicts = {c.role for c in overlaps}
    return vessel, "CONFLICT" if conflicts else "ACCEPTED", conflicts


def audit(
    db,
    actor,
    company_id,
    target_id,
    action,
    before=None,
    after=None,
    reason=None,
    target_type="fleet_declaration",
):
    db.add(
        CompanyAuditEvent(
            actor_user_id=actor.id,
            company_id=company_id,
            target_type=target_type,
            target_id=target_id,
            action=action,
            before_state=before,
            after_state=after,
            reason=reason,
        )
    )


def declare(db, company, actor, row, *, import_id=None):
    require_fleet_constraint(db)
    if company.status != "VERIFIED":
        raise HTTPException(403, "Only a verified company can declare a fleet.")
    resolve(db, row.imo_number, lock=True)
    vessel, state, conflicts = inspect_row(db, company.id, row)
    if state == "DUPLICATE":
        raise ValueError("Your fleet already has this role in an overlapping period.")
    group = str(uuid4())
    result = []
    for role in row.roles():
        claim = VesselRoleClaim(
            company_id=company.id,
            vessel_id=vessel.id,
            imo_number=row.imo_number,
            role=role,
            effective_start=boundary(row.start_date),
            effective_end=boundary(row.end_date),
            status="SUBMITTED" if role in conflicts else "AUTO_ACCEPTED",
            fleet_group=group,
            declared_vessel_name=vessel.name,
            fleet_import_id=import_id,
            evidence_type="company_declaration",
            evidence_reference=f"Fleet declaration by member {actor.id}",
        )
        # The DB exclusion is the final authority even against legacy SA writes.
        try:
            with db.begin_nested():
                db.add(claim)
                db.flush()
        except IntegrityError:
            claim = VesselRoleClaim(
                company_id=company.id,
                vessel_id=vessel.id,
                imo_number=row.imo_number,
                role=role,
                effective_start=boundary(row.start_date),
                effective_end=boundary(row.end_date),
                status="SUBMITTED",
                fleet_group=group,
                declared_vessel_name=vessel.name,
                fleet_import_id=import_id,
                evidence_type="company_declaration",
                evidence_reference=f"Fleet declaration by member {actor.id}",
            )
            db.add(claim)
            db.flush()
        audit(
            db,
            actor,
            company.id,
            claim.id,
            (
                "aggregate_declaration_accepted"
                if claim.status == "AUTO_ACCEPTED"
                else "declaration_pending_review"
            ),
            after=_interval(claim),
            reason="Company declaration. No independent verification or restricted access is granted.",
        )
        result.append(claim)
    return result


def group_query(db, company_id):
    from sqlalchemy import String, cast

    group = func.concat(
        VesselRoleClaim.vessel_id,
        "/",
        cast(VesselRoleClaim.effective_start, String),
        "/",
        func.coalesce(cast(VesselRoleClaim.effective_end, String), "ongoing"),
        "/",
        case((VesselRoleClaim.status.in_(OPEN), "open"), else_=VesselRoleClaim.status),
        "/",
        case((VesselRoleClaim.revoked_at.is_(None), "live"), else_="revoked"),
    )
    return (
        db.query(VesselRoleClaim).filter(VesselRoleClaim.company_id == company_id),
        group,
    )


def load_group(db, company_id, entry_id, *, lock=False):
    q, group = group_query(db, company_id)
    # Resolve the group from a stable claim id; company scope applies to both reads.
    first = q.filter(VesselRoleClaim.id == entry_id).first()
    if first is None:
        raise HTTPException(404, "Fleet entry not found.")
    key = q.with_entities(group).filter(VesselRoleClaim.id == entry_id).scalar()
    query = q.filter(group == key).order_by(VesselRoleClaim.id).populate_existing()
    rows = (query.with_for_update() if lock else query).all()
    return rows


def revision(rows):
    import hashlib, json

    return hashlib.sha256(
        json.dumps(
            [dict(id=c.id, **_interval(c)) for c in rows], sort_keys=True
        ).encode()
    ).hexdigest()


def entry_out(db, rows):
    c = rows[0]
    vessel = db.get(Vessel, c.vessel_id)
    now = datetime.now(timezone.utc)
    current = any(
        r.status in ACCEPTED
        and r.revoked_at is None
        and r.effective_start <= now
        and (r.effective_end is None or now < r.effective_end)
        for r in rows
    )
    return dict(
        id=c.id,
        vessel_id=c.vessel_id,
        imo_number=c.imo_number or vessel.imo_number,
        vessel_name=vessel.name if current else c.declared_vessel_name or vessel.name,
        master_name=vessel.name,
        effective_start=c.effective_start,
        effective_end=c.effective_end,
        **{
            field: any(r.role == code for r in rows)
            for field, code in ROLE_FIELDS.items()
        },
        accepted_roles=[
            r.role for r in rows if r.status in ACCEPTED and not r.revoked_at
        ],
        statuses=sorted({r.status for r in rows}),
        active=current,
        acceptance=(
            "Pending review"
            if any(r.status in OPEN and r.status not in ACCEPTED for r in rows)
            else (
                "Independently approved"
                if all(r.status == "APPROVED" for r in rows)
                else (
                    "Accepted for aggregates"
                    if any(r.status == "AUTO_ACCEPTED" for r in rows)
                    else "Historical record"
                )
            )
        ),
        revision=revision(rows),
        import_id=c.fleet_import_id,
    )
