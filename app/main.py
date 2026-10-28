from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.models import OAuthFlows as OAuthFlowsModel
from fastapi.openapi.utils import get_openapi
import logging
import logging.handlers
import sys
import os
from sqlalchemy import inspect, text
from apscheduler.schedulers.background import BackgroundScheduler

# --- Logging ---
# Without this, every logger.warning/exception in the app (notably the
# WhatsApp send failures) goes only to the console of whoever started the
# server, which makes production issues effectively invisible. Mirror all
# logs to a rotating file as well.
os.makedirs("logs", exist_ok=True)
_log_formatter = logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
)
_file_handler = logging.handlers.RotatingFileHandler(
    "logs/app.log", maxBytes=5 * 1024 * 1024, backupCount=3
)
_file_handler.setFormatter(_log_formatter)

_root_logger = logging.getLogger()
if not any(
    isinstance(h, logging.handlers.RotatingFileHandler)
    for h in _root_logger.handlers
):
    _root_logger.addHandler(_file_handler)

# And to stdout, which is the only one anybody can read on the deployed
# platform. The file handler above writes inside the container: DigitalOcean
# collects stdout, not arbitrary files, and the file is destroyed on every
# redeploy -- so until now every application log line was invisible in
# production and gone within a day.
#
# That is not just inconvenient. `ensure_company_domain_schema` catches its own
# exception and logs it, so the one startup guard that can fail without
# stopping the deploy was failing where nobody could see. The same went for the
# record of each crew-manifest PDF leaving the system and of who verified a
# vendor, both of which are deliberately log lines rather than audit tables.
# Matched on our own formatter rather than on "any StreamHandler": anything
# else attached to the root logger -- pytest's capture handlers, for one -- is
# not ours and must not stop ours being added.
if not any(
    isinstance(h, logging.StreamHandler)
    and not isinstance(h, logging.handlers.RotatingFileHandler)
    and getattr(getattr(h, "formatter", None), "_fmt", None) == _log_formatter._fmt
    for h in _root_logger.handlers
):
    _stream_handler = logging.StreamHandler(sys.stdout)
    _stream_handler.setFormatter(_log_formatter)
    _root_logger.addHandler(_stream_handler)
if _root_logger.level == logging.NOTSET or _root_logger.level > logging.INFO:
    _root_logger.setLevel(logging.INFO)

from app.core.config import settings
from app.db.schema_verification import verify_critical_schema
from app.db.session import ddl_transaction, engine
from app.db.base import Base
from app.services.shore_pass_reminders import run_shore_pass_reminders

from app.api.v1 import routes_auth, routes_contact, routes_files, routes_users, registration, routes_crew, routes_pubs, routes_hotels, routes_restaurants, routes_incidents, routes_ports, routes_drivers, routes_early_access, routes_chat, routes_superadmin, routes_reviews, routes_sightseeing, routes_notifications, routes_sos, routes_pricing_controls, routes_facilities, routes_chat_moderation

from app.api.v1.routes_vendor import router as vendor_router
from app.api.v1.routes_rfqs import router as rfq_router
from app.api.v1 import routes_quotes 
from app.api.v1 import routes_orders 
from app.api.v1 import routes_vessels
from app.api.v1 import routes_shipping_company
from app.api.v1 import routes_company_review
from app.api.v1 import routes_vendor_review
from app.api.v1 import routes_privacy
from app.api.v1 import routes_trips
from app.api.v1 import routes_agents
from app.api.v1 import routes_aggregators
from app.api.v1 import routes_bookings
from app.api.v1 import routes_itinerary
from app.api.v1 import routes_expenses
from app.api.v1 import routes_payments

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {
        "Bearer": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        }
    }
    # Apply security globally to all endpoints (optional, but good for docs)
    openapi_schema["security"] = [{"Bearer": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app = FastAPI(
    title="OneMarinex API",
    description="""
OneMarinex Backend API providing digitized port services, crew management, 
and sustainability tracking. Connects mariners, port agents, and aggregators.

### Core Features:
* **Authentication**: JWT-based security for all roles.
* **Crew Services**: Shore pass management and booking.
* **Port Operations**: Real-time monitoring and compliance.
* **Stakeholder Portal**: Specialized access for Agents and Aggregators.
""",
    version="1.0.0",
    contact={
        "name": "OneMarinex Support",
        "email": "support@onemarinex.io",
    },
    swagger_ui_parameters={"defaultModelsExpandDepth": -1}
)

app.openapi = custom_openapi

# --- CORS config ---
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:5175",
    "http://127.0.0.1:5175",
    "https://heyports-56we8.ondigitalocean.app",  # Production frontend
    "https://www.heyports-56we8.ondigitalocean.app",
    "https://heyports-dev-5285u.ondigitalocean.app",
    "https://heyports.com",
    "https://www.heyports.com",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://[a-z0-9-]+\.ondigitalocean\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # The frontend is a separate origin from this API, and a browser hides
    # every response header from JavaScript except a short safelist unless the
    # server names it here. `allow_headers` is about the *request*; this is the
    # response side, and without it the result-ceiling headers reach the
    # browser and are invisible to the code that has to act on them -- a list
    # silently truncated at the ceiling would render as a complete one.
    expose_headers=[
        "X-Result-Count",
        "X-Result-Limit",
        "X-Result-Truncated",
        "X-Collection-Pages",
    ],
)

# WebSocket connection logging middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

class WebSocketLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/v1/chat/ws"):
            logger = logging.getLogger("websocket_middleware")
            logger.info(f"🟢 [MIDDLEWARE] WebSocket request to {request.url.path}?{request.url.query}")
        return await call_next(request)

app.add_middleware(WebSocketLoggingMiddleware)

scheduler = BackgroundScheduler()

# --- Startup event: ensure tables exist ---
@app.on_event("startup")
def on_startup():
    # Base is already linked to all models via app/db/base.py imports
    Base.metadata.create_all(bind=engine)
    ensure_legacy_schema_columns()
    ensure_expense_bill_columns()
    ensure_chat_message_columns()
    ensure_magic_link_hardening_schema()
    ensure_port_time_and_sos_context_schema()
    ensure_vendor_commission_schema()
    ensure_agent_dashboard_schema()
    ensure_agent_support_number()
    ensure_cab_booking_vessel_id()
    ensure_agent_agency_rules()
    ensure_historical_context_schema()
    ensure_placeholder_helplines_removed()
    ensure_port_identity_schema()
    ensure_assignment_public_ref()
    ensure_assignment_source()
    ensure_shore_pass_profile_nullable()
    ensure_assignment_movement_columns()
    ensure_magic_link_lifecycle()
    ensure_ride_otp_attempt_columns()
    ensure_auth_attempt_columns()
    ensure_incident_occurred_at()
    ensure_company_domain_schema()
    ensure_alembic_baseline()
    # Last, after every guard has had its turn. Raises rather than logging, so
    # a guard that failed aborts the deployment instead of serving a broken
    # application that reports healthy.
    verify_critical_schema()
    _log_whatsapp_config()
    _log_chat_moderation_config()
    scheduler.add_job(run_shore_pass_reminders, "interval", minutes=5, id="shore_pass_reminders", replace_existing=True)
    scheduler.start()


def _log_whatsapp_config() -> None:
    """State the WhatsApp config at boot, without printing any secret.

    A deploy whose env vars never made it across is otherwise indistinguishable
    from a working one until someone notices, days later, that no customer got
    a message. This one line makes it obvious in the logs on every boot.
    """
    logging.getLogger("app.startup").info(
        "WhatsApp config: enabled=%s access_token_set=%s phone_number_id_set=%s "
        "api_version=%s default_country_code=%s public_base_url=%s",
        settings.WHATSAPP_ENABLED,
        bool(settings.WHATSAPP_ACCESS_TOKEN),
        bool(settings.WHATSAPP_PHONE_NUMBER_ID),
        settings.WHATSAPP_API_VERSION,
        settings.WHATSAPP_DEFAULT_COUNTRY_CODE,
        settings.APP_PUBLIC_BASE_URL,
    )
    if not settings.WHATSAPP_ENABLED:
        logging.getLogger("app.startup").warning(
            "WhatsApp is DISABLED — no template messages will be sent from this instance."
        )
    elif not (settings.WHATSAPP_ACCESS_TOKEN and settings.WHATSAPP_PHONE_NUMBER_ID):
        logging.getLogger("app.startup").warning(
            "WhatsApp is enabled but credentials are missing — every send will be skipped."
        )


def _log_chat_moderation_config() -> None:
    """State chat moderation config at boot without printing secrets."""
    from app.services.moderation_ai import moderation_enabled

    logging.getLogger("app.startup").info(
        "Chat moderation config: ai_enabled=%s model=%s timeout=%s fail_closed=%s",
        moderation_enabled(),
        os.getenv("CHAT_MODERATION_MODEL", "claude-opus-5"),
        os.getenv("CHAT_MODERATION_TIMEOUT", "8.0"),
        os.getenv("CHAT_MODERATION_FAIL_CLOSED", "true"),
    )
    if not moderation_enabled():
        logging.getLogger("app.startup").warning(
            "ANTHROPIC_API_KEY unset — AI moderation disabled, Level 0+1 checks only."
        )


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


def ensure_legacy_schema_columns():
    inspector = inspect(engine)
    if "aggregator_profiles" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("aggregator_profiles")}
    if "provider_type" in columns:
        return
    with ddl_transaction() as connection:
        connection.execute(
            text("ALTER TABLE aggregator_profiles ADD COLUMN provider_type VARCHAR(32) DEFAULT 'aggregator' NOT NULL")
        )


