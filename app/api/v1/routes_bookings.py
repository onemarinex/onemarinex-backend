from app.services.collection_pages import page_all
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_driver
from app.api.v1.routes_auth import get_current_user
from app.db.models.booking_timeline import BookingTimeline, TimelineEventType
from app.services import attempt_limits
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.driver import Driver
from app.db.models.driver_magic_link import DriverMagicLink, DriverMagicLinkReachEvent
from app.db.models.user import User
from app.db.session import get_db
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.models.crew_profile import CrewProfile
from app.db.models.agent_profile import AgentProfile
from app.db.models.port import Port
from app.db.models.vendors import Vendors
from app.services.booking_service import (
    accept_booking,
    assign_driver_to_booking,
    complete_trip,
    driver_accept_booking,
    get_booking_by_identifier,
    get_dashboard_metrics,
    get_eligible_drivers,
    list_bookings_for_user,
    reject_booking,
    request_trip_review,
    serialize_booking,
    start_trip,
    vehicle_category_matches,
)
from app.services.magic_link_service import (
    add_stop_to_magic_link,
    assert_link_usable,
    create_or_refresh_magic_link,
    rotate_magic_link,
    get_magic_link_by_token,
    mark_stop_reached,
    serialize_magic_link_public_payload,
)
from app.services.timeline_service import create_timeline_event
from app.services.timeline_service import get_booking_timeline

router = APIRouter()
logger = logging.getLogger(__name__)


def _agent_may_view_booking(
    db: Session, agent_user_id: int, booking: CabBooking
) -> bool:
    """Only immutable, resolved event-time agency grants historical access."""
    from app.services.event_origin import agency_scope

    agency_id = (
        db.query(AgentProfile.id).filter(AgentProfile.user_id == agent_user_id).scalar()
    )
    return (
        db.query(CabBooking.id)
        .filter(CabBooking.id == booking.id, agency_scope(CabBooking, agency_id))
        .first()
        is not None
    )


# Stop labels that are structural, not real venue names — a "drop" stop is
# stored as name="Drop" with the actual venue sitting in `address`.
_GENERIC_STOP_LABELS = {"pickup", "drop", "stop", "trip end", "trip end (port)"}
_MAGIC_LINK_TERMINAL_STATUSES = {
    BookingStatus.COMPLETED.value,
    BookingStatus.CANCELLED.value,
    BookingStatus.PROVIDER_REJECTED.value,
}
# The policy itself lives in app/services/otp_attempts, shared with the
# authenticated driver route, which had none of it.


def _require_magic_link_otp(magic_link) -> None:
    if not magic_link.otp_verified_at:
        raise HTTPException(status_code=403, detail="Verify OTP first")


def _require_magic_link_active(magic_link) -> CabBooking:
    booking = magic_link.booking
    if not booking:
        raise HTTPException(status_code=404, detail="Linked booking not found")
    if _booking_status_label(booking) in _MAGIC_LINK_TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail="This trip is no longer active",
        )
    return booking


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _booking_status_label(booking: CabBooking) -> str:
    """Lower-cased status, tolerant of how the row was written.

    `cab_bookings.status` holds a mix of enum names ("COMPLETED") and values
    ("completed") from earlier code, so comparing against either one alone
    misses half the rows.
    """
    status = booking.status
    return str(getattr(status, "value", status)).lower()


def _first_usable_phone(raw: Optional[str]) -> Optional[str]:
    """Vendor phone fields in this DB hold anything from a single number to a
    comma-separated list of five, or the placeholder text 'NA'. Return the
    first entry containing digits, so a multi-number field doesn't get
    mangled into one impossible number by the digit-stripping normalizer."""
    if not raw:
        return None
    for part in re.split(r"[,;/]", raw):
        if any(ch.isdigit() for ch in part):
            return part.strip()
    return None


