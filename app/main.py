from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.models import OAuthFlows as OAuthFlowsModel
from fastapi.openapi.utils import get_openapi
import os

from app.core.config import settings
from app.db.session import engine
from app.db.base import Base

from app.api.v1 import routes_auth, routes_contact, routes_files, routes_users, registration, routes_crew, routes_pubs, routes_hotels, routes_restaurants, routes_incidents, routes_ports, routes_drivers, routes_early_access, routes_chat, routes_superadmin, routes_reviews, routes_sightseeing, routes_notifications, routes_sos

from app.api.v1.routes_vendor import router as vendor_router
from app.api.v1.routes_rfqs import router as rfq_router
from app.api.v1 import routes_quotes 
from app.api.v1 import routes_orders 
from app.api.v1 import routes_vessels
from app.api.v1 import routes_trips
from app.api.v1 import routes_agents
from app.api.v1 import routes_aggregators

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
    "https://heyports-56we8.ondigitalocean.app",  # Production frontend
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Startup event: ensure tables exist ---
@app.on_event("startup")
def on_startup():
    # Base is already linked to all models via app/db/base.py imports
    Base.metadata.create_all(bind=engine)

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
app.include_router(routes_trips.router,       prefix="/api/v1/trips",         tags=["trips"])
app.include_router(routes_incidents.router, prefix="/api/v1/incidents", tags=["incidents"])
app.include_router(routes_agents.router, prefix="/api/v1/agents", tags=["agents"])
app.include_router(routes_aggregators.router, prefix="/api/v1/aggregators", tags=["aggregators"])
app.include_router(routes_ports.router, prefix="/api/v1/ports", tags=["ports"])
app.include_router(routes_drivers.router, prefix="/api/v1/drivers", tags=["drivers"])
app.include_router(routes_chat.router, prefix="/api/v1/chat", tags=["chat"])
app.include_router(routes_superadmin.router, prefix="/api/v1/superadmin", tags=["superadmin"])
app.include_router(routes_reviews.router, prefix="/api/v1/reviews", tags=["reviews"])
app.include_router(routes_notifications.router, prefix="/api/v1/notifications", tags=["notifications"])
app.include_router(routes_sos.router, prefix="/api/v1/sos", tags=["sos"])


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