def ensure_chat_message_columns():
    """Reply / edit / soft-delete columns for `chat_messages`.

    Alembic migration a8e1c2d3f4b5 defines these, but nothing runs
    `alembic upgrade head` on deploy and create_all never ALTERs an existing
    table — so without this the chat endpoints would 500 with UndefinedColumn.

    Everything here is idempotent and additive, and the migration itself is
    guarded the same way, so running Alembic before or after this is safe.
    Failures are logged rather than raised: a patch problem must not stop the
    whole app from booting.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "chat_messages" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it with all columns

        existing = {c["name"] for c in inspector.get_columns("chat_messages")}
        additions = {
            "reply_to_id": "INTEGER",
            "edited_at": "TIMESTAMP WITH TIME ZONE",
            "deleted_at": "TIMESTAMP WITH TIME ZONE",
        }
        missing = {n: ddl for n, ddl in additions.items() if n not in existing}

        index_names = {i["name"] for i in inspector.get_indexes("chat_messages")}
        fk_names = {f.get("name") for f in inspector.get_foreign_keys("chat_messages")}
        need_index = "ix_chat_messages_reply_to_id" not in index_names
        need_fk = "fk_chat_messages_reply_to_id" not in fk_names

        if not missing and not need_index and not need_fk:
            return

        with ddl_transaction() as connection:
            for name, ddl in missing.items():
                connection.execute(
                    text(f"ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS {name} {ddl}")
                )
            if need_index:
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_chat_messages_reply_to_id "
                        "ON chat_messages (reply_to_id)"
                    )
                )
            if need_fk:
                # Postgres has no ADD CONSTRAINT IF NOT EXISTS; the name check
                # above plus this guard keeps a concurrent boot from erroring.
                connection.execute(
                    text(
                        "DO $$ BEGIN "
                        "ALTER TABLE chat_messages ADD CONSTRAINT fk_chat_messages_reply_to_id "
                        "FOREIGN KEY (reply_to_id) REFERENCES chat_messages (id) ON DELETE SET NULL; "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                    )
                )
        log.info(
            "chat_messages patched (columns=%s index=%s fk=%s)",
            sorted(missing), need_index, need_fk,
        )
    except Exception:
        log.exception("ensure_chat_message_columns failed — chat features may be degraded")


def ensure_expense_bill_columns():
    """`expense_bills` shipped before trip-linking / tax-split / bill-number.
    create_all never ALTERs an existing table, so add the new columns here
    (idempotent). Plain columns (no FK constraint) — ownership is validated in
    the API layer."""
    inspector = inspect(engine)
    if "expense_bills" not in inspector.get_table_names():
        return  # brand-new deploy: create_all already built it with all columns
    existing = {c["name"] for c in inspector.get_columns("expense_bills")}
    additions = {
        "amount_pre_tax": "NUMERIC(10,2)",
        "amount_post_tax": "NUMERIC(10,2)",
        "bill_number": "VARCHAR(128)",
        "shore_pass_id": "INTEGER",
        "cab_booking_id": "INTEGER",
    }
    missing = {name: ddl for name, ddl in additions.items() if name not in existing}
    if not missing:
        return
    with ddl_transaction() as connection:
        for name, ddl in missing.items():
            connection.execute(text(f"ALTER TABLE expense_bills ADD COLUMN IF NOT EXISTS {name} {ddl}"))


def ensure_magic_link_hardening_schema():
    """Apply the additive magic-link hardening schema on deployments that do
    not run Alembic before boot.

    The matching Alembic revisions remain the canonical migration path. These
    guarded statements make an existing DigitalOcean database safe before the
    ORM queries the new OTP fields, regardless of whether migration or app
    startup happens first.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        table_names = set(inspector.get_table_names())

        link_table = "driver_magic_links"
        event_table = "driver_magic_link_reach_events"
        constraint_name = "uq_driver_magic_link_reach_event_stop"

        link_additions = {}
        if link_table in table_names:
            existing_columns = {
                column["name"] for column in inspector.get_columns(link_table)
            }
            additions = {
                "otp_verified_at": "TIMESTAMP WITH TIME ZONE",
                "otp_failed_attempts": "INTEGER DEFAULT 0 NOT NULL",
                "otp_last_attempt_at": "TIMESTAMP WITH TIME ZONE",
                "otp_locked_until": "TIMESTAMP WITH TIME ZONE",
            }
            link_additions = {
                name: ddl
                for name, ddl in additions.items()
                if name not in existing_columns
            }

        need_event_constraint = False
        if event_table in table_names:
            constraint_names = {
                constraint.get("name")
                for constraint in inspector.get_unique_constraints(event_table)
            }
            need_event_constraint = constraint_name not in constraint_names

        if not link_additions and not need_event_constraint:
            return

        with ddl_transaction() as connection:
            for name, ddl in link_additions.items():
                connection.execute(
                    text(
                        f"ALTER TABLE {link_table} "
                        f"ADD COLUMN IF NOT EXISTS {name} {ddl}"
                    )
                )

            if need_event_constraint:
                connection.execute(
                    text(
                        f"""
                        DELETE FROM {event_table}
                        WHERE id IN (
                            SELECT id
                            FROM (
                                SELECT
                                    id,
                                    ROW_NUMBER() OVER (
                                        PARTITION BY magic_link_id, stop_id
                                        ORDER BY reached_at DESC, id DESC
                                    ) AS duplicate_rank
                                FROM {event_table}
                            ) ranked_events
                            WHERE duplicate_rank > 1
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        f"DO $$ BEGIN "
                        f"ALTER TABLE {event_table} ADD CONSTRAINT {constraint_name} "
                        "UNIQUE (magic_link_id, stop_id); "
                        "EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                    )
                )

        log.info(
            "driver magic-link schema patched (columns=%s unique_stop=%s)",
            sorted(link_additions),
            need_event_constraint,
        )
    except Exception:
        log.exception(
            "ensure_magic_link_hardening_schema failed — driver magic-link actions may be degraded"
        )


def ensure_port_time_and_sos_context_schema():
    """Additive fallback for server-authoritative port time and trip-bound SOS.

    Alembic remains canonical. This guard prevents an App Platform instance
    that starts before the migration job from querying columns that do not yet
    exist.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        statements = []

        if "crew_profiles" in tables:
            # Which vessel the crew member picked, which is not an assignment.
            # The shore leave card needs it to tell "waiting on my agent" from
            # "no agency runs this ship", and it has to survive a refresh.
            crew_columns = {
                column["name"] for column in inspector.get_columns("crew_profiles")
            }
            if "selected_vessel_id" not in crew_columns:
                statements.append(
                    "ALTER TABLE crew_profiles "
                    "ADD COLUMN IF NOT EXISTS selected_vessel_id INTEGER"
                )
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_crew_profiles_selected_vessel_id "
                "ON crew_profiles (selected_vessel_id)"
            )
            if "vessels" in tables:
                statements.append("""
                DO $$ BEGIN
                    ALTER TABLE crew_profiles
                    ADD CONSTRAINT fk_crew_profiles_selected_vessel_id
                    FOREIGN KEY (selected_vessel_id) REFERENCES vessels(id) ON DELETE SET NULL;
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                """)

        if "port_rules" in tables:
            port_columns = {column["name"] for column in inspector.get_columns("port_rules")}
            if "timezone" not in port_columns:
                statements.append(
                    "ALTER TABLE port_rules ADD COLUMN IF NOT EXISTS timezone VARCHAR(64)"
                )
            statements.append(
                """
                UPDATE port_rules
                   SET timezone = CASE
                       WHEN lower(port_name) LIKE '%dubai%' THEN 'Asia/Dubai'
                       ELSE 'Asia/Kolkata'
                   END
                 WHERE timezone IS NULL OR btrim(timezone) = ''
                """
            )

        if "crew_sos_requests" in tables:
            sos_columns = {
                column["name"] for column in inspector.get_columns("crew_sos_requests")
            }
            additions = {
                "cab_booking_id": "INTEGER",
                "trip_id": "VARCHAR(64)",
                "crew_email": "VARCHAR(255)",
                "sos_email": "VARCHAR(255)",
            }
            for name, ddl in additions.items():
                if name not in sos_columns:
                    statements.append(
                        f"ALTER TABLE crew_sos_requests ADD COLUMN IF NOT EXISTS {name} {ddl}"
                    )

            has_booking_fk = any(
                foreign_key.get("constrained_columns") == ["cab_booking_id"]
                and foreign_key.get("referred_table") == "cab_bookings"
                for foreign_key in inspector.get_foreign_keys("crew_sos_requests")
            )

            statements.extend([
                "CREATE INDEX IF NOT EXISTS ix_crew_sos_requests_cab_booking_id ON crew_sos_requests (cab_booking_id)",
                "CREATE INDEX IF NOT EXISTS ix_crew_sos_requests_trip_id ON crew_sos_requests (trip_id)",
            ])
            if not has_booking_fk and "cab_bookings" in tables:
                statements.append("""
                DO $$ BEGIN
                    ALTER TABLE crew_sos_requests
                    ADD CONSTRAINT fk_crew_sos_requests_cab_booking_id
                    FOREIGN KEY (cab_booking_id) REFERENCES cab_bookings(id) ON DELETE SET NULL;
                EXCEPTION WHEN duplicate_object THEN NULL; END $$;
                """)

        if not statements:
            return
        with ddl_transaction() as connection:
            for statement in statements:
                connection.execute(text(statement))
        log.info("port timezone and trip-bound SOS schema verified")
    except Exception:
        log.exception(
            "ensure_port_time_and_sos_context_schema failed — time/SOS features may be degraded"
        )


def ensure_vendor_commission_schema():
    """Additive safety net for deployments that start before Alembic runs."""
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "vendors" not in inspector.get_table_names():
            return

        columns = {column["name"] for column in inspector.get_columns("vendors")}
        statements = []
        if "commission_percentage" not in columns:
            statements.append(
                "ALTER TABLE vendors ADD COLUMN IF NOT EXISTS commission_percentage NUMERIC(5,2) DEFAULT 0 NOT NULL"
            )

        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_vendors_port_category_commission ON vendors (port_id, category, commission_percentage)"
        )

        with ddl_transaction() as connection:
            for statement in statements:
                connection.execute(text(statement))
        log.info("vendor commission schema verified")
    except Exception:
        log.exception(
            "ensure_vendor_commission_schema failed — vendor ranking may be degraded"
        )


