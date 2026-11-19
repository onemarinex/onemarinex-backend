"""Disposable QA fixture builder/observer, never imported by application startup.

No application handler, dependency, model or schema is replaced. The optional
SQL barrier delays real DB statements; it never changes their contents/results.
"""
import datetime as dt
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
    "cab_bookings", "crew_sos", "crew_sos_timeline_events",
}
HIDDEN = {"hashed_password", "password", "code_hash", "ride_otp", "token",
          "access_token", "refresh_token", "secret", "magic_token"}
LOG = []
LOCK = threading.Lock()
GATE = None


def log(kind, **values):
    with LOCK:
        LOG.append({"sequence": len(LOG) + 1, "monotonic_ns": time.monotonic_ns(),
                    "kind": kind, "thread": threading.get_ident(), **values})


def install(app):
    if engine.url.database != "company_ops_fixture" or engine.url.host != "127.0.0.1" or engine.url.port != 55957:
        raise RuntimeError("QA observer refuses any non-disposable database")

    @event.listens_for(engine, "before_cursor_execute")
    def observe(conn, cursor, statement, parameters, context, executemany):
        global GATE
        pid = conn.connection.driver_connection.get_backend_pid()
        sql = statement.lower()
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

    class Seed(BaseModel):
        table: str
        rows: list[dict]

    @app.post("/__fixture/batch/seed")
    def seed(body: Seed):
        if body.table not in SEED_TABLES or not 1 <= len(body.rows) <= 30:
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
        if body.match not in {"from crew_assignments", "from vessel_crew", "from vessel_calls", "from vessels"}:
            raise HTTPException(400, "Unsupported observation barrier")
        GATE = {"match": body.match, "seen": set(), "barrier": threading.Barrier(2)}
        return {"armed": True, "participants": 2, "timeout_seconds": 12, "match": body.match}

    @app.get("/__fixture/batch/barrier")
    def barrier_result():
        return {"pids": sorted(GATE["seen"]) if GATE else [], "events": list(LOG)}
