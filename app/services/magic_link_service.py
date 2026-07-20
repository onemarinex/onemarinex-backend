import secrets
import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
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
            generic_name = bool(re.match(r"^(drop|stop|destination)$", stop_name, flags=re.IGNORECASE))
            for part_idx, part in enumerate(split_parts):
                split_id = f"{stop_id}__{part_idx + 1}"
                while split_id in used_ids:
                    split_id = f"{split_id}_x"
                used_ids.add(split_id)
                normalized.append(
                    {
                        "id": split_id,
                        "name": "Stop" if generic_name else part,
                        "address": part,
                        "lat": stop.get("lat"),
                        "lng": stop.get("lng"),
                        "type": stop_type,
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


def create_or_refresh_magic_link(
    db: Session,
    booking: CabBooking,
    aggregator_id: Optional[int],
    itinerary_stops: Optional[List[Dict[str, Any]]] = None,
) -> DriverMagicLink:
    token = secrets.token_urlsafe(24)
    stops = _normalize_itinerary_stops(booking, itinerary_stops)

    magic_link = db.query(DriverMagicLink).filter(DriverMagicLink.booking_id == booking.id).first()
    if magic_link:
        magic_link.token = token
        magic_link.created_by_aggregator_id = aggregator_id
        magic_link.itinerary_stops = stops
    else:
        magic_link = DriverMagicLink(
            booking_id=booking.id,
            token=token,
            created_by_aggregator_id=aggregator_id,
            itinerary_stops=stops,
        )
        db.add(magic_link)

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
        "booking_status": booking.status.value if booking.status else None,
        "magic_token": magic_link.token,
        "fare": {
            "estimated_price": float(booking.estimated_price) if booking.estimated_price else None,
            "distance_km": float(booking.distance_km) if booking.distance_km else None,
            "vehicle_type": booking.vehicle_type.value if booking.vehicle_type else None,
            "vehicle_name": booking.vehicle_name,
            "ride_type": booking.ride_type.value if booking.ride_type else None,
            "num_passengers": booking.num_passengers,
        },
        "crew": {
            "name": booking.crew.full_name if booking.crew else None,
            "hp_id": booking.crew.hpid if booking.crew else None,
            "vessel": booking.crew.vessel if booking.crew else None,
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
) -> DriverMagicLinkReachEvent:
    matching_stop = None
    for stop in magic_link.itinerary_stops or []:
        if str(stop.get("id")) == stop_id:
            matching_stop = stop
            break

    if not matching_stop:
        raise HTTPException(status_code=404, detail="Stop not found in itinerary")

    event = DriverMagicLinkReachEvent(
        magic_link_id=magic_link.id,
        stop_id=stop_id,
        stop_name=str(matching_stop.get("name") or stop_id),
        latitude=latitude,
        longitude=longitude,
        notes=notes,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


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
