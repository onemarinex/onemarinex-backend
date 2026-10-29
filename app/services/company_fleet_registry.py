"""Company-owned fleet editing with immutable history and bounded authority.

Registry periods describe aggregate eligibility. A company's edit can retain
an independently approved intersection, but cannot enlarge that approval.
Legacy period-group change requests keep their original API and revision.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal, Optional
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, String, cast
from sqlalchemy.exc import IntegrityError

from app.db.models.company_fleet_change import CompanyFleetChange
from app.db.models.shipping_company import VesselRoleClaim
from app.db.models.vessel import Vessel
from app.services import company_fleet_declarations as fleet
from app.services.company_fleet_identity import identity_key, lock_identity


class RegistryEdit(BaseModel):
    model_config = {"str_strip_whitespace": True, "extra": "forbid"}
    revision: str = Field(min_length=64, max_length=64)
    action: Literal["UPDATE", "REMOVE", "CORRECT"] = "UPDATE"
    change_kind: Literal["CORRECTION", "CHANGE"] = "CORRECTION"
    effective_date: Optional[str] = Field(default=None, max_length=40)
    owner: bool = False
    commercial_manager: bool = False
    ism_manager: bool = False
    owner_start_date: Optional[str] = Field(default=None, max_length=40)
    owner_end_date: Optional[str] = Field(default=None, max_length=40)
    commercial_manager_start_date: Optional[str] = Field(default=None, max_length=40)
    commercial_manager_end_date: Optional[str] = Field(default=None, max_length=40)
    ism_manager_start_date: Optional[str] = Field(default=None, max_length=40)
    ism_manager_end_date: Optional[str] = Field(default=None, max_length=40)
    vessel_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    imo_number: Optional[str] = Field(default=None, max_length=32)
    reason: Optional[str] = Field(default=None, max_length=4000)
    evidence_reference: Optional[str] = Field(default=None, max_length=255)


def moment(value):
    return value.replace(tzinfo=timezone.utc) if value is not None and value.tzinfo is None else value


def snapshot(row):
    return {
        "id": row.id,
        "company_id": row.company_id,
        "vessel_id": row.vessel_id,
        "imo_number": row.imo_number,
        "vessel_name": row.declared_vessel_name,
        "role": row.role,
        "status": row.status,
        "review_reason": row.review_reason,
        "evidence_type": row.evidence_type,
        "evidence_reference": row.evidence_reference,
        "effective_start": moment(row.effective_start).isoformat(),
        "effective_end": moment(row.effective_end).isoformat() if row.effective_end else None,
        "revoked_at": moment(row.revoked_at).isoformat() if row.revoked_at else None,
    }


def revision(rows):
    return hashlib.sha256(json.dumps([snapshot(r) for r in rows], sort_keys=True).encode()).hexdigest()


def load(db, company_id, entry_id, *, lock=False):
    q = db.query(VesselRoleClaim).filter(VesselRoleClaim.company_id == company_id)
    key = q.with_entities(identity_key()).filter(VesselRoleClaim.id == entry_id).scalar()
    if key is None:
        raise HTTPException(404, "Fleet entry not found.")
    if lock:
        lock_identity(db, key)
    q = q.filter(identity_key() == key).order_by(VesselRoleClaim.id).populate_existing()
    rows = (q.with_for_update() if lock else q).all()
    if not rows:
        raise HTTPException(404, "Fleet entry not found.")
    return key, rows


def selected_periods(rows):
    """Latest connected interval per role; prior separate periods stay intact."""
    selected = {}
    for role in fleet.ROLE_FIELDS.values():
        candidates = sorted(
            (r for r in rows if r.role == role and r.status in fleet.OPEN and not r.revoked_at),
            key=lambda r: (moment(r.effective_start), r.id),
        )
        clusters = []
        for row in candidates:
            if not clusters or (
                clusters[-1][1] is not None and moment(row.effective_start) > clusters[-1][1]
            ):
                clusters.append(([row], moment(row.effective_end)))
            else:
                group, end = clusters[-1]
                group.append(row)
                clusters[-1] = (group, None if end is None or row.effective_end is None else max(end, moment(row.effective_end)))
        if clusters:
            selected[role] = clusters[-1][0]
    return selected


def current(row, now):
    return row.status in fleet.ACCEPTED and not row.revoked_at and moment(row.effective_start) <= now and (
        row.effective_end is None or now < moment(row.effective_end)
    )


def entry_out(db, rows):
    now = datetime.now(timezone.utc)
    selected = selected_periods(rows)
    relevant = [r for group in selected.values() for r in group]
    latest = max(relevant or rows, key=lambda r: r.id)
    vessel = db.get(Vessel, latest.vessel_id) if latest.vessel_id else None
    value = {
        "id": min(r.id for r in rows),
        "vessel_id": latest.vessel_id,
        "vessel_name": latest.declared_vessel_name or (vessel.name if vessel else "Name not recorded"),
        "master_name": vessel.name if vessel else None,
        "imo_number": latest.imo_number or (vessel.imo_number if vessel else None),
        "operational_linked": vessel is not None,
        "effective_start": min(r.effective_start for r in relevant or rows),
        "effective_end": None if any(r.effective_end is None for r in relevant or rows) else max(r.effective_end for r in relevant or rows),
        "active": any(current(r, now) for r in rows),
        "accepted_roles": sorted({r.role for r in relevant if r.status in fleet.ACCEPTED}),
        "statuses": sorted({r.status for r in relevant or rows}),
        "acceptance": "Pending Conflict Resolution" if any(r.status not in fleet.ACCEPTED for r in relevant) else (
            "Applied" if relevant else "Historical record"
        ),
        "revision": revision(rows),
        "role_periods": [snapshot(r) for r in rows],
    }
    for field, role in fleet.ROLE_FIELDS.items():
        group = selected.get(role, [])
        value[field] = bool(group)
        value[field + "_start_date"] = min((r.effective_start for r in group), default=None)
        value[field + "_end_date"] = None if not group or any(r.effective_end is None for r in group) else max(r.effective_end for r in group)
    return value


def registry_page(db, company_id, search="", view="active", role=None, sort="name", descending=False, offset=0, limit=25):
    q = db.query(VesselRoleClaim).filter(VesselRoleClaim.company_id == company_id)
    now = datetime.now(timezone.utc)
    active = and_(VesselRoleClaim.status.in_(fleet.ACCEPTED), VesselRoleClaim.revoked_at.is_(None),
                  VesselRoleClaim.effective_start <= now,
                  or_(VesselRoleClaim.effective_end.is_(None), VesselRoleClaim.effective_end > now))
    count = q.filter(active).with_entities(func.count(func.distinct(identity_key()))).scalar() or 0
    # Project the correlated identity once before grouping. Do not let a filter
    # discard other roles/periods from a vessel's returned entry.
    records = q.outerjoin(Vessel, Vessel.id == VesselRoleClaim.vessel_id).with_entities(
        VesselRoleClaim.id.label("id"), identity_key().label("identity"),
        func.coalesce(VesselRoleClaim.declared_vessel_name, Vessel.name).label("name"),
        func.coalesce(VesselRoleClaim.imo_number, Vessel.imo_number).label("imo"),
        VesselRoleClaim.role.label("role"),
        VesselRoleClaim.effective_start.label("start"), VesselRoleClaim.effective_end.label("end"),
        active.label("active"),
    ).subquery()
    groups = db.query(records.c.identity, func.min(records.c.id).label("id")).group_by(records.c.identity)
    if view != "all":
        groups = groups.having(func.bool_or(records.c.active) if view == "active" else ~func.bool_or(records.c.active))
    if role:
        groups = groups.having(func.bool_or(and_(records.c.role == role, records.c.active)) if view == "active" else func.bool_or(records.c.role == role))
    if search.strip():
        term = "%" + search.strip().replace("%", "\\%").replace("_", "\\_") + "%"
        groups = groups.having(func.bool_or(or_(records.c.name.ilike(term, escape="\\"), records.c.imo.ilike(term, escape="\\"), records.c.identity.ilike(term, escape="\\"))))
    order = {"name": func.min(records.c.name), "imo": func.min(records.c.imo),
             "start": func.min(records.c.start), "end": func.max(records.c.end)}[sort]
    total = groups.count()
    keys = groups.order_by(order.desc().nullslast() if descending else order.asc().nullslast(), func.min(records.c.id)).offset(offset).limit(limit).all()
    return {"total": total, "offset": offset, "limit": limit, "current_vessels": count,
            "items": [entry_out(db, load(db, company_id, key.id)[1]) for key in keys]}


def _end_min(a, b):
    return b if a is None else a if b is None else min(a, b)


def _overlap(start, end, row):
    return (row.effective_end is None or start < moment(row.effective_end)) and (end is None or moment(row.effective_start) < end)


def _segments(start, end, approved):
    """Partition a requested period into old independent approval and new scope."""
    points = {start}
    if end is not None:
        points.add(end)
    for row in approved:
        if _overlap(start, end, row):
            points.add(max(start, moment(row.effective_start)))
            cutoff = _end_min(end, moment(row.effective_end))
            if cutoff is not None:
                points.add(cutoff)
    points = sorted(points)
    result = []
    for index, left in enumerate(points):
        right = points[index + 1] if index + 1 < len(points) else end
        if right is not None and right <= left:
            continue
        source = next((r for r in approved if moment(r.effective_start) <= left and (r.effective_end is None or left < moment(r.effective_end))), None)
        result.append((left, right, source))
    return result


def _new_claim(db, company_id, actor, identity, name, vessel_id, role, start, end, source=None, status=None, review_reason=None):
    row = VesselRoleClaim(
        company_id=company_id, vessel_id=source.vessel_id if source else vessel_id, imo_number=identity,
        declared_vessel_name=name, fleet_group=str(uuid4()), role=role,
        effective_start=start, effective_end=end,
        status=status or ("APPROVED" if source else "AUTO_ACCEPTED"),
        evidence_type=source.evidence_type if source else "company_declaration",
        evidence_reference=source.evidence_reference if source else f"Fleet change by member {actor.id}",
        approved_by_user_id=source.approved_by_user_id if source else None,
        approved_at=source.approved_at if source else None,
        review_reason=review_reason,
    )
    db.add(row)
    db.flush()
    return row


def _review_request(db, company_id, actor, rows, body, reason):
    if len((body.reason or "").strip()) < 10:
        raise HTTPException(422, "Give a reason of at least ten characters for this contested change.")
    change = CompanyFleetChange(company_id=company_id, claim_id=rows[0].id,
                                requested_by_user_id=actor.id, action="REGISTRY",
                                payload=body.model_dump(), reason=body.reason)
    db.add(change)
    db.flush()
    fleet.audit(db, actor, company_id, change.id, "registry_conflict_requested",
                before={"claims": [snapshot(r) for r in rows]},
                after={"requested_change": body.model_dump(), "review_required": reason},
                reason=body.reason, target_type="fleet_change")
    return {"status": "REQUESTED", "change_id": change.id, "entry": entry_out(db, rows)}


def apply_edit(db, company, actor, entry_id, body, *, reviewed=False, ignored_change_id=None):
    fleet.require_fleet_constraint(db)
    key, _ = load(db, company.id, entry_id)
    try:
        proposed_key = fleet.imo(body.imo_number) if body.action == "CORRECT" and body.imo_number else key
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    for value in sorted({key, proposed_key}):
        lock_identity(db, value)
    key, rows = load(db, company.id, entry_id, lock=True)
    if revision(rows) != body.revision:
        raise HTTPException(409, "The fleet entry changed. Refresh and try again.")
    pending = db.query(CompanyFleetChange.id).filter(CompanyFleetChange.claim_id.in_([r.id for r in rows]), CompanyFleetChange.status == "REQUESTED")
    if ignored_change_id is not None:
        pending = pending.filter(CompanyFleetChange.id != ignored_change_id)
    if pending.first():
        raise HTTPException(409, "A change for this vessel is already awaiting review. Its history is preserved.")
    selected = selected_periods(rows)
    targets = [r for group in selected.values() for r in group]
    if not targets:
        raise HTTPException(409, "This entry is closed. Add a new period if needed.")
    latest = max(targets, key=lambda r: r.id)
    vessel = db.get(Vessel, latest.vessel_id) if latest.vessel_id else None
    identity = latest.imo_number or (vessel.imo_number if vessel else None)
    name = latest.declared_vessel_name or (vessel.name if vessel else "Name not recorded")
    vessel_id = latest.vessel_id
    try:
        point = fleet.boundary(body.effective_date)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    desired = {}
    if body.action == "CORRECT":
        original_name = name
        try:
            identity = fleet.imo(body.imo_number or identity)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        name = body.vessel_name or name
        if (identity != key or vessel is not None) and len((body.reason or "").strip()) < 10:
            raise HTTPException(422, "Give a reason of at least ten characters for an IMO or operational vessel correction.")
        try:
            target = fleet.resolve(db, identity)
            ambiguous = False
        except ValueError:
            target, ambiguous = None, True
        different_company = db.query(VesselRoleClaim.id).filter(identity_key() == identity, VesselRoleClaim.company_id != company.id, VesselRoleClaim.status.in_(fleet.OPEN), VesselRoleClaim.revoked_at.is_(None)).first()
        mismatch = target is not None and " ".join(target.name.split()).casefold() != " ".join(name.split()).casefold()
        material = identity != key and (vessel is not None or target is not None or different_company)
        cross_company_name_change = different_company and name != original_name
        if not reviewed and (ambiguous or mismatch or cross_company_name_change or material):
            if not (body.evidence_reference or "").strip():
                raise HTTPException(422, "Add an evidence reference for an identity correction that requires review.")
            return _review_request(db, company.id, actor, rows, body, "Vessel identity needs independent review.")
        if ambiguous:
            raise HTTPException(409, "Resolve the ambiguous operational IMO before applying this correction.")
        vessel_id = target.id if target else None
        for role, group in selected.items():
            desired[role] = (min(moment(r.effective_start) for r in group), None if any(r.effective_end is None for r in group) else max(moment(r.effective_end) for r in group))
    elif body.action == "UPDATE":
        if not any(getattr(body, field) for field in fleet.ROLE_FIELDS):
            raise HTTPException(422, "Select at least one company role, or use Remove from Fleet.")
        if body.change_kind == "CHANGE" and point is None:
            raise HTTPException(422, "Enter the date when these role changes take effect.")
        for field, role in fleet.ROLE_FIELDS.items():
            if not getattr(body, field):
                if role in selected and point is None:
                    raise HTTPException(422, f"Enter an effective date to end {field.replace('_', ' ')}.")
                continue
            try:
                start = fleet.boundary(getattr(body, field + "_start_date"))
                end = fleet.boundary(getattr(body, field + "_end_date"))
            except ValueError as exc:
                raise HTTPException(422, f"{field.replace('_', ' ').title()}: {exc}") from None
            if start is None or (end is not None and end <= start):
                raise HTTPException(422, f"{field.replace('_', ' ').title()}: enter a start date and an end later than the start.")
            if body.change_kind == "CHANGE" and start < point:
                raise HTTPException(422, f"{field.replace('_', ' ').title()}: the new period must start on or after the change date.")
            desired[role] = (start, end)
    elif point is None:
        raise HTTPException(422, "Enter the date when the vessel leaves your fleet.")

    # A date/role update must not bypass an unresolved identity review from
    # initial declaration. Recheck the operational identity, even when the
    # pending claim itself is excluded from overlap detection below.
    identity_pending = False
    if body.action == "UPDATE":
        try:
            operational = vessel if key.startswith("vessel:") else fleet.resolve(db, key)
            identity_pending = operational is not None and (
                " ".join(operational.name.split()).casefold() != " ".join(name.split()).casefold()
            )
        except ValueError:
            identity_pending = True

    # Ordinary period edits do not change the operational identity. Legacy
    # submitted IMO text may be invalid or prefixed; use the authoritative
    # registry key for conflict checks rather than comparing raw text.
    same_identity = body.action != "CORRECT" or identity == key
    overlap_identity = key if body.action != "CORRECT" else identity

    # Ordinary updates never become approvals. A conflicting proposal is kept
    # separately while the company's current accepted intervals remain intact.
    target_ids = [r.id for r in targets]
    for role, (start, end) in desired.items():
        overlap = fleet.overlapping(db, overlap_identity, [role], start, end).filter(~VesselRoleClaim.id.in_(target_ids)).all()
        if any(r.company_id == company.id for r in overlap):
            raise HTTPException(422, "This period overlaps another period already in your fleet.")
        if overlap:
            if reviewed:
                raise HTTPException(409, "A competing role period is still active. Resolve it before accepting this change.")
            return _review_request(db, company.id, actor, rows, body, "Overlapping company role periods.")

    before = {"claims": [snapshot(r) for r in targets]}
    old_approved = [r for r in targets if r.status == "APPROVED" and same_identity]
    old_statuses = {r.id: r.status for r in targets}
    replacements = []
    now = datetime.now(timezone.utc)
    for row in targets:
        row.status = "CORRECTED"
        row.revoked_at = now
    db.flush()
    # A genuine later change retains every prior portion. Same-day correction
    # replaces the original rather than writing an invalid zero-length period.
    for row in targets:
        keep_prefix = body.action == "REMOVE" or (body.action == "UPDATE" and (
            body.change_kind == "CHANGE" or row.role not in desired
        ))
        if keep_prefix:
            if point < moment(row.effective_start):
                raise HTTPException(422, "The change date cannot precede the selected role's start.")
            end = _end_min(moment(row.effective_end), point)
            if end is None or moment(row.effective_start) < end:
                replacements.append(_new_claim(db, company.id, actor, identity, name, vessel_id, row.role, moment(row.effective_start), end, row if row in old_approved else None, old_statuses[row.id], row.review_reason))
    for role, (start, end) in desired.items():
        approved = [r for r in old_approved if r.role == role]
        for left, right, source in _segments(start, end, approved):
            replacements.append(_new_claim(
                db, company.id, actor, identity, name, vessel_id, role, left, right, source,
                status="SUBMITTED" if identity_pending and source is None else None,
                review_reason="Vessel identity requires independent review." if identity_pending and source is None else None,
            ))
    db.flush()
    action = {"UPDATE": "registry_roles_updated", "REMOVE": "registry_relationships_ended", "CORRECT": "registry_identity_corrected"}[body.action]
    fleet.audit(db, actor, company.id, rows[0].id, action, before=before,
                after={"claims": [snapshot(r) for r in replacements], "change_kind": body.change_kind}, reason=body.reason)
    updated = load(db, company.id, replacements[0].id if replacements and not same_identity else entry_id)[1]
    return {"status": "APPLIED", "review_pending": identity_pending, "entry": entry_out(db, updated)}
