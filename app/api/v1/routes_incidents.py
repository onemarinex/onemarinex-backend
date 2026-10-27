from fastapi import Response
from app.services.result_limits import capped_response
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone
import uuid
from app.db.session import get_db
from app.db.models.incident import Incident, IncidentNote, IncidentStatus, IncidentType
from app.db.models.crew_assignment import CrewAssignment
from app.api.v1.routes_auth import get_current_user
from app.services.vessel_lifecycle import belongs_to_call, current_call_for
from pydantic import BaseModel, Field, field_validator

router = APIRouter()

class IncidentNoteBase(BaseModel):
    note: str
    author_name: Optional[str] = None

class IncidentNoteResponse(IncidentNoteBase):
    id: int
    created_at: datetime
    class Config:
        from_attributes = True

# Cab aggregators have no incident surface, by product decision (owner,
# 26 October 2026). The screens were deleted after being found unrouted since
# June, and these endpoints carried matching branches that let an aggregator
# create an incident only they could then read -- a self-contained loop no
# other role could see into, fed by nothing.
#
# The branches are gone, so an aggregator now gets 403 rather than an empty
# list, which is the honest answer: there is nothing here for them. A crew
# member's incident about a cab trip belongs to the agency that manages the
# vessel, which is where the crew branch already routes it.
#
# `Incident.aggregator_id` is deliberately left on the model. Removing it would
# be a schema change that destroys the provenance of any row that still carries
# one, and nothing reads it now.


class IncidentBase(BaseModel):
    type: IncidentType
    title: str
    description: str
    # The crew form has always sent `category`; until now the schema had no such
    # field so Pydantic silently dropped it and nothing was ever recorded.
    category: Optional[str] = None
    sub_category: Optional[str] = None
    severity: Optional[str] = None
    reporter_name: Optional[str] = None
    reporter_role: Optional[str] = None
    reporter_id: Optional[str] = None
    trip_id: Optional[str] = None
    port_name: Optional[str] = None
    # When it happened, not when it was reported. Optional: a crew member who
    # does not know, or does not say, leaves it null rather than being pushed
    # into inventing one, and the dashboard treats that row as reporting-time.
    occurred_at: Optional[datetime] = None

    @field_validator("occurred_at")
    @classmethod
    def _occurrence_is_not_in_the_future(cls, value):
        """An incident cannot be reported before it happens.

        A small tolerance for the reporter's clock, which is a phone at a port
        and not synchronised to anything in particular. Without the check a
        mistyped year lands an incident in 2036, where it sits outside every
        company's tenure and is invisible to all of them.
        """
        if value is None:
            return value
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if moment > datetime.now(timezone.utc) + timedelta(minutes=15):
            raise ValueError("occurred_at cannot be in the future")
        return value

class IncidentCreate(IncidentBase):
    # Recipient/aggregator is always derived from the authenticated actor.
    crew_assignment_id: Optional[int] = Field(default=None, gt=0)

class IncidentResponse(IncidentBase):
    id: int
    incident_id: str
    status: IncidentStatus
    vessel_id: Optional[int] = None
    vessel_name: Optional[str] = None
    category_label: Optional[str] = None
    sub_category_label: Optional[str] = None
    resolved_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    aggregator_id: Optional[int] = None
    created_at: datetime
    updated_at: datetime
    routing_status: str = "assigned"
    routing_message: Optional[str] = None
    notes: List[IncidentNoteResponse] = []

    class Config:
        from_attributes = True

# --- Crew Safety Center helpers -------------------------------------------

def _agent_incident_filter(db: Session, agent_user_id: int):
    """Which incidents have immutable ownership by this agent's agency."""
    from app.db.models.agent_profile import AgentProfile
    agency_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == agent_user_id
    ).scalar()
    # No current-profile fallback: rows without an immutable agency remain
    # superadmin-only until they are manually reconciled.
    return Incident.agency_id == agency_id if agency_id is not None else None


def _record_timeline(db: Session, incident: Incident, event_type: str, label: str,
                     detail: Optional[str] = None, source: str = "system",
                     actor_name: Optional[str] = None) -> None:
    """Append one entry to an incident's timeline."""
    from app.db.models.incident import IncidentTimelineEvent

    db.add(IncidentTimelineEvent(
        incident_id=incident.id,
        source=source,
        event_type=event_type,
        label=label,
        detail=detail,
        actor_name=actor_name,
    ))