def _lookup_facility_phone(db: Session, stop: Dict[str, Any], port_value: Optional[str]) -> Optional[str]:
    """Find the real phone number for the venue an itinerary stop refers to,
    from the Vendors table the superadmin dashboard manages.

    Checks both `name` and `address`: package-trip facility stops carry the
    venue in `name`, but a coordinated-transfer drop stop is stored as
    name="Drop" with the venue in `address`. Exact (case-insensitive) match
    only — never fuzzy, so we can't notify the wrong business. Active
    vendors win over inactive duplicates (this table has several same-named
    rows across categories, e.g. the same pub listed under 'pub' and
    'restaurant')."""
    candidates = []
    for key in ("name", "address"):
        value = str(stop.get(key) or "").strip()
        if value and value.lower() not in _GENERIC_STOP_LABELS and value not in candidates:
            candidates.append(value)
    if not candidates:
        return None

    port = None
    if port_value:
        port = db.query(Port).filter((Port.code == port_value) | (Port.name == port_value)).first()

    for candidate in candidates:
        query = db.query(Vendors).filter(func.lower(Vendors.name) == candidate.lower())
        if port:
            query = query.filter(Vendors.port_id == port.id)
        matches = query.all()
        if not matches:
            continue

        # Prefer an active listing; fall back to any match if none are active.
        active = [v for v in matches if (v.status or "").strip().lower() == "active"]
        preferred = active or matches
        if len(preferred) > 1:
            logger.info(
                "facility phone lookup: %d vendors named %r at port %r — using the first",
                len(preferred), candidate, port_value,
            )
        for vendor in preferred:
            phone = _first_usable_phone(vendor.phone)
            if phone:
                return phone
    return None


def _resolve_booking_db_id(db: Session, booking_identifier: str) -> int:
    query = db.query(CabBooking.id)
    if booking_identifier.isdigit():
        row = query.filter(CabBooking.id == int(booking_identifier)).first()
    else:
        row = query.filter(CabBooking.booking_id == booking_identifier).first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    return int(row.id)


def _get_booking_action_row(db: Session, booking_identifier: str):
    query = db.query(
        CabBooking.id,
        cast(CabBooking.status, String).label("status"),
        CabBooking.provider_id,
        CabBooking.aggregator_id,
        CabBooking.driver_id,
        CabBooking.assigned_driver_id,
        CabBooking.driver_name,
        cast(CabBooking.vehicle_type, String).label("vehicle_type"),
        CabBooking.vehicle_name,
        CabBooking.pickup_address,
        CabBooking.pickup_lat,
        CabBooking.pickup_lng,
        CabBooking.drop_address,
        CabBooking.drop_lat,
        CabBooking.drop_lng,
    )
    if booking_identifier.isdigit():
        row = query.filter(CabBooking.id == int(booking_identifier)).first()
    else:
        row = query.filter(CabBooking.booking_id == booking_identifier).first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    return row


class AssignDriverIn(BaseModel):
    driver_id: int
    itinerary_stops: Optional[List[Dict[str, Any]]] = None


class MarkReachedIn(BaseModel):
    latitude: float
    longitude: float
    notes: Optional[str] = Field(default=None, max_length=500)


class VerifyMagicOtpIn(BaseModel):
    otp: str = Field(min_length=4, max_length=10)


class AddMagicStopIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address: Optional[str] = Field(default=None, max_length=255)
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    type: Optional[str] = Field(default="facility", max_length=32)


class CompleteRideIn(BaseModel):
    latitude: float
    longitude: float
    notes: Optional[str] = Field(default=None, max_length=500)


class BookingListOut(BaseModel):
    bookings: List[dict]
    total: int


@router.get("/magic/{token}")
def get_magic_link_payload(
    token: str,
    db: Session = Depends(get_db),
):
    magic_link = get_magic_link_by_token(db, token)
    return serialize_magic_link_public_payload(magic_link)