def ensure_agent_dashboard_schema():
    """Additive safety net for the agent dashboard work.

    Mirrors migrations w1c2d3e4f5g6, x1c2d3e4f5g6 and y1c2d3e4f5g6, the same way
    the guards above mirror theirs. `create_all()` covers the two new tables
    because it creates missing tables, but it never ALTERs an existing one, so
    these columns would otherwise be absent until a pre-deploy job runs — and
    every incident query selects `incidents.vessel_id`.

    Alembic remains canonical. This exists so a deploy that starts before the
    migration job cannot leave the API returning UndefinedColumn.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        statements = []

        if "incidents" in tables:
            columns = {c["name"] for c in inspector.get_columns("incidents")}
            for name, ddl in (
                ("vessel_id", "INTEGER"),
                ("category", "VARCHAR(64)"),
                ("sub_category", "VARCHAR(64)"),
                ("severity", "VARCHAR(16)"),
                ("resolved_at", "TIMESTAMP"),
                ("cancelled_at", "TIMESTAMP"),
            ):
                if name not in columns:
                    statements.append(
                        f"ALTER TABLE incidents ADD COLUMN IF NOT EXISTS {name} {ddl}"
                    )
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_incidents_vessel_id ON incidents (vessel_id)"
            )
            # Same constraint the migration creates. Named so the two agree and
            # whichever runs second is a no-op.
            fks = {fk.get("name") for fk in inspector.get_foreign_keys("incidents")}
            if "fk_incidents_vessel_id" not in fks and "vessels" in tables:
                statements.append(
                    "ALTER TABLE incidents ADD CONSTRAINT fk_incidents_vessel_id "
                    "FOREIGN KEY (vessel_id) REFERENCES vessels (id) ON DELETE SET NULL"
                )
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_incidents_category ON incidents (category)"
            )

        if "agent_profiles" in tables:
            columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
            if "agency_logo_url" not in columns:
                statements.append(
                    "ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS agency_logo_url VARCHAR(512)"
                )

        if "vessels" in tables:
            vessel_columns = {c["name"]: c for c in inspector.get_columns("vessels")}
            columns = set(vessel_columns)
            if "shore_pass_valid_upto" not in columns:
                statements.append(
                    "ALTER TABLE vessels ADD COLUMN IF NOT EXISTS shore_pass_valid_upto TIMESTAMP WITH TIME ZONE"
                )
            if "agent_id" in vessel_columns and not vessel_columns["agent_id"].get("nullable", True):
                statements.append(
                    "ALTER TABLE vessels ALTER COLUMN agent_id DROP NOT NULL"
                )
            agent_fk = next(
                (fk for fk in inspector.get_foreign_keys("vessels") if fk.get("constrained_columns") == ["agent_id"]),
                None,
            )
            if (
                agent_fk
                and agent_fk.get("name")
                and str((agent_fk.get("options") or {}).get("ondelete", "")).upper() != "SET NULL"
            ):
                statements.extend([
                    f'ALTER TABLE vessels DROP CONSTRAINT "{agent_fk["name"]}"',
                    "ALTER TABLE vessels ADD CONSTRAINT fk_vessels_agent_id "
                    "FOREIGN KEY (agent_id) REFERENCES users (id) ON DELETE SET NULL",
                ])

        if "notifications" in tables:
            columns = {c["name"] for c in inspector.get_columns("notifications")}
            if "audience_type" not in columns:
                statements.append(
                    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS audience_type VARCHAR(32)"
                )
            if "target_vessel_ids" not in columns:
                statements.append(
                    "ALTER TABLE notifications ADD COLUMN IF NOT EXISTS target_vessel_ids JSON"
                )
            statements.append(
                "CREATE INDEX IF NOT EXISTS ix_notifications_audience_type "
                "ON notifications (audience_type)"
            )

        with ddl_transaction() as connection:
            for statement in statements:
                connection.execute(text(statement))

        # ALTER TYPE ... ADD VALUE cannot run inside a transaction block before
        # Postgres 12, so it goes on its own autocommit connection rather than
        # sharing the block above.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(
                text("ALTER TYPE incidentstatus ADD VALUE IF NOT EXISTS 'CANCELLED'")
            )

        log.info("agent dashboard schema verified (%s statement(s))", len(statements))
    except Exception:
        log.exception(
            "ensure_agent_dashboard_schema failed — incident, vessel and agent "
            "profile endpoints may return UndefinedColumn until migrations run"
        )


def ensure_cab_booking_vessel_id():
    """Additive safety net for cab_bookings.vessel_id.

    Mirrors migration j1k2l3m4n5o6. Without it a trip has no ship of its own and
    has to be attributed by asking who is on a manifest — which drags a crew
    member's whole trip history onto whichever vessel they most recently joined.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "cab_bookings" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("cab_bookings")}
        foreign_keys = inspector.get_foreign_keys("cab_bookings")
        has_vessel_foreign_key = any(
            foreign_key.get("constrained_columns") == ["vessel_id"]
            and foreign_key.get("referred_table") == "vessels"
            and foreign_key.get("referred_columns") == ["id"]
            for foreign_key in foreign_keys
        )
        with ddl_transaction() as connection:
            if "vessel_id" not in columns:
                connection.execute(text(
                    "ALTER TABLE cab_bookings ADD COLUMN IF NOT EXISTS vessel_id INTEGER"
                ))
            if not has_vessel_foreign_key:
                connection.execute(text(
                    "DO $$ BEGIN "
                    "ALTER TABLE cab_bookings "
                    "ADD CONSTRAINT fk_cab_bookings_vessel_id "
                    "FOREIGN KEY (vessel_id) REFERENCES vessels (id) ON DELETE SET NULL; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; "
                    "END $$"
                ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_cab_bookings_vessel_id "
                "ON cab_bookings (vessel_id)"
            ))
        log.info("cab booking vessel_id schema verified")
    except Exception:
        log.exception("ensure_cab_booking_vessel_id failed")


