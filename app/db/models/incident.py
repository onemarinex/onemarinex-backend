from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from datetime import datetime
import enum

from app.db.base import Base
from app.db.types import UTCDateTime

class IncidentStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    INVESTIGATING = "INVESTIGATING"
    RESOLVED = "RESOLVED"
    # False alarms and duplicates. The detail screen has a "Cancel Incident"
    # action, which previously had no status to move to.
    CANCELLED = "CANCELLED"

class IncidentType(str, enum.Enum):
    CREW = "CREW"
    DRIVER = "DRIVER"

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(String(64), unique=True, index=True) # e.g. INC-001
    aggregator_id = Column(Integer, ForeignKey("aggregator_profiles.id", ondelete="CASCADE"), nullable=True)
    port_name = Column(String(128), nullable=True) # Port where incident occurred
    
    type = Column(SQLEnum(IncidentType), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    
    status = Column(SQLEnum(IncidentStatus), default=IncidentStatus.ACTIVE)
    
    # Reporter/Context info from Screenshot 1 & 2
    reporter_name = Column(String(255), nullable=True) # e.g. John
    reporter_role = Column(String(255), nullable=True) # e.g. Chief Officer
    reporter_id = Column(String(64), nullable=True)   # e.g. HPID-19383-9282
    
    trip_id = Column(String(64), nullable=True)      # e.g. TR 101

    # An incident belongs to a ship. Without this the only route from an
    # incident to a vessel was the reporter's HPID, which is indirect and fails
    # entirely for crew who have not registered an account.
    vessel_id = Column(Integer, ForeignKey("vessels.id", ondelete="SET NULL"), nullable=True, index=True)
    vessel_call_id = Column(
        Integer, ForeignKey("vessel_calls.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    agency_id = Column(
        Integer, ForeignKey("agent_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    crew_profile_id = Column(
        Integer, ForeignKey("crew_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    crew_assignment_id = Column(
        Integer,
        ForeignKey("crew_assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    port_id = Column(
        Integer, ForeignKey("ports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    context_resolution = Column(String(32), nullable=True)

    # See app/services/incident_taxonomy.py — six categories, each with
    # sub-categories. Stored as values, not labels, so wording can change.
    category = Column(String(64), nullable=True, index=True)
    sub_category = Column(String(64), nullable=True)
    severity = Column(String(16), nullable=True)  # high | medium | low

    # When the incident actually happened, as reported by whoever raised it.
    # Distinct from created_at, which is when we were told. The two differ by
    # however long it took someone to open the app, and at a management
    # handover that gap decides which company the incident belongs to.
    #
    # Nullable, and null on every row written before this column existed. Those
    # rows are not backfilled -- there is nowhere to recover the occurrence
    # from -- so they keep reporting-time quality and the event dictionary says
    # so per row rather than per family.
    occurred_at = Column(UTCDateTime, nullable=True, index=True)

    resolved_at = Column(UTCDateTime, nullable=True)
    cancelled_at = Column(UTCDateTime, nullable=True)

    created_at = Column(UTCDateTime, default=datetime.utcnow)
    updated_at = Column(UTCDateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    aggregator = relationship("AggregatorProfile", backref="incidents")
    vessel = relationship("Vessel")
    vessel_call = relationship("VesselCall")
    crew_profile = relationship("CrewProfile")
    crew_assignment = relationship("CrewAssignment")
    notes = relationship("IncidentNote", back_populates="incident", cascade="all, delete-orphan")
    timeline = relationship(
        "IncidentTimelineEvent",
        back_populates="incident",
        cascade="all, delete-orphan",
        order_by="IncidentTimelineEvent.event_time",
    )

class IncidentTimelineEvent(Base):
    """One entry on an incident's timeline.

    The design interleaves automatic events ("Incident Received", "Under
    Investigation") with entries an agent adds by hand. `incident_notes` cannot
    represent that — it has no event type, no ordering intent, and no way to
    tell a system event from a human one.
    """

    __tablename__ = "incident_timeline_events"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True)

    # "system" for automatic entries, "agent" for ones a person added.
    source = Column(String(16), nullable=False, default="system")
    event_type = Column(String(64), nullable=False)   # reported | received | investigating | resolved | cancelled | note
    label = Column(String(255), nullable=False)
    detail = Column(Text, nullable=True)

    actor_name = Column(String(255), nullable=True)
    event_time = Column(UTCDateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(UTCDateTime, default=datetime.utcnow)

    incident = relationship("Incident", back_populates="timeline")


class IncidentNote(Base):
    __tablename__ = "incident_notes"

    id = Column(Integer, primary_key=True, index=True)
    incident_id = Column(Integer, ForeignKey("incidents.id", ondelete="CASCADE"))
    author_name = Column(String(255), nullable=True)
    note = Column(Text, nullable=False)
    created_at = Column(UTCDateTime, default=datetime.utcnow)

    # Relationships
    incident = relationship("Incident", back_populates="notes")
