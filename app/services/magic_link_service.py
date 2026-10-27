import os
import secrets
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.models.cab_booking import CabBooking
from app.db.models.driver_magic_link import DriverMagicLink, DriverMagicLinkReachEvent


_STOP_SPLIT_REGEX = re.compile(r"\s*(?:->|→)\s*")


def _split_compound_stop_parts(name: str, address: str) -> List[str]:
    source = name if re.search(r"->|→", name) else address if re.search(r"->|→", address) else ""
    if not source:
        return []
    return [part.strip() for part in _STOP_SPLIT_REGEX.split(source) if part and part.strip()]


def _default_itinerary_stops(booking: CabBooking) -> List[Dict[str, Any]]:
    return [
        {
            "id": "pickup",
            "name": "Pickup",
            "address": booking.pickup_address,
            "lat": booking.pickup_lat,
            "lng": booking.pickup_lng,
            "type": "pickup",
        },
        {
            "id": "drop",
            "name": "Drop",
            "address": booking.drop_address,
            "lat": booking.drop_lat,
            "lng": booking.drop_lng,
            "type": "drop",
        },
        {
            "id": "trip_end",
            "name": "Trip End (Port)",
            "address": booking.pickup_address,
            "lat": booking.pickup_lat,
            "lng": booking.pickup_lng,
            "type": "trip_end",
        },
    ]