def ensure_agent_support_number():
    """Additive safety net for agent_profiles.support_number.

    Mirrors migration i0k1l2m3n4o5. Without it an agent saving their contact
    number would fall back to writing the shared port helpline, which is the
    behaviour that column exists to stop.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "agent_profiles" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
        if "support_number" in columns:
            return
        with ddl_transaction() as connection:
            connection.execute(text(
                "ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS support_number VARCHAR(32)"
            ))
        log.info("agent support number column added")
    except Exception:
        log.exception("ensure_agent_support_number failed")


def ensure_agent_agency_rules():
    """Additive safety net for agent_profiles.agency_rules.

    Mirrors migration k2l3m4n5o6p7. Without it an agent editing their crew rules
    falls back to writing the shared port_rules row, which is the behaviour this
    column exists to stop.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "agent_profiles" not in inspector.get_table_names():
            return
        columns = {c["name"] for c in inspector.get_columns("agent_profiles")}
        if "agency_rules" in columns:
            return
        with ddl_transaction() as connection:
            connection.execute(text(
                "ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS agency_rules JSON"
            ))
        log.info("agent agency rules column added")
    except Exception:
        log.exception("ensure_agent_agency_rules failed")


def ensure_historical_context_schema():
    """Additive boot guard for Release 1 event-context columns.

    ``create_all`` creates the two new tables, but it cannot alter existing
    event tables. Alembic remains canonical and performs the backfill and FK
    replacement; this guard only keeps the freshly deployed API compatible
    until the operator runs the migration.
    """
    log = logging.getLogger("app.startup")
    definitions = {
        "cab_bookings": {
            "vessel_call_id": "INTEGER",
            "crew_assignment_id": "INTEGER",
            "agency_id": "INTEGER",
            "port_id": "INTEGER",
            "context_resolution": "VARCHAR(32)",
        },
        "crew_sos_requests": {
            "vessel_call_id": "INTEGER",
            "vessel_id": "INTEGER",
            "agency_id": "INTEGER",
            "crew_assignment_id": "INTEGER",
            "port_id": "INTEGER",
            "context_resolution": "VARCHAR(32)",
        },
        "incidents": {
            "vessel_call_id": "INTEGER",
            "agency_id": "INTEGER",
            "crew_profile_id": "INTEGER",
            "crew_assignment_id": "INTEGER",
            "port_id": "INTEGER",
            "context_resolution": "VARCHAR(32)",
        },
    }
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        statements = []
        for table, columns in definitions.items():
            if table not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table)}
            for column, sql_type in columns.items():
                if column in existing:
                    continue
                statements.append(
                    f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS '
                    f'"{column}" {sql_type}'
                )
                if column.endswith("_id"):
                    statements.append(
                        f'CREATE INDEX IF NOT EXISTS "ix_{table}_{column}" '
                        f'ON "{table}" ("{column}")'
                    )
        with ddl_transaction() as connection:
            for statement in statements:
                connection.execute(text(statement))
        log.info("historical context schema verified (%s statement(s))", len(statements))
    except Exception:
        log.exception(
            "ensure_historical_context_schema failed — Release 1 migrations must run"
        )


