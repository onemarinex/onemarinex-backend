"""Read-only event origin classification. Never infer history from today's roster."""

from sqlalchemy import and_, or_, select, func
from sqlalchemy.orm import aliased
from app.db.models.vessel_call import VesselCall

CONFIRMED_AGENCY = "CONFIRMED_AGENCY"
CONFIRMED_UNMANAGED = "CONFIRMED_UNMANAGED"
UNRESOLVED = "UNRESOLVED"
CONFLICTED = "CONFLICTED"
CONFIRMED = (CONFIRMED_AGENCY, CONFIRMED_UNMANAGED)


def classify(record, call):
    explicit = (getattr(record, "context_resolution", None) or "").strip().upper()
    if explicit in (UNRESOLVED, CONFLICTED):
        return explicit
    if not record.vessel_id or not record.vessel_call_id or call is None:
        return UNRESOLVED
    if record.vessel_id != call.vessel_id or record.agency_id != call.agency_id:
        return CONFLICTED
    if record.agency_id is not None:
        return CONFIRMED_AGENCY
    if (
        explicit == CONFIRMED_UNMANAGED
        or (call.agency_name or "").strip().lower() == "other"
    ):
        return CONFIRMED_UNMANAGED
    return UNRESOLVED


def resolved_origin(model):
    """Correlated SQL equivalent of classify(); used BEFORE count/sum/pagination."""
    call = aliased(VesselCall)
    known = or_(
        model.agency_id.isnot(None),
        and_(
            model.agency_id.is_(None),
            or_(
                func.upper(func.trim(model.context_resolution)) == CONFIRMED_UNMANAGED,
                func.lower(func.trim(call.agency_name)) == "other",
            ),
        ),
    )
    agency_matches = or_(
        model.agency_id == call.agency_id,
        and_(model.agency_id.is_(None), call.agency_id.is_(None)),
    )
    return and_(
        func.upper(func.coalesce(func.trim(model.context_resolution), "")).notin_(
            (UNRESOLVED, CONFLICTED)
        ),
        select(call.id)
        .where(
            call.id == model.vessel_call_id,
            call.vessel_id == model.vessel_id,
            agency_matches,
            known,
        )
        .exists(),
    )


def agency_scope(model, agency_id):
    """Operational ownership can remain known when an old call link is missing.

    Preserve immutable agency-stamped history. Never use today's vessel/roster
    to fill a null agency; quarantine explicit conflicts and inconsistent calls.
    Company aggregates separately require resolved_origin(), including a call.
    """
    if agency_id is None:
        from sqlalchemy import false

        return false()
    call = aliased(VesselCall)
    same_call = (
        select(call.id)
        .where(
            call.id == model.vessel_call_id,
            call.vessel_id == model.vessel_id,
            call.agency_id == model.agency_id,
        )
        .exists()
    )
    return and_(
        model.agency_id == agency_id,
        func.upper(func.coalesce(func.trim(model.context_resolution), "")).notin_(
            (UNRESOLVED, CONFLICTED)
        ),
        or_(model.vessel_call_id.is_(None), same_call),
    )


def origin_expression(model):
    """SQL classifier matching classify(), suitable for queues and aggregate counts."""
    from sqlalchemy import case

    call = aliased(VesselCall)
    explicit = func.upper(func.coalesce(func.trim(model.context_resolution), ""))
    different_agency = or_(
        call.agency_id != model.agency_id,
        and_(call.agency_id.is_(None), model.agency_id.isnot(None)),
        and_(call.agency_id.isnot(None), model.agency_id.is_(None)),
    )
    mismatch = (
        select(call.id)
        .where(
            call.id == model.vessel_call_id,
            or_(call.vessel_id != model.vessel_id, different_agency),
        )
        .exists()
    )
    return case(
        (explicit.in_((UNRESOLVED, CONFLICTED)), explicit),
        (or_(model.vessel_id.is_(None), model.vessel_call_id.is_(None)), UNRESOLVED),
        (mismatch, CONFLICTED),
        (and_(resolved_origin(model), model.agency_id.isnot(None)), CONFIRMED_AGENCY),
        (resolved_origin(model), CONFIRMED_UNMANAGED),
        else_=UNRESOLVED,
    )