@router.post("/magic/{token}/stops/{stop_id}/reached")
def mark_magic_link_stop_reached(
    token: str,
    stop_id: str,
    body: MarkReachedIn,
    db: Session = Depends(get_db),
):
    magic_link = get_magic_link_by_token(db, token)
    _require_magic_link_active(magic_link)

    matched_stop: Dict[str, Any] = {}
    stop_type = "facility"
    stop_name = stop_id
    for stop in magic_link.itinerary_stops or []:
        if str(stop.get("id")) == stop_id:
            matched_stop = stop
            stop_type = str(stop.get("type") or "facility").strip().lower()
            # A "drop" stop's name is the literal label "Drop"; the venue the
            # crew actually arrived at is in `address`.
            stop_name = str(
                stop.get("address") if str(stop.get("name") or "").strip().lower() in _GENERIC_STOP_LABELS
                else stop.get("name") or stop_id
            )
            break

    if stop_type != "pickup":
        _require_magic_link_otp(magic_link)

    _, event_created = mark_stop_reached(
        db,
        magic_link=magic_link,
        stop_id=stop_id,
        latitude=body.latitude,
        longitude=body.longitude,
        notes=body.notes,
    )

    try:
        booking = magic_link.booking
        if booking and event_created:
            if stop_type == "pickup":
                from app.services.whatsapp import notify_driver_arrival
                crew_user = booking.crew.user if booking.crew else None
                notify_driver_arrival(crew_user.mobile_number if crew_user else None, booking.booking_id)
            elif stop_type != "trip_end":
                facility_phone = _lookup_facility_phone(db, matched_stop, booking.port)
                if facility_phone:
                    from app.services.whatsapp import notify_facility_reached
                    notify_facility_reached(facility_phone, booking.booking_id, booking.num_passengers, stop_name)
                else:
                    logger.info(
                        "facility_reached skipped — no vendor with a usable phone matched stop=%r port=%r",
                        matched_stop, booking.port,
                    )
    except Exception:
        logger.exception("WhatsApp stop-reached notify failed for magic link id=%s stop %s", magic_link.id, stop_id)

    refreshed = get_magic_link_by_token(db, token)
    return serialize_magic_link_public_payload(refreshed)


@router.post("/magic/{token}/verify-otp")
def verify_magic_link_otp(
    token: str,
    body: VerifyMagicOtpIn,
    db: Session = Depends(get_db),
):
    # Serialize attempts for this token so parallel requests cannot bypass the
    # failed-attempt counter or lockout threshold.
    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.token == token)
        .with_for_update()
        .first()
    )
    if not magic_link:
        raise HTTPException(status_code=404, detail="Magic link not found")
    # This handler locks the row itself rather than going through
    # get_magic_link_by_token, so it has to apply the same lifecycle check.
    assert_link_usable(magic_link)
    booking = _require_magic_link_active(magic_link)

    if magic_link.otp_verified_at:
        return {"verified": True}

    now = datetime.now(timezone.utc)
    attempt_limits.assert_attempt_allowed(magic_link, now=now)

    crew_otp = booking.crew.ride_otp if booking.crew else None
    booking_otp = booking.otp
    if body.otp != (crew_otp or "") and body.otp != (booking_otp or ""):
        locked = attempt_limits.record_failure(magic_link, now=now)
        db.commit()
        if locked:
            raise attempt_limits.locked_out_error()
        raise HTTPException(status_code=400, detail="Invalid OTP")

    magic_link.otp_verified_at = now
    attempt_limits.record_success(magic_link)
    if not booking.trip_started_at:
        booking.trip_started_at = now
        booking.status = BookingStatus.ON_TRIP

        create_timeline_event(
            db,
            booking_db_id=booking.id,
            event_type=TimelineEventType.TRIP_STARTED,
            actor_type="driver",
            metadata={"source": "magic_link_otp"},
            event_time=now,
        )
    db.commit()
    return {"verified": True}


@router.post("/magic/{token}/stops")
def add_magic_link_stop(
    token: str,
    body: AddMagicStopIn,
    db: Session = Depends(get_db),
):
    magic_link = get_magic_link_by_token(db, token)
    _require_magic_link_active(magic_link)
    _require_magic_link_otp(magic_link)
    updated = add_stop_to_magic_link(
        db,
        magic_link,
        name=body.name,
        address=body.address,
        latitude=body.latitude,
        longitude=body.longitude,
        stop_type=body.type or "facility",
    )
    return serialize_magic_link_public_payload(updated)