def ensure_placeholder_helplines_removed():
    """Remove retired demo helplines when no pre-deploy job is configured.

    Alembic remains canonical. This idempotent startup guard mirrors the other
    deployment fallbacks in this module because the checked-in Procfile starts
    only the web process and does not run Alembic itself.
    """
    log = logging.getLogger("app.startup")
    placeholders = {
        "placeholder_one": "+91 1800-HEYPORTS",
        "placeholder_two": "+91 1800 425 1234",
        "agent_placeholder_one": "+91 9876543251",
        "agent_placeholder_two": "+91 98765403251",
        "agent_placeholder_three": "+91 9876542064",
    }
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        targets = []
        for table_name in ("cab_bookings", "port_rules"):
            if table_name not in tables:
                continue
            columns = {
                column["name"] for column in inspector.get_columns(table_name)
            }
            if "helpline_number" in columns:
                targets.append((table_name, "helpline_number", "helpline_number IN (:placeholder_one, :placeholder_two)"))
            if table_name == "cab_bookings" and "agent_number" in columns:
                targets.append((table_name, "agent_number", "agent_number IN (:agent_placeholder_one, :agent_placeholder_two, :agent_placeholder_three)"))

        cleared = 0
        with ddl_transaction() as connection:
            for table_name, column_name, predicate in targets:
                result = connection.execute(
                    text(
                        f"UPDATE {table_name} SET {column_name} = NULL WHERE {predicate}"
                    ),
                    placeholders,
                )
                cleared += max(result.rowcount or 0, 0)
        log.info("retired helpline cleanup verified (cleared=%s)", cleared)
    except Exception:
        log.exception(
            "ensure_placeholder_helplines_removed failed — retired demo helplines may remain visible"
        )


def ensure_port_identity_schema():
    """Backfill and enforce one canonical identity per port.

    Existing duplicate identities require an explicit reviewed merge through
    ``scripts/consolidate_ports.py``. The startup guard adds/backfills the
    column, but deliberately does not guess which duplicate should survive.
    """
    from app.services.port_identity import canonical_port_key, reconcile_port_identities

    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "ports" not in inspector.get_table_names():
            return
        columns = {column["name"] for column in inspector.get_columns("ports")}
        with ddl_transaction() as connection:
            if "canonical_key" not in columns:
                connection.execute(
                    text("ALTER TABLE ports ADD COLUMN canonical_key VARCHAR(255)")
                )
            repair = reconcile_port_identities(connection)
            rows = connection.execute(text("SELECT id, name, code FROM ports")).mappings()
            grouped = {}
            for row in rows:
                key = canonical_port_key(row["code"] or row["name"])
                grouped.setdefault(key, []).append(int(row["id"]))
                connection.execute(
                    text("UPDATE ports SET canonical_key=:key WHERE id=:id"),
                    {"key": key, "id": row["id"]},
                )

            duplicates = {key: ids for key, ids in grouped.items() if len(ids) > 1}
            if duplicates:
                log.error(
                    "canonical port identity not enforced; review and merge duplicate "
                    "port ids with scripts/consolidate_ports.py: %s",
                    duplicates,
                )
                return

            connection.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS "
                    "ix_ports_canonical_key ON ports (canonical_key)"
                )
            )
            if engine.dialect.name == "postgresql":
                connection.execute(
                    text("ALTER TABLE ports ALTER COLUMN canonical_key SET NOT NULL")
                )
        log.info("canonical port identity verified: %s", repair)
    except Exception:
        log.exception(
            "ensure_port_identity_schema failed — duplicate port identities may remain"
        )