def _normalize_itinerary_stops(
    booking: CabBooking,
    itinerary_stops: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    if not itinerary_stops:
        return _default_itinerary_stops(booking)

    normalized: List[Dict[str, Any]] = []
    used_ids: set[str] = set()
    for idx, stop in enumerate(itinerary_stops):
        if not isinstance(stop, dict):
            continue
        stop_name = str(stop.get("name") or stop.get("address") or f"Stop {idx + 1}").strip()
        stop_address = str(stop.get("address") or "").strip() or None
        stop_type = str(stop.get("type") or "facility").strip().lower()
        stop_id = str(stop.get("id") or f"stop_{idx + 1}").strip() or f"stop_{idx + 1}"

        split_parts = _split_compound_stop_parts(stop_name, stop_address or "")
        if len(split_parts) > 1:
            for part_idx, part in enumerate(split_parts):
                split_id = f"{stop_id}__{part_idx + 1}"
                while split_id in used_ids:
                    split_id = f"{split_id}_x"
                used_ids.add(split_id)
                normalized.append(
                    {
                        "id": split_id,
                        "name": part,
                        "address": part,
                        "lat": stop.get("lat"),
                        "lng": stop.get("lng"),
                        "type": "facility" if stop_type in ("drop", "waypoint") else stop_type,
                    }
                )
            continue

        while stop_id in used_ids:
            stop_id = f"{stop_id}_x"
        used_ids.add(stop_id)
        normalized.append(
            {
                "id": stop_id,
                "name": stop_name,
                "address": stop_address,
                "lat": stop.get("lat"),
                "lng": stop.get("lng"),
                "type": stop_type,
            }
        )

    if not normalized:
        return _default_itinerary_stops(booking)

    has_pickup = any(str(stop.get("type") or "").strip().lower() == "pickup" for stop in normalized)
    if not has_pickup:
        normalized.insert(
            0,
            {
                "id": "pickup",
                "name": "Pickup",
                "address": booking.pickup_address,
                "lat": booking.pickup_lat,
                "lng": booking.pickup_lng,
                "type": "pickup",
            },
        )

    has_trip_end = any(
        str(stop.get("type") or "").strip().lower() == "trip_end"
        or str(stop.get("id") or "").strip().lower() == "trip_end"
        for stop in normalized
    )
    if not has_trip_end:
        normalized.append(
            {
                "id": "trip_end",
                "name": "Trip End (Port)",
                "address": booking.pickup_address,
                "lat": booking.pickup_lat,
                "lng": booking.pickup_lng,
                "type": "trip_end",
            }
        )

    return normalized



# How long a driver trip link stays usable. A link covers one trip, including a
# trip scheduled a day or two ahead, so three days is generous rather than
# tight. Configurable because the right answer is operational, not structural.
MAGIC_LINK_TTL_HOURS = int(os.getenv("MAGIC_LINK_TTL_HOURS", "72"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def link_expires_at(magic_link: DriverMagicLink) -> datetime:
    """When this link stops working.

    A null `expires_at` is not "never". Links created before expiry existed
    carry one, and treating those as immortal would leave exactly the holders
    the audit was about. They fall back to creation plus the TTL, so the policy
    reaches them without rewriting a row.
    """
    explicit = _aware(magic_link.expires_at)
    if explicit is not None:
        return explicit
    created = _aware(magic_link.created_at) or _utc_now()
    return created + timedelta(hours=MAGIC_LINK_TTL_HOURS)


def assert_link_usable(magic_link: DriverMagicLink) -> None:
    """Expiry and revocation, checked on every read and every write.

    Raised as 410 Gone rather than 404: the link existed and is finished, and a
    driver holding a dead URL should be told that rather than left guessing
    whether they mistyped it. It leaks nothing a 404 would not — the token had
    to be correct to get this far.
    """
    if _aware(magic_link.revoked_at) is not None:
        raise HTTPException(
            status_code=410,
            detail="This trip link has been revoked. Ask the provider for a new one.",
        )
    if _utc_now() >= link_expires_at(magic_link):
        raise HTTPException(
            status_code=410,
            detail="This trip link has expired. Ask the provider for a new one.",
        )


def revoke_magic_link(db: Session, booking: CabBooking) -> Optional[DriverMagicLink]:
    """Kill a link without replacing it."""
    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.booking_id == booking.id)
        .first()
    )
    if magic_link is None or magic_link.revoked_at is not None:
        return magic_link
    magic_link.revoked_at = _utc_now()
    db.commit()
    db.refresh(magic_link)
    return magic_link


def rotate_magic_link(db: Session, booking: CabBooking) -> Optional[DriverMagicLink]:
    """Reset authorization when the trip changes hands.

    `booking_id` is unique, so there is one link per booking and rotation
    replaces its token in place. The previous driver's URL then resolves to
    nothing, which is the revocation. OTP state is cleared with it, so the new
    holder verifies for themselves rather than inheriting a verified session.
    """
    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.booking_id == booking.id)
        .first()
    )
    if magic_link is None:
        return None
    magic_link.token = secrets.token_urlsafe(24)
    magic_link.otp_verified_at = None
    magic_link.otp_failed_attempts = 0
    magic_link.otp_last_attempt_at = None
    magic_link.otp_locked_until = None
    magic_link.revoked_at = None
    magic_link.expires_at = _utc_now() + timedelta(hours=MAGIC_LINK_TTL_HOURS)
    db.commit()
    db.refresh(magic_link)
    return magic_link


def create_or_refresh_magic_link(
    db: Session,
    booking: CabBooking,
    aggregator_id: Optional[int],
    itinerary_stops: Optional[List[Dict[str, Any]]] = None,
) -> DriverMagicLink:
    stops = _normalize_itinerary_stops(booking, itinerary_stops)

    # Serialize creation per booking before generating/resetting anything. The
    # unique constraint remains the final boundary for old/non-Postgres DBs.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        from sqlalchemy import text

        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": int(booking.id)},
        )
    magic_link = (
        db.query(DriverMagicLink)
        .filter(DriverMagicLink.booking_id == booking.id)
        .first()
    )
    if magic_link:
        # Idempotent retries return the original URL. Rotating its token would
        # invalidate a link the driver may already have open.
        if magic_link.created_by_aggregator_id is None:
            magic_link.created_by_aggregator_id = aggregator_id
        magic_link.itinerary_stops = stops
        # A refresh means the trip is live again, so the clock restarts rather
        # than the link quietly ageing out mid-trip.
        magic_link.expires_at = _utc_now() + timedelta(hours=MAGIC_LINK_TTL_HOURS)
    else:
        magic_link = DriverMagicLink(
            booking_id=booking.id,
            token=secrets.token_urlsafe(24),
            created_by_aggregator_id=aggregator_id,
            itinerary_stops=stops,
            expires_at=_utc_now() + timedelta(hours=MAGIC_LINK_TTL_HOURS),
        )
        db.add(magic_link)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        magic_link = (
            db.query(DriverMagicLink)
            .filter(DriverMagicLink.booking_id == booking.id)
            .first()
        )
        if magic_link is None:
            raise exc
        magic_link.itinerary_stops = stops
        if magic_link.created_by_aggregator_id is None:
            magic_link.created_by_aggregator_id = aggregator_id
        db.commit()
    db.refresh(magic_link)
    return magic_link


