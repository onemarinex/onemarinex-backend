from datetime import datetime
from typing import Any, Dict, List, Optional
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_driver
from app.api.v1.routes_auth import get_current_user
from app.db.models.cab_booking import CabBooking
from app.db.models.driver import Driver
from app.db.models.user import User
from app.db.session import get_db
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
    serialize_booking,
    start_trip,
)
from app.services.magic_link_service import (
    create_or_refresh_magic_link,
    get_magic_link_by_token,
    mark_stop_reached,
    serialize_magic_link_public_payload,
)
from app.services.timeline_service import get_booking_timeline

router = APIRouter()


def _resolve_booking_db_id(db: Session, booking_identifier: str) -> int:
    query = db.query(CabBooking.id)
    if booking_identifier.isdigit():
        row = query.filter(CabBooking.id == int(booking_identifier)).first()
    else:
        row = query.filter(CabBooking.booking_id == booking_identifier).first()
    if not row:
        raise HTTPException(status_code=404, detail="Booking not found")
    return int(row.id)


class AssignDriverIn(BaseModel):
    driver_id: int
    itinerary_stops: Optional[List[Dict[str, Any]]] = None


class MarkReachedIn(BaseModel):
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
    mark_stop_reached(
        db,
        magic_link=magic_link,
        stop_id=stop_id,
        latitude=body.latitude,
        longitude=body.longitude,
        notes=body.notes,
    )
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
    booking = get_booking_by_identifier(db, booking_id)
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can view eligible drivers")

    provider = current_user.aggregator_profile
    if not provider:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    provider_id = booking.provider_id or booking.aggregator_id
    if provider_id != provider.id:
        raise HTTPException(status_code=403, detail="Unauthorized")

    drivers = get_eligible_drivers(db, booking, provider)
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
        for driver in drivers
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
    booking = get_booking_by_identifier(db, booking_id)
    updated = reject_booking(db, booking, current_user)
    return serialize_booking(updated)


@router.post("/{booking_id}/assign-driver")
def assign_driver_endpoint(
    booking_id: str,
    body: AssignDriverIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    booking = get_booking_by_identifier(db, booking_id)
    updated = assign_driver_to_booking(db, booking, current_user, body.driver_id)
    magic_link = create_or_refresh_magic_link(
        db,
        booking=updated,
        aggregator_id=current_user.aggregator_profile.id if current_user.aggregator_profile else None,
        itinerary_stops=body.itinerary_stops,
    )
    return {
        "booking": serialize_booking(updated),
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