def ensure_auth_attempt_columns():
    """Add the password-attempt counters to `users` and `drivers`.

    The same schema change as migration d0e1f2a3b4c5, repeated here because
    migrations do not run on this deployment.

    Every login route compared a password and returned 401 with no counter and
    no cooldown, so any account could be guessed at indefinitely. These three
    columns let one shared policy cover passwords as well as OTPs.

    Additive. The counter defaults to 0 server-side and both timestamps are
    nullable, so no existing row is read or rewritten.
    """
    log = logging.getLogger("app.startup")
    statements = {
        "users": {
            "failed_auth_attempts":
                "ALTER TABLE users ADD COLUMN failed_auth_attempts INTEGER NOT NULL DEFAULT 0",
            "last_auth_attempt_at":
                "ALTER TABLE users ADD COLUMN last_auth_attempt_at TIMESTAMPTZ",
            "auth_locked_until":
                "ALTER TABLE users ADD COLUMN auth_locked_until TIMESTAMPTZ",
        },
        "drivers": {
            "failed_auth_attempts":
                "ALTER TABLE drivers ADD COLUMN failed_auth_attempts INTEGER NOT NULL DEFAULT 0",
            "last_auth_attempt_at":
                "ALTER TABLE drivers ADD COLUMN last_auth_attempt_at TIMESTAMPTZ",
            "auth_locked_until":
                "ALTER TABLE drivers ADD COLUMN auth_locked_until TIMESTAMPTZ",
        },
    }
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        pending = []
        for table, wanted in statements.items():
            if table not in tables:
                continue  # fresh deploy: create_all built it with the columns
            present = {column["name"] for column in inspector.get_columns(table)}
            pending.extend(sql for name, sql in wanted.items() if name not in present)
        if not pending:
            return
        with ddl_transaction() as connection:
            for sql in pending:
                connection.execute(text(sql))
        log.info("auth attempt columns ensured (%d statement(s))", len(pending))
    except Exception:
        log.exception("ensure_auth_attempt_columns failed")


def ensure_ride_otp_attempt_columns():
    """Add the three ride-OTP counters to `cab_bookings`.

    The same schema change as migration b8c9d0e1f2a3, repeated here because
    migrations do not run on this deployment.

    `CrewProfile.ride_otp` is four digits and never rotates. The magic-link
    route already counted failed attempts on the DriverMagicLink row; the
    authenticated driver route counted nothing, so the assigned driver could
    walk all ten thousand values and keep the result for every future trip
    that crew member takes. These columns let one shared policy cover both.

    Additive. The counter defaults to 0 server-side and the two timestamps are
    nullable, so no existing row is read or rewritten -- a booking written
    before today starts from zero attempts, which is true of all of them.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "cab_bookings" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it with the columns already
        columns = {column["name"] for column in inspector.get_columns("cab_bookings")}
        # Written out literally rather than built from a loop: the guard
        # coverage test reads this file for `ALTER TABLE ... ADD COLUMN`, and
        # an interpolated column name is invisible to it. That is how these
        # three nearly shipped without being verified at startup.
        statements = {
            "otp_failed_attempts":
                "ALTER TABLE cab_bookings ADD COLUMN otp_failed_attempts INTEGER NOT NULL DEFAULT 0",
            "otp_last_attempt_at":
                "ALTER TABLE cab_bookings ADD COLUMN otp_last_attempt_at TIMESTAMPTZ",
            "otp_locked_until":
                "ALTER TABLE cab_bookings ADD COLUMN otp_locked_until TIMESTAMPTZ",
        }
        missing = {name: sql for name, sql in statements.items() if name not in columns}
        if not missing:
            return
        with ddl_transaction() as connection:
            for sql in missing.values():
                connection.execute(text(sql))
        log.info("cab_bookings ride-OTP attempt columns ensured: %s", ", ".join(missing))
    except Exception:
        log.exception("ensure_ride_otp_attempt_columns failed")


def ensure_assignment_public_ref():
    """Add `crew_assignments.public_ref` and its unique index.

    The same schema change as migration u2v3w4x5y6z7, repeated here because
    migrations do not run on this deployment. There is no PRE_DEPLOY job; the
    build command installs requirements and the run command starts uvicorn, so
    nothing ever calls `alembic upgrade`. `create_all()` adds missing *tables*
    and never ALTERs an existing one, and `ensure_alembic_baseline()` stamps
    head without applying any DDL -- so without this guard the column would not
    exist while the ORM model declares it, and the first SELECT against
    crew_assignments would fail.

    Written to match the migration exactly, so a database that gets the column
    from either route is identical. Nothing is backfilled: the column is
    nullable, every surface treats a null reference as "no reference", and
    issuing the values is `scripts/backfill_assignment_public_ref.py`, run
    deliberately rather than as a side effect of a deploy.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "crew_assignments" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it with the column already
        columns = {column["name"] for column in inspector.get_columns("crew_assignments")}
        indexes = {index["name"] for index in inspector.get_indexes("crew_assignments")}
        if "public_ref" in columns and "uq_crew_assignments_public_ref" in indexes:
            return
        with ddl_transaction() as connection:
            if "public_ref" not in columns:
                connection.execute(text(
                    "ALTER TABLE crew_assignments ADD COLUMN public_ref VARCHAR(40)"
                ))
            # NULLs are distinct in Postgres, so this is satisfied by a table
            # where every row is still null.
            if "uq_crew_assignments_public_ref" not in indexes:
                connection.execute(text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_crew_assignments_public_ref "
                    "ON crew_assignments (public_ref)"
                ))
        log.info("crew_assignments.public_ref ensured")
    except Exception:
        log.exception("ensure_assignment_public_ref failed")


def ensure_assignment_movement_columns():
    """Add `signed_on_at` / `signed_off_at` — HPID spec §8.

    Additive and nullable, so no row is read or rewritten. They record an
    agent confirming a movement, which the specification separates from a trip
    being booked or completed; a null means nobody has confirmed, which is
    exactly true of every row written before they existed.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "crew_assignments" not in inspector.get_table_names():
            return  # fresh deploy: create_all built them already
        existing = {c["name"] for c in inspector.get_columns("crew_assignments")}
        missing = [
            name for name in ("signed_on_at", "signed_off_at")
            if name not in existing
        ]
        if not missing:
            return
        with ddl_transaction() as connection:
            for name in missing:
                connection.execute(text(
                    f"ALTER TABLE crew_assignments ADD COLUMN IF NOT EXISTS "
                    f"{name} TIMESTAMPTZ"
                ))
        log.info("crew_assignments movement columns ensured: %s", ", ".join(missing))
    except Exception:
        log.exception("ensure_assignment_movement_columns failed")


def ensure_shore_pass_profile_nullable():
    """Drop NOT NULL from `shore_passes.crew_profile_id` — Q1, HPID spec §7.

    An agent can mark an unregistered crew member eligible, and until this
    column was nullable no pass could be written for them: the schema refused
    the exact case the owner's answer describes.

    Purely permissive. Dropping a NOT NULL constraint reads no row, rewrites
    no row and cannot fail on existing data; every pass that exists keeps its
    profile. Guarded so a database that already allows nulls is left alone.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "shore_passes" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it nullable already
        columns = {c["name"]: c for c in inspector.get_columns("shore_passes")}
        column = columns.get("crew_profile_id")
        if column is None or column.get("nullable"):
            return
        with ddl_transaction() as connection:
            connection.execute(text(
                "ALTER TABLE shore_passes ALTER COLUMN crew_profile_id DROP NOT NULL"
            ))
        log.info("shore_passes.crew_profile_id is now nullable")
    except Exception:
        log.exception("ensure_shore_pass_profile_nullable failed")