def get_magic_link_by_token(db: Session, token: str) -> DriverMagicLink:
    magic_link = (
        db.query(DriverMagicLink)
        .options(
            joinedload(DriverMagicLink.booking).joinedload(CabBooking.crew),
            joinedload(DriverMagicLink.booking).joinedload(CabBooking.assigned_driver),
            joinedload(DriverMagicLink.booking).joinedload(CabBooking.aggregator),
            joinedload(DriverMagicLink.reach_events),
        )
        .filter(DriverMagicLink.token == token)
        .first()
    )
    if not magic_link:
        raise HTTPException(status_code=404, detail="Magic link not found")
    # Every public read and write resolves the token through here, so this is
    # the one place expiry and revocation have to hold.
    assert_link_usable(magic_link)

    # Backfill older links that stored clubbed stop strings so each stop can
    # be marked independently with a real stop id.
    if magic_link.booking:
        normalized = _normalize_itinerary_stops(
            magic_link.booking,
            magic_link.itinerary_stops,
        )
        if normalized != (magic_link.itinerary_stops or []):
            magic_link.itinerary_stops = normalized
            db.commit()
            db.refresh(magic_link)

    return magic_link


def _stop_reach_lookup(magic_link: DriverMagicLink) -> Dict[str, DriverMagicLinkReachEvent]:
    lookup: Dict[str, DriverMagicLinkReachEvent] = {}
    for event in magic_link.reach_events or []:
        if event.stop_id not in lookup:
            lookup[event.stop_id] = event
    return lookup


