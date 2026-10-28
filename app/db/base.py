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
from app.db.models import vessel_call     # noqa: F401
from app.db.models import crew_assignment # noqa: F401
from app.db.models import report_snapshot  # noqa: F401
from app.db.models import event_context_reconciliation  # noqa: F401
from app.db.models import crew_identity_conflict  # noqa: F401
from app.db.models import rfq             # noqa: F401
from app.db.models import rfq_quote       # noqa: F401
from app.db.models import shore_pass      # noqa: F401
from app.db.models import cab_booking     # noqa: F401
from app.db.models import booking_timeline # noqa: F401
from app.db.models import driver          # noqa: F401
from app.db.models import cab_pricing     # noqa: F401
from app.db.models import file_asset      # noqa: F401
from app.db.models import pub             # noqa: F401
from app.db.models import restaurant      # noqa: F401
from app.db.models import hotels          # noqa: F401
from app.db.models import sightseeing     # noqa: F401
from app.db.models import incident        # noqa: F401
from app.db.models import password_reset  # noqa: F401
from app.db.models.order import Order     # noqa: F401
from app.db.models.order_event import OrderEvent  # noqa: F401
from app.db.models.port import Port       # noqa: F401
from app.db.models.port_rule import PortRule # noqa: F401
from app.db.models import contact_message # noqa: F401
from app.db.models import early_access      # noqa: F401
from app.db.models import login_event      # noqa: F401
from app.db.models import agent_roster_event  # noqa: F401
from app.db.models import port_service_request  # noqa: F401
from app.db.models.facility_scan import FacilityScan # noqa: F401
from app.db.models.chat import ChatMessage # noqa: F401
from app.db.models.venue_review import VenueReview # noqa: F401
from app.db.models.notification import Notification # noqa: F401
from app.db.models.notification_read import NotificationRead # noqa: F401
from app.db.models.crew_sos import CrewSos, CrewSosTimelineEvent, CrewSosNote # noqa: F401
from app.db.models import pricing_controls # noqa: F401
from app.db.models import driver_magic_link # noqa: F401
from app.db.models import pricing_controls # noqa: F401
from app.db.models import vendor_tag # noqa: F401
from app.db.models import vendors  # noqa: F401
from app.db.models import booking_provider_rejection # noqa: F401
from app.db.models import booking_review # noqa: F401
from app.db.models import booking_invitation # noqa: F401
from app.db.models import expense_bill  # noqa: F401
from app.db.models import payment  # noqa: F401
from app.db.models import email_verification  # noqa: F401
from app.db.models.shore_pass_reminder import ShorePassReminder # noqa: F401
from app.db.models.chat_restricted_word import ChatRestrictedWord # noqa: F401
from app.db.models.chat_moderation_event import ChatModerationEvent # noqa: F401
from app.db.models.chat_moderation_setting import ChatModerationSetting # noqa: F401
from app.db.models import shipping_company  # noqa: F401
from app.db.models.data_deletion import DataDeletionRequest  # noqa: F401

from app.db.models import company_invitation  # noqa: F401
from app.db.models import agency_transfer  # noqa: F401