def ensure_assignment_source():
    """Add `crew_assignments.source` — HPID specification §11.

    Same reasoning as the guard above: migrations do not run here and
    `create_all` never ALTERs. The default is MANIFEST rather than anything
    meaning "unknown", because every row that existed before this column was
    created from an agent's manifest; calling them unknown would lose a fact
    the schema already implies.

    NOT NULL with a default, so the backfill is the default itself and no row
    is read or rewritten.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "crew_assignments" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it with the column already
        columns = {column["name"] for column in inspector.get_columns("crew_assignments")}
        if "source" in columns:
            return
        with ddl_transaction() as connection:
            connection.execute(text(
                "ALTER TABLE crew_assignments ADD COLUMN source VARCHAR(32) "
                "NOT NULL DEFAULT 'MANIFEST'"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_crew_assignments_source "
                "ON crew_assignments (source)"
            ))
        log.info("crew_assignments.source ensured")
    except Exception:
        log.exception("ensure_assignment_source failed")


def ensure_magic_link_lifecycle():
    """Add `expires_at` and `revoked_at` to driver_magic_links.

    The same schema change as migration v3w4x5y6z7a8. Repeated here for the
    same reason as ensure_assignment_public_ref: nothing on this deployment
    calls `alembic upgrade`, and create_all() never ALTERs an existing table.

    Writes no row. A link created before these columns existed has a null
    `expires_at`, which `link_expires_at()` reads as creation plus the TTL, so
    those links expire on the same rule without being rewritten.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "driver_magic_links" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it with both columns
        columns = {column["name"] for column in inspector.get_columns("driver_magic_links")}
        missing = [c for c in ("expires_at", "revoked_at") if c not in columns]
        if not missing:
            return
        with ddl_transaction() as connection:
            for column in missing:
                connection.execute(text(
                    f"ALTER TABLE driver_magic_links ADD COLUMN {column} TIMESTAMPTZ"
                ))
        log.info("driver_magic_links lifecycle columns ensured: %s", ", ".join(missing))
    except Exception:
        log.exception("ensure_magic_link_lifecycle failed")