def serialize_magic_link_public_payload(magic_link: DriverMagicLink) -> Dict[str, Any]:
    booking = magic_link.booking
    if not booking:
        raise HTTPException(status_code=404, detail="Linked booking not found")

    def enum_or_value(value: Any) -> Any:
        """Serialize both current SQLAlchemy enums and legacy string rows."""
        return getattr(value, "value", value) if value is not None else None

    reached_by_stop = _stop_reach_lookup(magic_link)
    itinerary = []
    for stop in (magic_link.itinerary_stops or []):
        stop_id = str(stop.get("id") or "")
        reached_event = reached_by_stop.get(stop_id)
        itinerary.append(
            {
                "id": stop_id,
                "name": stop.get("name"),
                "address": stop.get("address"),
                "lat": stop.get("lat"),
                "lng": stop.get("lng"),
                "type": stop.get("type"),
                "reached": reached_event is not None,
                "reached_at": reached_event.reached_at if reached_event else None,
                "reached_location": {
                    "lat": reached_event.latitude,
                    "lng": reached_event.longitude,
                }
                if reached_event
                else None,
            }
        )

    return {
        "booking_id": booking.booking_id,
        "booking_status": enum_or_value(booking.status),
        "otp_verified": magic_link.otp_verified_at is not None,
        "magic_token": magic_link.token,
        "fare": {
            "estimated_price": float(booking.estimated_price) if booking.estimated_price else None,
            "distance_km": float(booking.distance_km) if booking.distance_km else None,
            "vehicle_type": enum_or_value(booking.vehicle_type),
            "vehicle_name": booking.vehicle_name,
            "ride_type": enum_or_value(booking.ride_type),
            "num_passengers": booking.num_passengers,
        },
        "crew": {
            "name": booking.crew.full_name if booking.crew else None,
            # The opaque per-assignment reference, so a driver can confirm the
            # passenger without being handed the passport number the legacy
            # hp_id embeds. Empty for a booking with no assignment, or one whose
            # assignment predates the reference.
            "reference": (
                booking.crew_assignment.public_ref
                if booking.crew_assignment
                else None
            ),
            "vessel": (
                booking.vessel_call.vessel_name if booking.vessel_call else None
            ),
        },
        "aggregator": {
            "name": booking.aggregator_name,
        },
        "driver": {
            "name": booking.driver_name,
            "phone": booking.driver_phone,
            "vehicle": booking.driver_plate,
        },
        "pickup": {
            "address": booking.pickup_address,
            "lat": booking.pickup_lat,
            "lng": booking.pickup_lng,
        },
        "drop": {
            "address": booking.drop_address,
            "lat": booking.drop_lat,
            "lng": booking.drop_lng,
        },
        "itinerary": itinerary,
        "events": [
            {
                "id": event.id,
                "stop_id": event.stop_id,
                "stop_name": event.stop_name,
                "latitude": event.latitude,
                "longitude": event.longitude,
                "notes": event.notes,
                "reached_at": event.reached_at,
            }
            for event in (magic_link.reach_events or [])
        ],
    }


def mark_stop_reached(
    db: Session,
    magic_link: DriverMagicLink,
    stop_id: str,
    latitude: float,
    longitude: float,
    notes: Optional[str] = None,
) -> Tuple[DriverMagicLinkReachEvent, bool]:
    matching_stop = None
    for stop in magic_link.itinerary_stops or []:
        if str(stop.get("id")) == stop_id:
            matching_stop = stop
            break

    if not matching_stop:
        raise HTTPException(status_code=404, detail="Stop not found in itinerary")

    existing_event = (
        db.query(DriverMagicLinkReachEvent)
        .filter(
            DriverMagicLinkReachEvent.magic_link_id == magic_link.id,
            DriverMagicLinkReachEvent.stop_id == stop_id,
        )
        .first()
    )
    if existing_event:
        return existing_event, False

    event = DriverMagicLinkReachEvent(
        magic_link_id=magic_link.id,
        stop_id=stop_id,
        stop_name=str(matching_stop.get("name") or stop_id),
        latitude=latitude,
        longitude=longitude,
        notes=notes,
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent retry may have inserted the same stop after the lookup.
        # The database constraint is the final idempotency boundary.
        db.rollback()
        existing_event = (
            db.query(DriverMagicLinkReachEvent)
            .filter(
                DriverMagicLinkReachEvent.magic_link_id == magic_link.id,
                DriverMagicLinkReachEvent.stop_id == stop_id,
            )
            .first()
        )
        if existing_event:
            return existing_event, False
        raise
    db.refresh(event)
    return event, True


def add_stop_to_magic_link(
    db: Session,
    magic_link: DriverMagicLink,
    *,
    name: str,
    address: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    stop_type: str = "facility",
) -> DriverMagicLink:
    existing = list(magic_link.itinerary_stops or [])
    stop_id = f"stop_{len(existing) + 1}"
    new_stop = {
        "id": stop_id,
        "name": name.strip(),
        "address": (address or "").strip() or None,
        "lat": latitude,
        "lng": longitude,
        "type": (stop_type or "facility").strip().lower(),
    }
    existing.append(new_stop)
    magic_link.itinerary_stops = _normalize_itinerary_stops(magic_link.booking, existing)
    db.commit()
    db.refresh(magic_link)
    return magic_link