def _serialize_incident(db: Session, incident: Incident) -> dict:
    from app.services import incident_taxonomy as tax
    from app.db.models.agent_profile import AgentProfile
    from app.db.models.vessel import Vessel
    from app.db.models.vessel_call import VesselCall

    vessel_name = None
    responsible_agent_id = None
    if incident.vessel_call_id:
        call = db.query(VesselCall).filter(VesselCall.id == incident.vessel_call_id).first()
        if call:
            vessel_name = call.vessel_name
    if incident.agency_id:
        responsible_agent_id = db.query(AgentProfile.user_id).filter(
            AgentProfile.id == incident.agency_id
        ).scalar()
    if incident.vessel_id and vessel_name is None:
        v = db.query(Vessel.name, Vessel.agent_id).filter(
            Vessel.id == incident.vessel_id
        ).first()
        vessel_name = v[0] if v else None
        responsible_agent_id = responsible_agent_id or (v[1] if v else None)

    routing_status = "assigned" if responsible_agent_id else "superadmin_follow_up"
    routing_message = None if responsible_agent_id else (
        "No responsible shipping agent is currently assigned. The incident "
        "was retained for superadmin follow-up."
    )

    return {
        "id": incident.id,
        "incident_id": incident.incident_id,
        "type": incident.type,
        "title": incident.title,
        "description": incident.description,
        "status": incident.status,
        "category": incident.category,
        "sub_category": incident.sub_category,
        "severity": incident.severity,
        "category_label": tax.category_label(incident.category),
        "sub_category_label": tax.sub_category_label(incident.category, incident.sub_category),
        "reporter_name": incident.reporter_name,
        "reporter_role": incident.reporter_role,
        "reporter_id": incident.reporter_id,
        "trip_id": incident.trip_id,
        "port_name": incident.port_name,
        "vessel_id": incident.vessel_id,
        "vessel_name": vessel_name,
        "resolved_at": incident.resolved_at,
        "cancelled_at": incident.cancelled_at,
        "created_at": incident.created_at,
        # Required by IncidentResponse, which the status endpoint returns
        # through. Omitting it made every status change commit and then fail
        # response validation with a 500, so the client saw an error for a
        # change that had actually been saved.
        "updated_at": incident.updated_at,
        "routing_status": routing_status,
        "routing_message": routing_message,
    }


@router.get("/categories")
def list_incident_categories():
    """The category tree, served from one place so every form agrees.

    The rank list taught us what happens when a taxonomy is copied into each
    screen instead: three copies, two value formats, and unusable reports.
    """
    from app.services import incident_taxonomy as tax

    return {"categories": tax.INCIDENT_CATEGORIES, "severities": tax.SEVERITIES}