@router.post("/magic/{token}/complete-ride")
def complete_magic_link_ride(
    token: str,
    body: CompleteRideIn,
    db: Session = Depends(get_db),
):
    # Serialize completion for a token. Without this lock, two requests can
    # both pass the status check and race on the unique trip-end event.
    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.token == token)
        .with_for_update()
        .first()
    )
    if not magic_link:
        raise HTTPException(status_code=404, detail="Magic link not found")
    booking = magic_link.booking
    if not booking:
        raise HTTPException(status_code=404, detail="Linked booking not found")

    # A second tap on "Complete Ride" (or a retry over a flaky connection) used
    # to re-run the whole thing: it overwrote trip_completed_at, appended
    # duplicate reach and timeline events, and sent the crew a second review
    # request. Completing an already-completed ride is a no-op.
    if _booking_status_label(booking) == "completed":
        logger.info(
            "complete-ride ignored for booking %s — already completed", booking.booking_id
        )
        return serialize_magic_link_public_payload(get_magic_link_by_token(db, token))

    _require_magic_link_active(magic_link)
    _require_magic_link_otp(magic_link)

    trip_end_stop = None
    for stop in magic_link.itinerary_stops or []:
        stop_type = str(stop.get("type") or "").strip().lower()
        stop_id = str(stop.get("id") or "").strip().lower()
        if stop_type == "trip_end" or stop_id == "trip_end":
            trip_end_stop = stop
            break
    if not trip_end_stop:
        trip_end_stop = {
            "id": "trip_end",
            "name": "Trip End (Port)",
            "address": booking.pickup_address,
            "lat": booking.pickup_lat,
            "lng": booking.pickup_lng,
            "type": "trip_end",
        }
        itinerary = list(magic_link.itinerary_stops or [])
        itinerary.append(trip_end_stop)
        magic_link.itinerary_stops = itinerary

    now = datetime.utcnow()

    trip_end_stop_id = str(trip_end_stop.get("id") or "trip_end")
    existing_trip_end_event = (
        db.query(DriverMagicLinkReachEvent)
        .filter(
            DriverMagicLinkReachEvent.magic_link_id == magic_link.id,
            DriverMagicLinkReachEvent.stop_id == trip_end_stop_id,
        )
        .first()
    )
    if not existing_trip_end_event:
        db.add(DriverMagicLinkReachEvent(
            magic_link_id=magic_link.id,
            stop_id=trip_end_stop_id,
            stop_name=str(trip_end_stop.get("name") or "Trip End (Port)"),
            latitude=body.latitude,
            longitude=body.longitude,
            notes=body.notes,
        ))

    db.query(CabBooking).filter(CabBooking.id == booking.id).update(
        {
            CabBooking.status: BookingStatus.COMPLETED,
            CabBooking.completed_at: now,
            CabBooking.trip_completed_at: now,
        },
        synchronize_session=False,
    )
    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.TRIP_COMPLETED,
        actor_id=None,
        actor_type="magic_link",
        metadata={"source": "public_magic_link"},
        event_time=now,
    )
    db.commit()

    request_trip_review(booking)

    refreshed = get_magic_link_by_token(db, token)
    return serialize_magic_link_public_payload(refreshed)


