"""Welfare scopes are explicit and rechecked on every aggregate request."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.db.base  # noqa: F401
from app.db.models.port import Port
from app.db.models.port_geography import PortGeography
from app.db.models.user import User
from app.db.models.welfare_organization import WelfareMembership, WelfareOrganization, WelfareScope, WelfareScopePort
from app.services.welfare_access import may_access_welfare_aggregate


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    for model in (User, Port, PortGeography, WelfareOrganization, WelfareMembership, WelfareScope, WelfareScopePort):
        model.__table__.create(engine)
    with Session(engine) as session:
        session.add_all([
            User(id=1, email="demo@example.invalid", hashed_password="not-a-password"),
            Port(id=1, name="Example", code="example", canonical_key="example"),
            Port(id=2, name="Other", code="other", canonical_key="other"),
            WelfareOrganization(id=1, legal_name="Example", status="ACTIVE", verified_at=datetime.now(timezone.utc), verified_by_user_id=1),
            WelfareOrganization(id=2, legal_name="Other", status="ACTIVE", verified_at=datetime.now(timezone.utc), verified_by_user_id=1),
        ])
        session.flush()
        session.add_all([
            PortGeography(port_id=1, country_code="IN", evidence_reference="reviewed", updated_by_user_id=1),
            PortGeography(port_id=2, country_code="IN", evidence_reference="reviewed", updated_by_user_id=1),
            WelfareMembership(organization_id=1, user_id=1, role="ANALYST", enabled=True),
            WelfareScope(organization_id=1, countries=["IN"], modules=["overview"],
                data_categories=["calls"], aggregate_levels=["port"], export_allowed=True,
                effective_from=datetime.now(timezone.utc) - timedelta(days=1)),
        ])
        session.flush()
        session.add(WelfareScopePort(organization_id=1, port_id=1))
        session.commit()
        yield session
    engine.dispose()


def access(db, **changes):
    args = dict(user_id=1, organization_id=1, module="overview", data_category="calls", aggregate_level="port", port_ids={1})
    args.update(changes)
    return may_access_welfare_aggregate(db, **args)


def test_scope_and_geography_are_required(db):
    assert access(db)
    assert not access(db, port_ids=set())
    assert not access(db, port_ids={1, 2})
    assert not access(db, organization_id=2)
    assert not access(db, module="facilities")
    assert not access(db, data_category="crew")
    assert not access(db, aggregate_level="national")
    db.get(WelfareScope, 1).countries = ["GB"]
    db.commit()
    assert not access(db)


def test_membership_and_organization_are_rechecked(db):
    assert access(db, export=True)
    member = db.query(WelfareMembership).one()
    member.role = "VIEWER"
    db.commit()
    assert access(db)
    assert not access(db, export=True)
    member.enabled = False
    db.commit()
    assert not access(db)
    member.enabled = True
    db.get(WelfareOrganization, 1).status = "SUSPENDED"
    db.commit()
    assert not access(db)


def test_missing_or_expired_scope_fails_closed(db):
    scope = db.get(WelfareScope, 1)
    scope.effective_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()
    assert not access(db)
    scope.effective_until = None
    scope.modules = {"overview": True}
    db.commit()
    assert not access(db)


def test_revocation_in_another_session_is_seen(db):
    assert access(db, export=True)
    with Session(db.get_bind()) as other:
        other.query(WelfareMembership).one().enabled = False
        other.commit()
    assert not access(db)


def test_migration_creates_tables_and_can_run_after_startup_create_all():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    Port.__table__.create(engine)
    path = Path(__file__).resolve().parents[1] / "alembic/versions/m9b0c1d2e3f4_welfare_organization_foundation.py"
    spec = importlib.util.spec_from_file_location("welfare_foundation_migration", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    with engine.begin() as connection:
        revision.op = Operations(MigrationContext.configure(connection))
        revision.upgrade()
        revision.upgrade()
        assert set(("welfare_organizations", "welfare_memberships", "welfare_scopes", "welfare_scope_ports")).issubset(
            __import__("sqlalchemy").inspect(connection).get_table_names())
    engine.dispose()


def test_activation_requires_recorded_verification(db):
    from sqlalchemy.exc import IntegrityError

    db.add(WelfareOrganization(legal_name="Unverified", status="ACTIVE"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()