@router.get("/eligible-trips")
def list_eligible_incident_trips(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Trips the signed-in crew member may explicitly attach to an incident."""
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can select an incident trip")

    from app.db.models.cab_booking import CabBooking
    from app.db.models.crew_profile import CrewProfile

    crew = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not crew:
        return {"trips": []}
    rows = (
        db.query(CabBooking)
        .filter(CabBooking.crew_id == crew.id)
        .order_by(CabBooking.created_at.desc(), CabBooking.id.desc())
        .limit(50)
        .all()
    )
    return {
        "trips": [
            {
                "trip_id": row.booking_id,
                "status": row.status.value if hasattr(row.status, "value") else row.status,
                "pickup_address": row.pickup_address,
                "drop_address": row.drop_address,
                "created_at": row.created_at,
            }
            for row in rows
        ]
    }


@router.get("/monitoring")
async def get_incident_monitoring(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    base_query = db.query(Incident)
    
    if current_user.role == "agent":
        ownership = _agent_incident_filter(db, current_user.id)
        if ownership is None:
            return {
                "active_crew": 0,
                "active_aggregator": 0,
                "active_incidents": [],
                "resolved_incidents": []
            }
        base_query = base_query.filter(ownership)
    
    else:
        raise HTTPException(status_code=403, detail="Not authorized")

    # Deliberately NOT capped: these rows are counted below as well as
    # returned, so a ceiling here would under-report how many incidents are
    # open -- a safety number, read as fact. They are bounded by operational
    # reality rather than by time: an incident leaves this set when it is
    # resolved.
    active_incidents = (
        base_query
        .filter(Incident.status.in_([IncidentStatus.ACTIVE, IncidentStatus.INVESTIGATING]))
        .order_by(Incident.created_at.desc(), Incident.id.desc())
        .all()
    )

    # Resolved incidents only accumulate, and this list is not counted. Newest
    # first, so a ceiling drops the oldest.
    resolved_incidents = capped_response(
        response,
        base_query.filter(Incident.status == IncidentStatus.RESOLVED)
        .order_by(Incident.created_at.desc(), Incident.id.desc()),
        limit=limit,
    )
    
    active_crew = sum(1 for inc in active_incidents if inc.type == IncidentType.CREW)
    active_aggregator = sum(1 for inc in active_incidents if inc.type == IncidentType.DRIVER) # Assuming DRIVER type represents aggregator incidents in this context as per original model

    return {
        "active_crew": active_crew,
        "active_aggregator": active_aggregator,
        "active_incidents": active_incidents,
        "resolved_incidents": resolved_incidents
    }

@router.get("/", response_model=List[IncidentResponse])
async def get_incidents(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    # Ordered newest-first as well as capped. It had no ordering at all, so a
    # ceiling would have truncated an arbitrary slice -- and "the first 2000
    # rows Postgres happened to return" is not an answer anyone can read.
    if current_user.role == "superadmin":
        return capped_response(
            response,
            db.query(Incident).order_by(Incident.created_at.desc(), Incident.id.desc()),
            limit=limit,
        )

    elif current_user.role == "agent":
        ownership = _agent_incident_filter(db, current_user.id)
        if ownership is None:
            return []
        return capped_response(
            response,
            db.query(Incident).filter(ownership)
            .order_by(Incident.created_at.desc(), Incident.id.desc()),
            limit=limit,
        )
    
    else:
        raise HTTPException(status_code=403, detail="Not authorized to list incidents")

@router.post("/", response_model=IncidentResponse)
async def create_incident(
    incident_in: IncidentCreate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    from app.services import incident_taxonomy as tax

    incident_data = incident_in.model_dump()
    incident_id = f"INC-{uuid.uuid4().hex[:6].upper()}"

    # Validate the taxonomy before anything is written. An unknown category
    # would sail straight through to reports and quietly skew them.
    category = incident_data.get("category")
    sub_category = incident_data.get("sub_category")
    if category and not tax.is_valid_category(category):
        raise HTTPException(status_code=400, detail=f"Unknown incident category: {category}")
    if not tax.is_valid_sub_category(category, sub_category):
        raise HTTPException(
            status_code=400,
            detail=f"Sub-category {sub_category!r} does not belong to {category!r}",
        )

    # Crew never set severity — a person in trouble should not be grading their
    # own risk. Medical and Safety start High; the agent can adjust later.
    incident_data["severity"] = tax.default_severity_for(category)
    
    if current_user.role == "crew":
        from app.db.models.crew_profile import CrewProfile
        crew = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
        if crew is None:
            raise HTTPException(status_code=404, detail="Crew profile not found")
        
        requested_trip_id = (incident_data.get("trip_id") or "").strip() or None

        # Remove fields that we will set explicitly to avoid "multiple values for keyword argument"
        for field in [
            "aggregator_id",
            "reporter_name",
            "reporter_role",
            "reporter_id",
            "type",
            "port_name",
            "trip_id",
            "crew_assignment_id",
        ]:
            incident_data.pop(field, None)

        reporter_hpid = crew.hpid if crew else None
        trip_id = None
        selected_booking = None
        if requested_trip_id:
            from app.db.models.cab_booking import CabBooking
            selected_booking = (
                db.query(CabBooking)
                .filter(
                    CabBooking.booking_id == requested_trip_id,
                    CabBooking.crew_id == crew.id if crew else False,
                )
                .first()
            )
            if not selected_booking:
                # Do not reveal whether another crew member owns the supplied id.
                raise HTTPException(status_code=404, detail="Trip not found")
            trip_id = selected_booking.booking_id
        from app.services.historical_context import event_context

        requested_assignment_id = incident_in.crew_assignment_id
        if selected_booking is not None:
            if requested_assignment_id is not None and (
                selected_booking.crew_assignment_id != requested_assignment_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="The selected assignment does not belong to this trip",
                )
            if selected_booking.crew_assignment_id is None:
                raise HTTPException(
                    status_code=409,
                    detail="This legacy trip has unresolved vessel ownership",
                )
            historical = event_context(
                db, booking=selected_booking, profile=crew
            )
        else:
            from app.services.historical_context import (
                selected_assignment_for_profile,
            )

            try:
                assignment = selected_assignment_for_profile(
                    db, crew, requested_assignment_id
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc))
            if assignment is not None:
                call = assignment.vessel_call
                resolution = "assignment"
            else:
                # A vessel no agency runs never produces an assignment, so
                # refusing here would leave its crew unable to report anything
                # at all. The call still identifies where it happened.
                from app.api.v1.routes_crew import _unmanaged_vessel_call

                _vessel, call = _unmanaged_vessel_call(db, crew)
                resolution = "unmanaged_vessel"
                if call is None:
                    raise HTTPException(
                        status_code=409,
                        detail="No active vessel assignment is available for this incident",
                    )
            if call is None:
                raise HTTPException(
                    status_code=409,
                    detail="The selected vessel assignment has no vessel call context",
                )
            historical = {
                "vessel_call": call,
                "vessel_id": call.vessel_id,
                "agency_id": call.agency_id,
                "crew_assignment_id": assignment.id if assignment else None,
                "port_id": call.port_id,
                "context_resolution": resolution,
            }
        incident = Incident(
            **incident_data,
            incident_id=incident_id,
            reporter_name=current_user.name,
            reporter_role=crew.rank if crew else "Crew",
            reporter_id=reporter_hpid,
            port_name=(
                historical["vessel_call"].port_name
                if historical["vessel_call"]
                else None
            ),
            vessel_id=historical["vessel_id"],
            vessel_call_id=(
                historical["vessel_call"].id if historical["vessel_call"] else None
            ),
            agency_id=historical["agency_id"],
            crew_profile_id=crew.id if crew else None,
            crew_assignment_id=historical["crew_assignment_id"],
            port_id=historical["port_id"],
            context_resolution=historical["context_resolution"],
            trip_id=trip_id,
            type=IncidentType.CREW
        )
    else:
        raise HTTPException(status_code=403, detail="Not authorized to create incidents")
    
    db.add(incident)
    db.flush()
    _record_timeline(db, incident, "reported", "Incident Reported",
                     detail=f"Reported by {incident.reporter_name or 'crew'}.")
    _record_timeline(db, incident, "received", "Incident Received",
                     detail="Incident has been received and logged in the system.")
    db.commit()
    db.refresh(incident)
    return _serialize_incident(db, incident)

@router.get("/{id:int}", response_model=IncidentResponse)
async def get_incident(
    id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    query = db.query(Incident).filter(Incident.id == id)
    
    if current_user.role == "superadmin":
        incident = query.first()
    elif current_user.role == "agent":
        ownership = _agent_incident_filter(db, current_user.id)
        if ownership is None:
             raise HTTPException(status_code=403, detail="Not authorized")
        incident = query.filter(ownership).first()
    else:
        raise HTTPException(status_code=403, detail="Not authorized")

    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    
    return incident

class StatusUpdate(BaseModel):
    status: IncidentStatus

@router.patch("/{id:int}/status", response_model=IncidentResponse)
async def update_incident_status(
    id: int,
    status_update: StatusUpdate,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    incident = await get_incident(id, db, current_user)
    incident = db.query(Incident).filter(Incident.id == incident.id).with_for_update().first()
    previous = incident.status
    if previous == status_update.status:
        return _serialize_incident(db, incident)
    if previous in {IncidentStatus.RESOLVED, IncidentStatus.CANCELLED}:
        raise HTTPException(status_code=409, detail="This incident is already in a terminal state")
    incident.status = status_update.status

    now = datetime.utcnow()
    # Stamp the closing time so resolution duration can be reported without
    # inferring it from the timeline.
    if status_update.status == IncidentStatus.RESOLVED and not incident.resolved_at:
        incident.resolved_at = now
    if status_update.status == IncidentStatus.CANCELLED and not incident.cancelled_at:
        incident.cancelled_at = now
    # Non-terminal transitions never carry closing stamps.
    if status_update.status in (IncidentStatus.ACTIVE, IncidentStatus.INVESTIGATING):
        incident.resolved_at = None
        incident.cancelled_at = None

    if previous != status_update.status:
        labels = {
            IncidentStatus.ACTIVE: ("reported", "Reopened"),
            IncidentStatus.INVESTIGATING: ("investigating", "Under Investigation"),
            IncidentStatus.RESOLVED: ("resolved", "Incident Resolved"),
            IncidentStatus.CANCELLED: ("cancelled", "Incident Cancelled"),
        }
        event_type, label = labels.get(status_update.status, ("status", "Status changed"))
        _record_timeline(
            db, incident, event_type, label,
            detail=f"Status changed from {previous.value} to {status_update.status.value}.",
            source="agent",
            actor_name=getattr(current_user, "name", None),
        )

    db.commit()
    db.refresh(incident)
    return _serialize_incident(db, incident)

@router.post("/{id:int}/notes", response_model=IncidentNoteResponse)
async def add_incident_note(
    id: int,
    note_in: IncidentNoteBase,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    incident = await get_incident(id, db, current_user)
    
    note = IncidentNote(
        incident_id=incident.id,
        note=note_in.note,
        author_name=note_in.author_name or current_user.name
    )
    
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


# --- Crew Safety Center (agent) -------------------------------------------

@router.get("/agent/summary")
def agent_safety_summary(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """The four tiles on the Crew Safety Center landing page.

    Everything is limited to the agent's own vessels — an agent should not see
    another agency's incidents, the same rule applied to the dashboard,
    notifications and trips.
    """
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can view the safety summary")

    from app.db.models.crew_sos import CrewSos
    from app.services.port_time import agent_port_day
    today_start, today_end, _ = agent_port_day(db, current_user)

    active_sos = 0
    avg_response_seconds = None
    from app.db.models.agent_profile import AgentProfile
    agency_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == current_user.id
    ).scalar()
    if agency_id is not None:
        owned_sos = db.query(CrewSos).filter(CrewSos.agency_id == agency_id)
        active_sos = owned_sos.filter(
            CrewSos.closed_at.is_(None),
            CrewSos.cancelled_at.is_(None),
        ).count()

        # Agreed definition: SOS raised -> agent acknowledged.
        acked = owned_sos.filter(CrewSos.acknowledged_at.isnot(None)).with_entities(
            CrewSos.created_at, CrewSos.acknowledged_at
        ).all()
        deltas = [
            (a - c).total_seconds()
            for c, a in acked
            if c and a and a >= c
        ]
        if deltas:
            avg_response_seconds = int(sum(deltas) / len(deltas))

    open_incidents = investigating = resolved_today = 0
    ownership = _agent_incident_filter(db, current_user.id)
    if ownership is not None:
        base = db.query(Incident).filter(ownership)
        open_incidents = base.filter(Incident.status == IncidentStatus.ACTIVE).count()
        investigating = base.filter(Incident.status == IncidentStatus.INVESTIGATING).count()
        resolved_today = base.filter(
            Incident.status == IncidentStatus.RESOLVED,
            Incident.resolved_at.isnot(None),
            Incident.resolved_at >= today_start,
            Incident.resolved_at < today_end,
        ).count()

    return {
        "active_sos": active_sos,
        "open_incidents": open_incidents,
        "investigating_incidents": investigating,
        "resolved_today": resolved_today,
        "avg_response_seconds": avg_response_seconds,
    }


@router.get("/agent/list")
def agent_incident_list(
    status_filter: Optional[str] = None,
    vessel_id: Optional[int] = None,
    include_sos: bool = False,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Incidents raised by crew on this agent's vessels.

    `include_sos` adds SOS alerts, tagged with `kind`, for the vessel page —
    which shows one combined safety list and would otherwise show nothing where
    an emergency happened, since an SOS no longer leaves a copycat Incident row.

    It is off by default because Incident Management has a dedicated SOS view
    beside it. Returning SOS there unconditionally put every emergency in both
    places at once, which is the duplication removing the copycat row was meant
    to end.
    """
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can view these incidents")

    ownership = _agent_incident_filter(db, current_user.id)
    if ownership is None:
        return {"incidents": []}

    query = db.query(Incident).filter(ownership)
    call = None
    if vessel_id is not None:
        # The vessel page is a view of the call the ship is on, not of the ship
        # for all time. Without this a returning vessel showed the previous
        # calls' incidents appended to its new one.
        call = current_call_for(db, vessel_id)
        query = query.filter(belongs_to_call(
            Incident.vessel_call_id, Incident.vessel_id, Incident.created_at,
            vessel_id=vessel_id, call=call,
        ))
    if status_filter:
        try:
            query = query.filter(Incident.status == IncidentStatus(status_filter.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Unknown status: {status_filter}")

    # Deliberately not given a result ceiling, though both streams grow with
    # time. This response merges two of them and sorts the union in Python, so
    # a ceiling would need a policy the ceiling cannot express: capping each
    # stream separately yields up to twice the limit and a header that cannot
    # say what was cut from which, and capping after the merge means the
    # database has already been asked for everything. It needs pagination over
    # the merged feed, which is a response-shape decision rather than a safety
    # limit. See app/services/result_limits for the same problem in a nested
    # list.
    rows = query.order_by(Incident.created_at.desc()).all()
    records = [dict(_serialize_incident(db, i), kind="incident") for i in rows]
    if include_sos:
        records.extend(_agent_sos_records(db, current_user.id, vessel_id, status_filter, call))
    # created_at mixes naive legacy Incident values with timezone-aware SOS
    # ones; comparing those directly raises TypeError.
    records.sort(key=lambda item: _sort_instant(item.get("created_at")), reverse=True)
    return {"incidents": records}


def _sort_instant(value) -> float:
    if value is None:
        return float("-inf")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _agent_sos_records(db: Session, agent_user_id: int, vessel_id: Optional[int],
                       status_filter: Optional[str], call=None) -> List[dict]:
    """This agent's SOS alerts, shaped like the incident rows beside them."""
    from app.db.models.agent_profile import AgentProfile
    from app.db.models.crew_profile import CrewProfile
    from app.db.models.crew_sos import CrewSos
    from app.db.models.vessel import Vessel

    agency_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == agent_user_id
    ).scalar()
    strong_query = db.query(CrewSos).filter(CrewSos.agency_id == agency_id)
    if vessel_id is not None:
        strong_query = strong_query.filter(belongs_to_call(
            CrewSos.vessel_call_id, CrewSos.vessel_id, CrewSos.created_at,
            vessel_id=vessel_id, call=call,
        ))
    strong_rows = strong_query.all() if agency_id is not None else []

    records: List[dict] = []
    seen = set()

    def append_record(sos, resolved_vessel_id, resolved_vessel_name):
        if sos.id in seen:
            return
        status = str(sos.status or "ACTIVE").upper()
        if status_filter and status != status_filter.upper():
            return
        profile_name = db.query(CrewProfile.full_name).filter(
            CrewProfile.id == sos.crew_profile_id
        ).scalar() if sos.crew_profile_id else None
        seen.add(sos.id)
        records.append({
            "kind": "sos",
            "id": sos.id,
            "incident_id": f"SOS-{sos.id}",
            "type": "sos",
            "title": "SOS Alert",
            "description": None,
            "status": status,
            "category": None,
            "sub_category": None,
            "severity": "critical",
            "category_label": "SOS Alert",
            "sub_category_label": None,
            "reporter_name": profile_name or sos.crew_email,
            "reporter_role": None,
            "reporter_id": None,
            "trip_id": sos.trip_id,
            "port_name": sos.port_name,
            "vessel_id": resolved_vessel_id,
            "vessel_name": resolved_vessel_name,
            "resolved_at": sos.closed_at,
            "cancelled_at": sos.cancelled_at,
            "created_at": sos.created_at,
            "updated_at": sos.created_at,
            "routing_status": "assigned",
            "routing_message": None,
        })

    for sos in strong_rows:
        append_record(sos, sos.vessel_id, sos.vessel)

    return records


@router.get("/agent/reports")
def agent_safety_report_records(
    vessel_id: Optional[int] = None,
    vessel_call_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Newest-first safety records for a current vessel or historical call."""
    if current_user.role != "agent":
        raise HTTPException(status_code=403, detail="Only agents can view reports")
    from app.db.models.crew_sos import CrewSos
    from app.db.models.vessel import Vessel
    from app.db.models.vessel_call import VesselCall
    from app.db.models.agent_profile import AgentProfile

    vessel = None
    if vessel_call_id is None:
        if vessel_id is None:
            raise HTTPException(status_code=422, detail="Choose a vessel or historical call")
        vessel = db.query(Vessel).filter(
            Vessel.id == vessel_id, Vessel.agent_id == current_user.id,
        ).first()
        if not vessel:
            raise HTTPException(status_code=404, detail="Vessel not found")

    agency_id = db.query(AgentProfile.id).filter(
        AgentProfile.user_id == current_user.id
    ).scalar()
    if agency_id is None:
        raise HTTPException(status_code=403, detail="Agent profile not found")

    if vessel_call_id is not None:
        call = db.query(VesselCall).filter(
            VesselCall.id == vessel_call_id,
            VesselCall.agency_id == agency_id,
        ).first()
        if not call:
            raise HTTPException(status_code=404, detail="Vessel call not found")
        incidents = db.query(Incident).filter(
            Incident.vessel_call_id == call.id,
            Incident.agency_id == agency_id,
        ).all()
        sos_rows = db.query(CrewSos).filter(
            CrewSos.vessel_call_id == call.id,
            CrewSos.agency_id == agency_id,
        ).all()
        vessel_payload = {
            "id": call.vessel_id,
            "vessel_call_id": call.id,
            "name": call.vessel_name,
            "imo_number": call.imo_number,
            "flag": call.flag,
            "eta": call.eta,
            "etd": call.etd,
            "berth": None,
            "port_name": call.port_name,
        }
    else:
        assert vessel is not None
        incidents = db.query(Incident).filter(
            Incident.vessel_id == vessel.id,
            Incident.agency_id == agency_id,
        ).all()
        sos_rows = db.query(CrewSos).filter(
            CrewSos.vessel_id == vessel.id,
            CrewSos.agency_id == agency_id,
        ).all()
        vessel_payload = {
            "id": vessel.id,
            "name": vessel.name,
            "imo_number": vessel.imo_number,
            "flag": vessel.flag,
            "eta": vessel.eta,
            "etd": vessel.etd,
            "berth": vessel.berth_assignment,
        }

    records = [{
        "kind": "incident", "id": item.id, "reference": item.incident_id,
        "title": item.title, "status": item.status.value,
        "severity": item.severity, "created_at": item.created_at,
        "resolved_at": item.resolved_at or item.cancelled_at,
    } for item in incidents]
    records.extend({
        "kind": "sos", "id": item.id, "reference": f"SOS-{item.id}",
        "title": "SOS Alert", "status": item.status,
        "severity": "high", "created_at": item.created_at,
        "resolved_at": item.closed_at or item.cancelled_at,
    } for item in sos_rows)
    def report_sort_key(item):
        created = item.get("created_at")
        if created is None:
            timestamp = float("-inf")
        else:
            # Legacy Incident timestamps are naive UTC while SOS columns are
            # timezone-aware. Comparing those datetime objects directly raises
            # TypeError as soon as a report contains both record types.
            timestamp = (
                created.replace(tzinfo=timezone.utc)
                if created.tzinfo is None
                else created.astimezone(timezone.utc)
            ).timestamp()
        return timestamp, int(item["id"])

    records.sort(key=report_sort_key, reverse=True)
    return {
        # Report generation time must be server-authoritative; a changed device
        # clock must not produce a misleading operational record.
        "generated_at": datetime.utcnow(),
        "vessel": vessel_payload,
        "records": records,
    }


def _agent_incident_or_404(db: Session, agent_user_id: int, incident_id: int) -> Incident:
    """One incident, only if it belongs to this agent's crew.

    404 rather than 403 for someone else's incident, so ids cannot be probed —
    the same rule used for trips, shore passes and SOS.
    """
    ownership = _agent_incident_filter(db, agent_user_id)
    incident = None
    if ownership is not None:
        incident = (
            db.query(Incident)
            .filter(Incident.id == incident_id, ownership)
            .first()
        )
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@router.get("/agent/detail/{incident_id}")
def agent_incident_detail(
    incident_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Everything the Incident Details screen shows, in one request.

    The screen needs the incident, its timeline, its notes and the crew member
    who raised it. Four round-trips for one screen is wasteful when the
    ownership check is identical for all four.
    """
    if current_user.role not in {"agent", "superadmin"}:
        raise HTTPException(status_code=403, detail="Not authorized to view incident details")

    from app.db.models.crew_profile import CrewProfile
    from app.db.models.incident import IncidentTimelineEvent
    from app.db.models.user import User
    from app.db.models.vessel import Vessel
    from app.db.models.vessel_call import VesselCall
    from app.services.historical_context import vessel_call_context
    from app.services.operations_context import booking_context, find_booking, vessel_context

    incident = (
        db.query(Incident).filter(Incident.id == incident_id).first()
        if current_user.role == "superadmin"
        else _agent_incident_or_404(db, current_user.id, incident_id)
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")

    events = (
        db.query(IncidentTimelineEvent)
        .filter(IncidentTimelineEvent.incident_id == incident.id)
        .order_by(IncidentTimelineEvent.event_time.asc(), IncidentTimelineEvent.id.asc())
        .all()
    )
    notes = (
        db.query(IncidentNote)
        .filter(IncidentNote.incident_id == incident.id)
        .order_by(IncidentNote.created_at.asc())
        .all()
    )

    reporter = None
    assignment = (
        db.query(CrewAssignment).filter(
            CrewAssignment.id == incident.crew_assignment_id
        ).first()
        if incident.crew_assignment_id else None
    )
    if assignment:
        profile = assignment.crew_profile
        phone = (
            db.query(User.mobile_number).filter(User.id == profile.user_id).first()
            if profile else None
        )
        reporter = {
            "hpid": assignment.hpid,
            "full_name": assignment.crew_name,
            "rank": assignment.rank,
            "nationality": assignment.nationality,
            "phone": phone[0] if phone else None,
        }
    elif incident.reporter_id:
        cp = db.query(CrewProfile).filter(CrewProfile.hpid == incident.reporter_id).first()
        if cp:
            # The phone number lives on the user account, not the crew profile.
            phone = db.query(User.mobile_number).filter(User.id == cp.user_id).first()
            reporter = {
                "hpid": cp.hpid,
                "full_name": cp.full_name,
                "rank": cp.rank,
                "nationality": cp.nationality,
                "phone": phone[0] if phone else None,
            }

    detail = _serialize_incident(db, incident)
    # Reported -> closed, when the incident has actually been closed. Left null
    # while it is open rather than counting up to "now", which would make an
    # open incident look like it had a resolution time.
    closed_at = incident.resolved_at or incident.cancelled_at
    resolution_seconds = None
    if closed_at and incident.created_at:
        elapsed = int((closed_at - incident.created_at).total_seconds())
        # A close stamped before the report is impossible; showing it would
        # render as a negative duration ("-20m"). Report nothing rather than
        # nonsense, and leave the bad row visible as bad rather than papering
        # over it with a zero.
        resolution_seconds = elapsed if elapsed >= 0 else None
    detail["resolution_seconds"] = resolution_seconds

    vessel = db.query(Vessel).filter(Vessel.id == incident.vessel_id).first() if incident.vessel_id else None
    vessel_call = (
        db.query(VesselCall).filter(VesselCall.id == incident.vessel_call_id).first()
        if incident.vessel_call_id
        else None
    )
    booking = find_booking(db, incident.trip_id)

    return {
        "incident": detail,
        "reporter": reporter,
        "vessel": (
            vessel_call_context(vessel_call, fallback_port=incident.port_name)
            if vessel_call
            else vessel_context(vessel, port_name=incident.port_name)
        ),
        # As of when the incident was filed, not now: a trip that has since
        # finished must not report a skipped stop as the crew's next destination.
        "trip": booking_context(db, booking, as_of=incident.created_at),
        "timeline": [
            {
                "id": e.id,
                "source": e.source,
                "event_type": e.event_type,
                "label": e.label,
                "detail": e.detail,
                "actor_name": e.actor_name,
                "event_time": e.event_time,
            }
            for e in events
        ],
        "notes": [
            {
                "id": n.id,
                "author_name": n.author_name,
                "note": n.note,
                "created_at": n.created_at,
            }
            for n in notes
        ],
    }


@router.get("/agent/report/{record_kind}/{record_id}")
def agent_safety_report(
    record_kind: str,
    record_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Canonical, server-stamped payload for Incident and SOS PDF reports."""
    if current_user.role not in {"agent", "superadmin"}:
        raise HTTPException(status_code=403, detail="Not authorized to generate safety reports")
    kind = record_kind.strip().lower()
    if kind == "incident":
        payload = agent_incident_detail(record_id, db=db, current_user=current_user)
    elif kind == "sos":
        from app.api.v1.routes_sos import get_sos_timeline

        sos_payload = get_sos_timeline(record_id, db=db, current_user=current_user)
        payload = sos_payload.model_dump()
    else:
        raise HTTPException(status_code=404, detail="Safety record not found")
    return {
        "record_kind": kind,
        "generated_at": datetime.now(timezone.utc),
        "payload": payload,
    }


@router.post("/agent/report/{record_kind}/{record_id}/snapshots")
def create_agent_safety_report_snapshot(
    record_kind: str,
    record_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Freeze one generated safety report as an immutable audit artifact."""
    if current_user.role not in {"agent", "superadmin"}:
        raise HTTPException(status_code=403, detail="Not authorized to generate report snapshots")

    from app.db.models.agent_profile import AgentProfile
    from app.db.models.crew_sos import CrewSos
    from app.services.report_snapshots import (
        create_report_snapshot,
        serialize_report_snapshot,
    )

    report = agent_safety_report(
        record_kind=record_kind,
        record_id=record_id,
        db=db,
        current_user=current_user,
    )
    kind = report["record_kind"]
    if kind == "incident":
        source = db.query(Incident).filter(Incident.id == record_id).one()
        reference = source.incident_id
    else:
        source = db.query(CrewSos).filter(CrewSos.id == record_id).one()
        reference = f"SOS-{source.id}"

    agency_id = (
        source.agency_id
        if current_user.role == "superadmin"
        else db.query(AgentProfile.id).filter(
            AgentProfile.user_id == current_user.id
        ).scalar()
    )
    if agency_id is None:
        raise HTTPException(
            status_code=409,
            detail="This historical record needs agency reconciliation before snapshotting",
        )

    snapshot = create_report_snapshot(
        db,
        report_kind=kind,
        source_id=source.id,
        source_reference=reference,
        agency_id=agency_id,
        vessel_call_id=source.vessel_call_id,
        generated_by_user_id=current_user.id,
        payload=report["payload"],
    )
    db.commit()
    db.refresh(snapshot)
    return serialize_report_snapshot(snapshot)


@router.get("/agent/report-snapshots/{snapshot_id}")
def get_agent_safety_report_snapshot(
    snapshot_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Read an artifact without rebuilding it from mutable current state."""
    from app.db.models.agent_profile import AgentProfile
    from app.db.models.report_snapshot import ReportSnapshot
    from app.services.report_snapshots import serialize_report_snapshot

    query = db.query(ReportSnapshot).filter(ReportSnapshot.id == snapshot_id)
    if current_user.role == "agent":
        agency_id = db.query(AgentProfile.id).filter(
            AgentProfile.user_id == current_user.id
        ).scalar()
        if agency_id is None:
            raise HTTPException(status_code=404, detail="Report snapshot not found")
        query = query.filter(ReportSnapshot.agency_id == agency_id)
    elif current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Not authorized")

    snapshot = query.first()
    if not snapshot:
        raise HTTPException(status_code=404, detail="Report snapshot not found")
    return serialize_report_snapshot(snapshot)


@router.get("/{id:int}/timeline")
async def get_incident_timeline(
    id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """System and agent events for one incident, oldest first."""
    from app.db.models.incident import IncidentTimelineEvent

    incident = await get_incident(id, db, current_user)
    events = (
        db.query(IncidentTimelineEvent)
        .filter(IncidentTimelineEvent.incident_id == incident.id)
        .order_by(IncidentTimelineEvent.event_time.asc(), IncidentTimelineEvent.id.asc())
        .all()
    )
    return {
        "incident_id": incident.incident_id,
        "events": [
            {
                "id": e.id,
                "source": e.source,
                "event_type": e.event_type,
                "label": e.label,
                "detail": e.detail,
                "actor_name": e.actor_name,
                "event_time": e.event_time,
            }
            for e in events
        ],
    }


# --- Manual timeline entries ("custom timings") ----------------------------

class TimelineEntryIn(BaseModel):
    label: str
    detail: Optional[str] = None
    event_time: Optional[datetime] = None
    event_type: Optional[str] = None


MANUAL_EVENT_TYPES = {"update", "investigation", "resolved", "note"}


def _manual_event_or_404(db: Session, current_user, event_id: int):
    """A manual timeline row on an incident this agent owns.

    System rows are the incident's own audit trail — they are never editable,
    or the timeline stops being evidence. Ownership is checked through the
    parent incident, and a system row is reported as not found rather than as
    forbidden so the two cases stay indistinguishable.
    """
    from app.db.models.incident import IncidentTimelineEvent

    event = db.query(IncidentTimelineEvent).filter(
        IncidentTimelineEvent.id == event_id
    ).first()
    if not event:
        raise HTTPException(status_code=404, detail="Timeline entry not found")

    # The ownership check raises its own "Incident not found". Re-raise with the
    # timeline wording so a row belonging to another agency is indistinguishable
    # from one that does not exist — otherwise the message itself confirms the id.
    if current_user.role != "superadmin":
        try:
            _agent_incident_or_404(db, current_user.id, event.incident_id)
        except HTTPException:
            raise HTTPException(status_code=404, detail="Timeline entry not found")

    if (event.source or "system") not in {"agent", "superadmin"}:
        raise HTTPException(status_code=404, detail="Timeline entry not found")
    if current_user.role == "agent" and (event.source or "system") != "agent":
        raise HTTPException(status_code=404, detail="Timeline entry not found")
    return event


def _timeline_out(event, viewer_role: str) -> dict:
    return {
        "id": event.id,
        "source": event.source,
        "event_type": event.event_type,
        "label": event.label,
        "detail": event.detail,
        "actor_name": event.actor_name,
        "event_time": event.event_time,
        "editable": (
            (event.source or "system") in {"agent", "superadmin"}
            and (viewer_role == "superadmin" or (event.source or "system") == "agent")
        ),
    }


@router.post("/agent/{incident_id}/timeline")
def add_incident_timeline_entry(
    incident_id: int,
    body: TimelineEntryIn,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Add an agent's own update to an incident timeline."""
    if current_user.role not in {"agent", "superadmin"}:
        raise HTTPException(status_code=403, detail="Not authorized to add timeline updates")

    from app.db.models.incident import IncidentTimelineEvent

    incident = (
        db.query(Incident).filter(Incident.id == incident_id).first()
        if current_user.role == "superadmin"
        else _agent_incident_or_404(db, current_user.id, incident_id)
    )
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status in (IncidentStatus.RESOLVED, IncidentStatus.CANCELLED):
        raise HTTPException(status_code=409,
                            detail="Timeline updates are locked for a closed incident")
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="An update needs a label")

    event_type = (body.event_type or "update").strip().lower()
    if event_type not in MANUAL_EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown update type: {event_type}")

    event = IncidentTimelineEvent(
        incident_id=incident.id,
        source=current_user.role,
        event_type=event_type,
        label=label,
        detail=(body.detail or "").strip() or None,
        actor_name=getattr(current_user, "name", None) or "Agent",
        event_time=body.event_time or datetime.utcnow(),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return _timeline_out(event, current_user.role)


@router.patch("/agent/timeline/{event_id}")
def edit_incident_timeline_entry(
    event_id: int,
    body: TimelineEntryIn,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    if current_user.role not in {"agent", "superadmin"}:
        raise HTTPException(status_code=403, detail="Not authorized to edit timeline updates")

    event = _manual_event_or_404(db, current_user, event_id)
    label = (body.label or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="An update needs a label")

    event.label = label
    event.detail = (body.detail or "").strip() or None
    if body.event_time is not None:
        event.event_time = body.event_time
    if body.event_type:
        event_type = body.event_type.strip().lower()
        if event_type not in MANUAL_EVENT_TYPES:
            raise HTTPException(status_code=400, detail=f"Unknown update type: {event_type}")
        event.event_type = event_type

    db.commit()
    db.refresh(event)
    return _timeline_out(event, current_user.role)


@router.delete("/agent/timeline/{event_id}")
def delete_incident_timeline_entry(
    event_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    if current_user.role not in {"agent", "superadmin"}:
        raise HTTPException(status_code=403, detail="Not authorized to delete timeline updates")

    event = _manual_event_or_404(db, current_user, event_id)
    db.delete(event)
    db.commit()
    return {"deleted": True, "id": event_id}
