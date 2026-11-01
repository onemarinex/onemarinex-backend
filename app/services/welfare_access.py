"""Fail-closed checks for future organization-scoped aggregate endpoints.

Callers must derive the port IDs from the source records they will disclose,
not from an untrusted request filter. Empty/unknown coverage is not authorized.
"""
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models.port_geography import PortGeography
from app.db.models.welfare_organization import WelfareMembership, WelfareOrganization, WelfareScope, WelfareScopePort


def _contains(values, value):
    return (isinstance(values, list) and all(isinstance(item, str) and item for item in values)
            and isinstance(value, str) and bool(value) and value in values)


def may_access_welfare_aggregate(
    db: Session, *, user_id: int, organization_id: int, module: str,
    data_category: str, aggregate_level: str, port_ids: set[int], export: bool = False,
) -> bool:
    """Check current membership, scope, reviewed geography and export rights."""
    if not isinstance(port_ids, (set, frozenset)) or not port_ids or any(type(port_id) is not int or port_id <= 0 for port_id in port_ids):
        return False
    now = datetime.now(timezone.utc)
    result = (db.query(WelfareMembership, WelfareScope)
        .join(WelfareOrganization, WelfareOrganization.id == WelfareMembership.organization_id)
        .join(WelfareScope, WelfareScope.organization_id == WelfareOrganization.id)
        .populate_existing()
        .filter(WelfareMembership.user_id == user_id, WelfareOrganization.id == organization_id,
                WelfareOrganization.status == "ACTIVE", WelfareMembership.enabled.is_(True),
                WelfareScope.effective_from <= now,
                (WelfareScope.effective_until.is_(None) | (WelfareScope.effective_until > now)))
        .one_or_none())
    if not result:
        return False
    membership, scope = result
    if membership.role not in ("ADMIN", "ANALYST", "VIEWER"):
        return False
    if export and (not scope.export_allowed or membership.role == "VIEWER"):
        return False
    if not (_contains(scope.modules, module) and _contains(scope.data_categories, data_category)
            and _contains(scope.aggregate_levels, aggregate_level)):
        return False
    ports = (db.query(WelfareScopePort.port_id, PortGeography.country_code)
        .join(PortGeography, PortGeography.port_id == WelfareScopePort.port_id)
        .filter(WelfareScopePort.organization_id == organization_id,
                WelfareScopePort.port_id.in_(port_ids)).all())
    return (len(ports) == len(port_ids)
            and all(_contains(scope.countries, country) for _, country in ports))