def ensure_incident_occurred_at():
    """Add `incidents.occurred_at`.

    The same schema change as migration x5y6z7a8b9c0, repeated here for the
    same reason as the other guards: nothing on this deployment calls
    `alembic upgrade`, and create_all() never ALTERs an existing table.

    Writes no row. Existing incidents keep a null occurrence on purpose --
    there is nowhere to recover it from, and the event dictionary reports those
    rows as carrying a reporting time rather than inventing one.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "incidents" not in inspector.get_table_names():
            return  # fresh deploy: create_all built it with the column
        columns = {column["name"] for column in inspector.get_columns("incidents")}
        indexes = {index["name"] for index in inspector.get_indexes("incidents")}
        if "occurred_at" in columns and "ix_incidents_occurred_at" in indexes:
            return
        with ddl_transaction() as connection:
            if "occurred_at" not in columns:
                connection.execute(text(
                    "ALTER TABLE incidents ADD COLUMN occurred_at TIMESTAMPTZ"
                ))
            if "ix_incidents_occurred_at" not in indexes:
                connection.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_incidents_occurred_at "
                    "ON incidents (occurred_at)"
                ))
        log.info("incidents.occurred_at ensured")
    except Exception:
        log.exception("ensure_incident_occurred_at failed")


def ensure_company_domain_schema():
    """The overlap invariant on approved vessel-role claims.

    Document 2 section 6: the same role on the same vessel must not be held
    over overlapping periods, "database-level exclusion or serializable
    locking... ORM checks alone are insufficient", because two simultaneous
    approvals must not both succeed.

    create_all() builds the four tables but cannot express EXCLUDE, so the
    constraint is added here. Half-open tstzrange, so a handover at the
    boundary instant is not an overlap, and scoped to live approved rows: a
    pending or revoked claim must not block a real one.

    Writes no row. Skips quietly where btree_gist is unavailable, logging it,
    because the constraint is an invariant to add rather than a reason to
    refuse to boot -- but an environment without it has no overlap protection
    and the log line is how that gets noticed.
    """
    log = logging.getLogger("app.startup")
    try:
        inspector = inspect(engine)
        if "vessel_role_claims" not in inspector.get_table_names():
            return
        with ddl_transaction() as connection:
            existing = connection.execute(text(
                "SELECT 1 FROM pg_constraint WHERE conname = :name"
            ), {"name": "uq_vessel_role_claims_no_overlap"}).first()
            if existing:
                return
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            connection.execute(text("""
                ALTER TABLE vessel_role_claims
                ADD CONSTRAINT uq_vessel_role_claims_no_overlap
                EXCLUDE USING gist (
                    vessel_id WITH =,
                    role WITH =,
                    tstzrange(effective_start, effective_end, '[)') WITH &&
                ) WHERE (status = 'APPROVED' AND revoked_at IS NULL)
            """))
        log.info("vessel_role_claims overlap constraint ensured")
    except Exception:
        log.exception(
            "ensure_company_domain_schema failed — approved vessel-role claims "
            "are NOT protected against overlap on this database"
        )


def ensure_alembic_baseline():
    """Make Alembic usable against a database that was never stamped.

    Every deployment so far built its schema with `create_all()` and never ran
    Alembic, so there is no `alembic_version` row. Alembic cannot fix that
    itself: this project's history is not replayable from base — the earliest
    revision alters `rfqs` / `rfq_quotes`, tables from a retired product, and
    dies with UndefinedTable on an empty database.

    Stamping records that the schema `create_all()` produced is equivalent to
    head, which is what lets future revisions apply as ordinary deltas. It runs
    no migration DDL, so it cannot fail partway.

    A pre-deploy job (`python -m app.db.migrate`) is the intended place for
    applying deltas. This is only the safety net that guarantees the baseline
    exists even if that job is never configured.
    """
    log = logging.getLogger("app.startup")
    try:
        from app.db.migrate import baseline_if_unstamped

        if baseline_if_unstamped():
            log.info("alembic baseline stamped at head")
    except Exception:
        log.exception(
            "ensure_alembic_baseline failed — Alembic migrations will not apply "
            "until this database is stamped"
        )


# --- Routes ---
app.include_router(routes_auth.router,    prefix="/api/v1/auth",    tags=["authentication"])
app.include_router(routes_contact.router, prefix="/api/v1/contact", tags=["contact"])
app.include_router(routes_early_access.router, prefix="/api/v1/early-access", tags=["early-access"])
app.include_router(routes_files.router,   prefix="/api/v1/files",   tags=["files"])
app.include_router(routes_users.router,   prefix="/api/v1/users",   tags=["users"])
app.include_router(vendor_router,         prefix="/api/v1",         tags=["vendor"])
app.include_router(rfq_router, prefix="/api/v1", tags=["rfqs"])
app.include_router(routes_quotes.router, prefix="/api/v1", tags=["quotes"])
app.include_router(routes_orders.router,  prefix="/api/v1",         tags=["orders"])
app.include_router(registration.router,   prefix="/api/v1/registration", tags=["registration"])
app.include_router(routes_crew.router,     prefix="/api/v1/crew",         tags=["crew"])
app.include_router(routes_pubs.router,     prefix="/api/v1/pubs",         tags=["pubs"])
app.include_router(routes_hotels.router,   prefix="/api/v1/hotels",       tags=["hotels"])
app.include_router(routes_sightseeing.router, prefix="/api/v1/sightseeing", tags=["sightseeing"])
app.include_router(routes_restaurants.router, prefix="/api/v1/restaurants",   tags=["restaurants"])
app.include_router(routes_vessels.router,     prefix="/api/v1/vessels",       tags=["vessels"])
app.include_router(
    routes_shipping_company.router,
    prefix="/api/v1/shipping-company",
    tags=["shipping-company"],
)
from app.api.v1 import routes_company_staff

app.include_router(
    routes_company_staff.router,
    prefix="/api/v1/shipping-company",
    tags=["company-staff"],
)
app.include_router(
    routes_company_review.router, prefix="/api/v1/superadmin", tags=["company-review"]
)
app.include_router(
    routes_vendor_review.router, prefix="/api/v1/superadmin", tags=["vendor-review"]
)
app.include_router(routes_privacy.router, prefix="/api/v1", tags=["privacy"])
app.include_router(routes_privacy.admin_router, prefix="/api/v1/superadmin", tags=["privacy-review"])
app.include_router(routes_trips.router,       prefix="/api/v1/trips",         tags=["trips"])
app.include_router(routes_incidents.router, prefix="/api/v1/incidents", tags=["incidents"])
app.include_router(routes_agents.router, prefix="/api/v1/agents", tags=["agents"])
app.include_router(routes_aggregators.router, prefix="/api/v1/aggregators", tags=["aggregators"])
app.include_router(routes_bookings.router, prefix="/api/v1/bookings", tags=["bookings"])
app.include_router(routes_ports.router, prefix="/api/v1/ports", tags=["ports"])
app.include_router(routes_drivers.router, prefix="/api/v1/drivers", tags=["drivers"])
app.include_router(routes_chat.router, prefix="/api/v1/chat", tags=["chat"])
app.include_router(routes_superadmin.router, prefix="/api/v1/superadmin", tags=["superadmin"])
app.include_router(routes_pricing_controls.router, prefix="/api/v1/superadmin", tags=["pricing-controls"])
app.include_router(routes_chat_moderation.router, prefix="/api/v1/superadmin", tags=["chat-moderation"])
app.include_router(routes_reviews.router, prefix="/api/v1/reviews", tags=["reviews"])
app.include_router(routes_notifications.router, prefix="/api/v1/notifications", tags=["notifications"])
app.include_router(routes_sos.router, prefix="/api/v1/sos", tags=["sos"])
app.include_router(routes_itinerary.router, prefix="/api/v1/itinerary", tags=["itinerary"])
app.include_router(routes_facilities.router, prefix="/api/v1/facilities", tags=["facilities"])
app.include_router(routes_expenses.router, prefix="/api/v1/crew/expense-bills", tags=["expenses"])
app.include_router(routes_payments.router, prefix="/api/v1/crew/payments", tags=["payments"])


# --- Health checks & root ---
@app.get("/")
def read_root():
    return {"message": "Welcome to OneMarinex API"}

@app.get("/health")
def health_check():
    return {"status": "healthy"}

# --- Static uploads ---
os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

from app.api.v1 import routes_agency_transfers

app.include_router(
    routes_agency_transfers.router,
    prefix="/api/v1/agency-transfers",
    tags=["agency-transfers"],
)
app.include_router(
    routes_agency_transfers.public_router,
    prefix="/api/v1/agency-transfer-trip",
    tags=["agency-transfer-trip"],
)

from app.services.collection_pages import collection_paging

app.middleware("http")(collection_paging)

from app.api.v1 import routes_company_intelligence
app.include_router(routes_company_intelligence.router, prefix="/api/v1/shipping-company/intelligence", tags=["company-intelligence"])
from app.api.v1 import routes_company_administration
app.include_router(routes_company_administration.router, prefix='/api/v1/shipping-company/administration', tags=['company-administration'])
from app.api.v1 import routes_company_onboarding
app.include_router(routes_company_onboarding.router, prefix='/api/v1/superadmin/company-onboarding', tags=['company-onboarding'])
from app.api.v1 import routes_welfare
app.include_router(routes_welfare.crew_router, prefix='/api/v1/crew', tags=['crew-welfare'])
app.include_router(routes_welfare.company_router, prefix='/api/v1/shipping-company/welfare-intelligence', tags=['company-welfare'])
app.include_router(routes_welfare.admin_router, prefix='/api/v1/superadmin/welfare-intelligence', tags=['welfare-intelligence'])
from app.api.v1 import routes_company_disclosure
app.include_router(routes_company_disclosure.company_router, prefix='/api/v1/shipping-company', tags=['company-disclosure'])
app.include_router(routes_company_disclosure.admin_router, prefix='/api/v1/superadmin', tags=['company-disclosure-review'])
from app.api.v1 import routes_company_reports
app.include_router(routes_company_reports.router, prefix='/api/v1/shipping-company/report-centre', tags=['company-reports'])

from app.api.v1 import routes_company_fleet
app.include_router(routes_company_fleet.router, prefix="/api/v1/shipping-company/fleet", tags=["Company fleet"])
app.include_router(routes_company_fleet.admin_router, prefix="/api/v1/superadmin", tags=["Fleet change review"])

from app.services.company_private_responses import company_private_responses
app.middleware("http")(company_private_responses)
