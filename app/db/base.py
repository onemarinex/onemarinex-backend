# app/db/base.py
from sqlalchemy.orm import declarative_base

# Shared declarative base
Base = declarative_base()

# Import models so they're registered with Base.metadata
# (No engine/session imports here to avoid circulars)
from app.db.models import user            # noqa: F401
from app.db.models import vendor_profile  # noqa: F401
from app.db.models import crew_profile    # noqa: F401
from app.db.models import client_profile  # noqa: F401
from app.db.models import agent_profile   # noqa: F401
from app.db.models import aggregator_profile # noqa: F401
from app.db.models import vessel          # noqa: F401
from app.db.models import vessel_crew     # noqa: F401
from app.db.models import rfq             # noqa: F401
from app.db.models import rfq_quote       # noqa: F401
from app.db.models import shore_pass      # noqa: F401
from app.db.models import cab_booking     # noqa: F401
from app.db.models import driver          # noqa: F401
from app.db.models import cab_pricing     # noqa: F401
from app.db.models import file_asset      # noqa: F401
from app.db.models import pub             # noqa: F401
from app.db.models import restaurant      # noqa: F401
from app.db.models import hotels          # noqa: F401
from app.db.models import incident        # noqa: F401
from app.db.models import password_reset  # noqa: F401
from app.db.models.order import Order     # noqa: F401
from app.db.models.order_event import OrderEvent  # noqa: F401
from app.db.models.port import Port       # noqa: F401
from app.db.models.port_rule import PortRule # noqa: F401
from app.db.models import contact_message # noqa: F401
from app.db.models import early_access      # noqa: F401
from app.db.models import port_service_request  # noqa: F401
from app.db.models.facility_scan import FacilityScan # noqa: F401
