"""One identity predicate for future port calls and independent declarations.

An explicit legacy operational link remains authoritative. An unlinked entry
can match a valid IMO only when that IMO identifies exactly one operational
vessel. No port call, booking or claim is relinked or backfilled by a read.
"""

from sqlalchemy import and_, or_, func, select, literal, String, cast, false
from sqlalchemy.orm import aliased

from app.db.models.vessel import Vessel
from app.db.models.shipping_company import VesselRoleClaim


def canonical_imo(column):
    return func.canonical_fleet_imo(column)


def identity_key():
    operational = (
        select(canonical_imo(Vessel.imo_number))
        .where(Vessel.id == VesselRoleClaim.vessel_id)
        .correlate(VesselRoleClaim)
        .scalar_subquery()
    )
    return func.coalesce(
        operational,
        VesselRoleClaim.fleet_identity_key,
        canonical_imo(VesselRoleClaim.imo_number),
        literal("vessel:") + cast(VesselRoleClaim.vessel_id, String),
    )


def claim_matches_vessel(vessel_column):
    if vessel_column is None:
        return false()
    target, duplicate = aliased(Vessel), aliased(Vessel)
    unique_number = (
        select(func.count(duplicate.id))
        .where(canonical_imo(duplicate.imo_number) == canonical_imo(target.imo_number))
        .correlate(target)
        .scalar_subquery()
    )
    # correlate_except keeps the caller's event table correlated even when the
    # supplied column is an ORM attribute or a literal vessel ID.
    match = (
        select(target.id)
        .where(
            target.id == vessel_column,
            canonical_imo(target.imo_number).isnot(None),
            canonical_imo(target.imo_number) == canonical_imo(VesselRoleClaim.imo_number),
            unique_number == 1,
        )
        .correlate_except(target)
        .exists()
    )
    return or_(
        VesselRoleClaim.vessel_id == vessel_column,
        and_(VesselRoleClaim.vessel_id.is_(None), match),
    )


def lock_identity(db, number):
    # Same namespace as the database trigger. Use stable order for bulk files.
    db.execute(select(func.pg_advisory_xact_lock(
        func.hashtextextended(literal("company-fleet:") + number, 0)
    )))


def operational_ids(db, claim):
    if claim.vessel_id is not None:
        return [claim.vessel_id]
    rows = db.query(Vessel.id).filter(
        canonical_imo(Vessel.imo_number) == canonical_imo(claim.imo_number)
    ).limit(2).all()
    return [rows[0][0]] if len(rows) == 1 else []


def claims_by_vessel(db, company_id, vessel_ids):
    """Resolve accepted claims in batches within the current transaction.

    Explicit links remain authoritative. An independent IMO is usable only
    when it identifies one operational vessel across the whole database.
    The result is request-local; a later download reads authorization again.
    """
    vessel_ids = set(vessel_ids)
    result = {key: [] for key in vessel_ids}
    claims = db.query(VesselRoleClaim).filter(
        VesselRoleClaim.company_id == company_id,
        VesselRoleClaim.status.in_(("APPROVED", "AUTO_ACCEPTED")),
        VesselRoleClaim.revoked_at.is_(None),
    ).all()
    independent = [claim for claim in claims if claim.vessel_id is None]
    unique = {}
    if independent:
        numbers = [row[0] for row in db.query(canonical_imo(VesselRoleClaim.imo_number))
                   .filter(VesselRoleClaim.id.in_([c.id for c in independent])).all() if row[0]]
        for number, count, vessel_id in db.query(
            canonical_imo(Vessel.imo_number), func.count(Vessel.id), func.min(Vessel.id)
        ).filter(canonical_imo(Vessel.imo_number).in_(numbers)).group_by(canonical_imo(Vessel.imo_number)):
            if count == 1:
                unique[number] = vessel_id
        normalized = dict(db.query(VesselRoleClaim.id, canonical_imo(VesselRoleClaim.imo_number))
                          .filter(VesselRoleClaim.id.in_([c.id for c in independent])).all())
    else:
        normalized = {}
    for claim in claims:
        vessel_id = claim.vessel_id if claim.vessel_id is not None else unique.get(normalized.get(claim.id))
        if vessel_id in result:
            result[vessel_id].append(claim)
    return result


def lock_claim_identity(db, claim_id):
    """Take the identity lock before any claim or review row lock.

    All mutation routes use this order, including the legacy approval routes.
    Refresh the claim after acquiring the lock so a waited mutation cannot act
    on an ORM snapshot from before another transaction committed.
    """
    key = db.query(identity_key()).filter(VesselRoleClaim.id == claim_id).scalar()
    if key is not None:
        lock_identity(db, key)
    return db.query(VesselRoleClaim).filter_by(id=claim_id).populate_existing().with_for_update().first()
