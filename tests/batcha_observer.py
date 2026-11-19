"""Disposable QA fixture builder/observer, never imported by application startup.

No application handler, dependency, model or schema is replaced. The optional
SQL barrier delays real DB statements; it never changes their contents/results.
"""
import datetime as dt
import os
import threading
import time
from decimal import Decimal

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel
from sqlalchemy import Date, DateTime, Enum, event, select, text
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.session import SessionLocal, engine

SEED_TABLES = {
    "ports", "vessels", "vessel_calls", "vessel_crew", "crew_assignments",
    "shore_passes", "crew_profiles", "port_rules",
    "cab_bookings", "crew_sos_requests", "crew_sos_timeline_events", "incidents", "agent_profiles",
    "users",
    "company_user_memberships", "vessel_role_claims", "aggregator_profiles",
}
HIDDEN = {"hashed_password", "password", "code_hash", "ride_otp", "token",
          "access_token", "refresh_token", "secret", "magic_token"}
LOG = []
LOCK = threading.Lock()
GATE = None
SINK = {"email_failures": 0, "whatsapp_failures": 0, "attempts": []}
FAULT = {"match": None, "remaining": 0, "events": []}


def log(kind, **values):
    with LOCK:
        LOG.append({"sequence": len(LOG) + 1, "monotonic_ns": time.monotonic_ns(),
                    "kind": kind, "thread": threading.get_ident(), **values})