@router.get("")
def list_bookings(
    status: Optional[str] = None,
    provider_id: Optional[int] = None,
    provider_type: Optional[str] = None,
    port: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role not in {"superadmin", "aggregator", "crew"}:
        raise HTTPException(status_code=403, detail="Unauthorized to list bookings")

    bookings = list_bookings_for_user(
        db,
        current_user,
        status=status,
        provider_id=provider_id,
        provider_type=provider_type,
        port=port,
        date_from=date_from,
        date_to=date_to,
    )
    serialized = [serialize_booking(booking) for booking in bookings]
    return {"bookings": serialized, "total": len(serialized)}


@router.get("/metrics")
def booking_metrics(
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Only super admins can view booking metrics")
    return get_dashboard_metrics(db, port_id=port_id)


@router.get("/{booking_id}")
def get_booking(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = get_booking_by_identifier(db, booking_id)

    if current_user.role == "crew":
        profile = current_user.crew_profile
        if not profile or booking.crew_id != profile.id:
            raise HTTPException(status_code=403, detail="Unauthorized")
    elif current_user.role == "aggregator":
        provider = current_user.aggregator_profile
        provider_id = booking.provider_id or booking.aggregator_id
        if not provider or provider_id != provider.id:
            raise HTTPException(status_code=403, detail="Unauthorized")
    elif current_user.role == "agent":
        if not _agent_may_view_booking(db, current_user.id, booking):
            raise HTTPException(status_code=403, detail="Unauthorized")
    elif current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Unauthorized")

    return serialize_booking(booking)


@router.get("/{booking_id}/timeline")
def get_timeline(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = get_booking_by_identifier(db, booking_id)

    if current_user.role == "crew":
        profile = current_user.crew_profile
        if not profile or booking.crew_id != profile.id:
            raise HTTPException(status_code=403, detail="Unauthorized")
    elif current_user.role == "aggregator":
        provider = current_user.aggregator_profile
        provider_id = booking.provider_id or booking.aggregator_id
        if not provider or provider_id != provider.id:
            raise HTTPException(status_code=403, detail="Unauthorized")
    elif current_user.role == "agent":
        if not _agent_may_view_booking(db, current_user.id, booking):
            raise HTTPException(status_code=403, detail="Unauthorized")
    elif current_user.role == "driver":
        raise HTTPException(status_code=403, detail="Drivers should use driver endpoints")
    elif current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Unauthorized")

    return {
        "booking_id": booking.booking_id,
        "timeline": get_booking_timeline(db, booking.id),
    }


@router.get("/{booking_id}/eligible-drivers")
def list_eligible_drivers(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can view eligible drivers")

    provider = current_user.aggregator_profile
    if not provider:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    booking = _get_booking_action_row(db, booking_id)
    provider_id = booking.provider_id or booking.aggregator_id
    if provider_id != provider.id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    vehicle_type = (booking.vehicle_type or "").lower()
    drivers = page_all(
        db.query(Driver).filter(Driver.aggregator_id == provider.id),
        collection="routes_bookings.list_eligible_drivers.0",
        label="Eligible drivers",
    )
    eligible = []
    for driver in drivers:
        if (driver.status or "").lower() != "available":
            continue
        if not vehicle_category_matches(
            db,
            provider.operating_port_id,
            vehicle_type,
            booking.vehicle_name,
            driver.vehicle_type,
        ):
            continue
        eligible.append(driver)

    return [
        {
            "id": driver.id,
            "name": driver.name,
            "phone": driver.phone,
            "vehicle_number": driver.vehicle_number,
            "vehicle_type": driver.vehicle_type,
            "status": driver.status,
            "rating": driver.rating,
        }
        for driver in eligible
    ]


@router.post("/{booking_id}/accept")
def accept_booking_endpoint(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking_db_id = _resolve_booking_db_id(db, booking_id)
    updated = accept_booking(db, SimpleNamespace(id=booking_db_id), current_user)
    return serialize_booking(updated)


@router.post("/{booking_id}/reject")
def reject_booking_endpoint(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can perform this action")

    provider = current_user.aggregator_profile
    if not provider:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    from app.db.models.booking_provider_rejection import BookingProviderRejection

    booking = _get_booking_action_row(db, booking_id)
    assigned_provider_id = booking.provider_id or booking.aggregator_id
    if assigned_provider_id is not None and assigned_provider_id != provider.id:
        raise HTTPException(status_code=403, detail="Booking not assigned to your provider account")

    current_status = (booking.status or "").lower()
    if current_status != BookingStatus.PENDING_PROVIDER_RESPONSE.value:
        raise HTTPException(status_code=400, detail="Booking is not awaiting provider response")

    already_declined = (
        db.query(BookingProviderRejection.id)
        .filter(
            BookingProviderRejection.booking_id == booking.id,
            BookingProviderRejection.provider_id == provider.id,
        )
        .first()
    )
    if already_declined:
        return {
            "message": "Booking already declined by this provider",
            "booking_id": booking_id,
        }

    now = datetime.utcnow()

    rejection = BookingProviderRejection(
        booking_id=booking.id,
        provider_id=provider.id,
        created_at=now,
    )
    db.add(rejection)
    db.flush()

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.PROVIDER_REJECTED,
        actor_id=provider.id,
        actor_type="provider",
        metadata={"provider_name": provider.company_name},
        event_time=now,
    )

    from app.services.booking_service import get_eligible_providers_for_ride
    full_booking = db.query(CabBooking).filter(CabBooking.id == booking.id).first()
    ride_type = full_booking.ride_type
    vehicle_type_val = full_booking.vehicle_type.value if full_booking.vehicle_type else ""
    vehicle_name_val = full_booking.vehicle_name or ""
    eligible_providers = get_eligible_providers_for_ride(db, ride_type, full_booking.port, vehicle_type_val, vehicle_name_val)
    rejected_provider_ids = {
        r.provider_id for r in
        db.query(BookingProviderRejection.provider_id)
        .filter(BookingProviderRejection.booking_id == booking.id)
        .all()
    }
    remaining = [p for p in eligible_providers if p.id not in rejected_provider_ids]

    if not remaining:
        full_booking.status = BookingStatus.PROVIDER_REJECTED
        create_timeline_event(
            db,
            booking_db_id=full_booking.id,
            event_type=TimelineEventType.PROVIDER_REJECTED,
            actor_id=provider.id,
            actor_type="provider",
            metadata={"reason": "all_providers_rejected"},
            event_time=now,
        )

    db.commit()

    if not remaining:
        try:
            from app.services.whatsapp import notify_trip_rejection
            crew_user = full_booking.crew.user if full_booking.crew else None
            notify_trip_rejection(crew_user.mobile_number if crew_user else None)
        except Exception:
            logger.exception("WhatsApp trip_rejection notify failed for booking %s", full_booking.booking_id)

    return {
        "message": "Booking declined for this provider; still available to others" if remaining else "Booking cancelled — no eligible providers remaining",
        "booking_id": booking_id,
        "cancelled": not remaining,
    }


@router.post("/{booking_id}/assign-driver")
def assign_driver_endpoint(
    booking_id: str,
    body: AssignDriverIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can perform this action")

    provider = current_user.aggregator_profile
    if not provider:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    booking = _get_booking_action_row(db, booking_id)
    assigned_provider_id = booking.provider_id or booking.aggregator_id
    if assigned_provider_id != provider.id:
        raise HTTPException(status_code=403, detail="Booking not assigned to your provider account")

    current_status = (booking.status or "").lower()
    already_assigned_driver_id = booking.assigned_driver_id or booking.driver_id
    if current_status == BookingStatus.DRIVER_ASSIGNED.value and already_assigned_driver_id == body.driver_id:
        updated = SimpleNamespace(
            id=booking.id,
            pickup_address=booking.pickup_address,
            pickup_lat=booking.pickup_lat,
            pickup_lng=booking.pickup_lng,
            drop_address=booking.drop_address,
            drop_lat=booking.drop_lat,
            drop_lng=booking.drop_lng,
        )
        magic_link = create_or_refresh_magic_link(
            db,
            booking=updated,
            aggregator_id=provider.id,
            itinerary_stops=body.itinerary_stops,
        )
        return {
            "booking_id": booking_id,
            "message": "Driver already assigned",
            "magic_link": {
                "token": magic_link.token,
                "path": f"/magic-link/{magic_link.token}",
            },
        }

    if current_status != BookingStatus.PROVIDER_ACCEPTED.value:
        raise HTTPException(status_code=400, detail="Booking must be accepted before assigning a driver")

    driver = (
        db.query(Driver)
        .filter(Driver.id == body.driver_id, Driver.aggregator_id == provider.id)
        .first()
    )
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    if (driver.status or "").lower() != "available":
        raise HTTPException(status_code=400, detail="Driver is not available")

    vehicle_type = (booking.vehicle_type or "").lower()
    if not vehicle_category_matches(
        db,
        provider.operating_port_id,
        vehicle_type,
        booking.vehicle_name,
        driver.vehicle_type,
    ):
        raise HTTPException(status_code=400, detail="Driver vehicle category does not match booking")

    now = datetime.utcnow()
    updated_rows = (
        db.query(CabBooking)
        .filter(
            CabBooking.id == booking.id,
            func.lower(cast(CabBooking.status, String)) == BookingStatus.PROVIDER_ACCEPTED.value,
            CabBooking.provider_id == provider.id,
        )
        .update(
            {
                CabBooking.driver_id: driver.id,
                CabBooking.assigned_driver_id: driver.id,
                CabBooking.driver_name: driver.name,
                CabBooking.driver_phone: driver.phone,
                CabBooking.driver_plate: driver.vehicle_number,
                CabBooking.driver_assigned_at: now,
                CabBooking.status: BookingStatus.DRIVER_ASSIGNED,
            },
            synchronize_session=False,
        )
    )
    if updated_rows == 0:
        raise HTTPException(status_code=409, detail="Booking is no longer in accepted state")

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.DRIVER_ASSIGNED,
        actor_id=provider.id,
        actor_type="provider",
        metadata={
            "driver_id": driver.id,
            "driver_name": driver.name,
            "vehicle_number": driver.vehicle_number,
        },
        event_time=now,
    )
    db.commit()

    try:
        from app.services.whatsapp import notify_driver_alloted
        full_booking = db.query(CabBooking).filter(CabBooking.id == booking.id).first()
        crew_user = full_booking.crew.user if full_booking and full_booking.crew else None
        pickup_address = full_booking.pickup_address if full_booking else booking.pickup_address
        notify_driver_alloted(
            crew_user.mobile_number if crew_user else None,
            full_booking.booking_id if full_booking else booking_id,
            driver.name, driver.vehicle_number, pickup_address, driver.phone,
        )
    except Exception:
        logger.exception("WhatsApp driver_alloted notify failed for booking %s", booking_id)

    updated = SimpleNamespace(
        id=booking.id,
        pickup_address=booking.pickup_address,
        pickup_lat=booking.pickup_lat,
        pickup_lng=booking.pickup_lng,
        drop_address=booking.drop_address,
        drop_lat=booking.drop_lat,
        drop_lng=booking.drop_lng,
    )
    # The trip changed hands, so the previous driver's URL must stop working.
    # Rotating the token is the revocation: booking_id is unique, so there is
    # one link per booking and the old token resolves to nothing afterwards.
    # OTP state goes with it, so the new driver verifies for themselves.
    if already_assigned_driver_id and already_assigned_driver_id != driver.id:
        rotate_magic_link(db, booking)

    magic_link = create_or_refresh_magic_link(
        db,
        booking=updated,
        aggregator_id=provider.id,
        itinerary_stops=body.itinerary_stops,
    )
    return {
        "booking_id": booking_id,
        "magic_link": {
            "token": magic_link.token,
            "path": f"/magic-link/{magic_link.token}",
        },
    }


@router.post("/{booking_id}/driver-accept")
def driver_accept_endpoint(
    booking_id: str,
    db: Session = Depends(get_db),
    current_driver: Driver = Depends(get_current_driver),
):
    booking = get_booking_by_identifier(db, booking_id)
    updated = driver_accept_booking(db, booking, current_driver)
    return serialize_booking(updated)


@router.post("/{booking_id}/start-trip")
def start_trip_endpoint(
    booking_id: str,
    db: Session = Depends(get_db),
    current_driver: Driver = Depends(get_current_driver),
):
    booking = get_booking_by_identifier(db, booking_id)
    updated = start_trip(db, booking, current_driver)
    return serialize_booking(updated)


@router.post("/{booking_id}/complete-trip")
def complete_trip_endpoint(
    booking_id: str,
    db: Session = Depends(get_db),
    current_driver: Driver = Depends(get_current_driver),
):
    booking = get_booking_by_identifier(db, booking_id)
    updated = complete_trip(db, booking, current_driver)
    return serialize_booking(updated)