def install(app):
    if engine.url.database != "company_ops_fixture" or engine.url.host != "127.0.0.1" or engine.url.port != 55957:
        raise RuntimeError("QA observer refuses any non-disposable database")

    class SinkMode(BaseModel):
        email_failures: int = 0
        whatsapp_failures: int = 0

    @app.post("/__fixture/batch/sinks")
    def sink_mode(body: SinkMode):
        if not 0 <= body.email_failures <= 3 or not 0 <= body.whatsapp_failures <= 3:
            raise HTTPException(400, "Bounded synthetic sink failures only")
        SINK.update(email_failures=body.email_failures, whatsapp_failures=body.whatsapp_failures)
        return {"fixture_only": True, "mode": body.model_dump()}

    @app.get("/__fixture/batch/sinks")
    def sink_evidence():
        return SINK

    @event.listens_for(engine, "before_cursor_execute")
    def observe(conn, cursor, statement, parameters, context, executemany):
        global GATE
        pid = conn.connection.driver_connection.get_backend_pid()
        sql = statement.lower()
        if FAULT["remaining"] and sql.lstrip().startswith(FAULT["match"]):
            FAULT["remaining"] -= 1
            log("one-shot-database-fault", pid=pid, match=FAULT["match"])
            FAULT["events"].append({"pid": pid, "match": FAULT["match"], "sequence": len(LOG)})
            # A real Postgres statement error aborts only this transaction.
            # No business handler, return value or commit method is mocked.
            cursor.execute("SELECT 1 / 0")
        if sql.lstrip().startswith(("insert", "update", "delete")):
            # SQL values are deliberately not collected.
            log("statement", pid=pid, sql=statement[:600])
        gate = GATE
        if gate and gate["match"] in sql and sql.lstrip().startswith("select"):
            with LOCK:
                first = pid not in gate["seen"] and len(gate["seen"]) < 2
                if first:
                    gate["seen"].add(pid)
            if first:
                log("barrier-arrived", pid=pid)
                try:
                    gate["barrier"].wait(timeout=12)
                    log("barrier-released", pid=pid)
                except threading.BrokenBarrierError:
                    log("barrier-timeout", pid=pid)

    @event.listens_for(engine, "commit")
    def commit(conn):
        log("commit-invoked", pid=conn.connection.driver_connection.get_backend_pid())

    @event.listens_for(Session, "after_commit")
    def committed(session):
        log("commit-confirmed")

    @app.get("/__fixture/batch/snapshot")
    def snapshot():
        with engine.connect() as conn:
            identity = dict(conn.execute(text("select current_database() db, inet_server_addr()::text host, inet_server_port() port, pg_backend_pid() observer_pid, current_setting('transaction_isolation') isolation")).mappings().one())
            tables = {}
            for table in Base.metadata.sorted_tables:
                if table.name in {"email_verifications", "refresh_tokens"}:
                    continue
                columns = [c for c in table.columns if c.name not in HIDDEN and not any(k in c.name.lower() for k in ("password", "token", "secret", "otp", "hash"))]
                rows = conn.execute(select(*columns).limit(300)).mappings().all()
                tables[table.name] = [dict(r) for r in rows]
            return jsonable_encoder({"identity": identity, "tables": tables, "transaction_events": list(LOG)})

    @app.get("/__fixture/batch/schema")
    def schema():
        from app.services import email, whatsapp
        with engine.connect() as conn:
            constraints = [dict(r) for r in conn.execute(text("SELECT conname, conrelid::regclass::text table_name, pg_get_constraintdef(oid) definition FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY conrelid,conname")).mappings()]
            indexes = [dict(r) for r in conn.execute(text("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname")).mappings()]
            version_table = conn.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
            versions = list(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) if version_table else []
            triggers = [dict(r) for r in conn.execute(text("SELECT c.relname table_name,t.tgname,pg_get_triggerdef(t.oid) definition FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE NOT t.tgisinternal AND c.relnamespace='public'::regnamespace ORDER BY c.relname,t.tgname")).mappings()]
            identity = dict(conn.execute(text("select current_database() db, current_user db_user, inet_server_addr()::text host, inet_server_port() port, pg_backend_pid() observer_pid, clock_timestamp() database_clock")).mappings().one())
            return jsonable_encoder({"database_identity":identity, "external_adapters":{"email":email.send_email.__name__, "whatsapp_http":whatsapp.httpx.post.__name__, "manifest_pdf_processor":os.getenv("MANIFEST_PDF_PROCESSOR", "anthropic")}, "fixture_schema_method":"Base.metadata.create_all + existing isolated fleet schema/exclusion initializer", "alembic_versions":versions, "migration_parity_verified":False, "constraints":constraints, "indexes":indexes,"triggers":triggers})

    class Seed(BaseModel):
        table: str
        rows: list[dict]

    @app.post("/__fixture/batch/seed")
    def seed(body: Seed):
        if body.table not in SEED_TABLES or body.table == "users" or not 1 <= len(body.rows) <= 30:
            raise HTTPException(400, "Unsupported QA fixture builder")
        table = Base.metadata.tables[body.table]
        ids = []
        with engine.begin() as conn:
            for raw in body.rows:
                row = dict(raw)
                if set(row) - set(table.columns.keys()):
                    raise HTTPException(400, "Unknown fixture column")
                for key, value in row.items():
                    kind = table.c[key].type
                    if isinstance(value, str) and isinstance(kind, Enum) and kind.enum_class:
                        row[key] = kind.enum_class(value)
                    elif isinstance(value, str) and (isinstance(kind, DateTime) or isinstance(getattr(kind, "impl", None), DateTime)):
                        row[key] = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                    elif isinstance(value, str) and isinstance(table.c[key].type, Date):
                        row[key] = dt.date.fromisoformat(value)
                ids.append(conn.execute(table.insert().values(**row).returning(table.c.id)).scalar_one())
        return {"table": body.table, "ids": ids, "fixture_only": True}

    class Update(BaseModel):
        table: str
        id: int
        values: dict

    @app.patch("/__fixture/batch/seed")
    def update(body: Update):
        if body.table not in SEED_TABLES or not body.values or "id" in body.values:
            raise HTTPException(400, "Unsupported QA fixture update")
        if body.table == "users" and set(body.values) != {"mobile_number"}:
            raise HTTPException(400, "Synthetic actor phone fixture only")
        table = Base.metadata.tables[body.table]
        row = dict(body.values)
        if set(row) - set(table.columns.keys()):
            raise HTTPException(400, "Unknown fixture column")
        for key, value in row.items():
            kind = table.c[key].type
            if isinstance(value, str) and isinstance(kind, Enum) and kind.enum_class:
                row[key] = kind.enum_class(value)
            elif isinstance(value, str) and (isinstance(kind, DateTime) or isinstance(getattr(kind, "impl", None), DateTime)):
                row[key] = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            elif isinstance(value, str) and isinstance(table.c[key].type, Date):
                row[key] = dt.date.fromisoformat(value)
        with engine.begin() as conn:
            result = conn.execute(table.update().where(table.c.id == body.id).values(**row))
        return {"updated": result.rowcount, "fixture_only": True}

    class Gate(BaseModel):
        match: str

    @app.post("/__fixture/batch/barrier")
    def barrier(body: Gate):
        global GATE
        if body.match not in {"from crew_assignments", "from vessel_crew", "from vessel_calls", "from vessels", "from crew_sos_requests"}:
            raise HTTPException(400, "Unsupported observation barrier")
        GATE = {"match": body.match, "seen": set(), "barrier": threading.Barrier(2)}
        return {"armed": True, "participants": 2, "timeout_seconds": 12, "match": body.match}

    @app.get("/__fixture/batch/barrier")
    def barrier_result():
        return {"pids": sorted(GATE["seen"]) if GATE else [], "events": list(LOG)}

    class Fault(BaseModel):
        match: str

    @app.post("/__fixture/batch/fault")
    def fault(body: Fault):
        if body.match not in {"insert into crew_sos_requests", "update crew_assignments"}:
            raise HTTPException(400, "Unsupported isolated fault boundary")
        FAULT.update(match=body.match, remaining=1, events=[])
        return {"armed": True, "match": body.match, "remaining": 1}

    @app.get("/__fixture/batch/fault")
    def fault_result():
        return FAULT
