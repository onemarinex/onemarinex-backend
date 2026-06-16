from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import or_, and_, func, cast, String
from sqlalchemy.orm import Session, joinedload

from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.booking_timeline import TimelineEventType
from app.db.models.cab_booking import BookingStatus, CabBooking, RideType, VehicleType
from app.db.models.crew_profile import CrewProfile
from app.db.models.driver import Driver
from app.db.models.port import Port
from app.db.models.pricing_controls import PricingVehicleCategory
from app.db.models.user import User
from app.services.timeline_service import create_timeline_event


RIDE_TYPE_TO_PROVIDER_TYPE = {
    RideType.FLEXIBLE_RIDE: "partnered_driver",
    RideType.GUARANTEED_COORDINATED_RIDE: "aggregator",
}

# Support legacy/plural provider_type values found across environments.
RIDE_TYPE_PROVIDER_TYPE_ALIASES = {
    RideType.FLEXIBLE_RIDE: ["partnered_driver", "partner_drivers", "partner_driver"],
    RideType.GUARANTEED_COORDINATED_RIDE: ["aggregator", "aggregators"],
}

RIDE_TYPE_LABELS = {
    RideType.FLEXIBLE_RIDE: "Flexible Ride",
    RideType.GUARANTEED_COORDINATED_RIDE: "Guaranteed Coordinated Ride",
}

STATUS_LABELS = {
    BookingStatus.PENDING_PROVIDER_RESPONSE: "Pending Provider Response",
    BookingStatus.PROVIDER_ACCEPTED: "Provider Accepted",
    BookingStatus.PROVIDER_REJECTED: "Provider Rejected",
    BookingStatus.DRIVER_ASSIGNED: "Driver Assigned",
    BookingStatus.DRIVER_ACCEPTED: "Driver Accepted",
    BookingStatus.ON_TRIP: "On Trip",
    BookingStatus.COMPLETED: "Completed",
    BookingStatus.CANCELLED: "Cancelled",
}


def resolve_port(db: Session, port_value: Optional[str]) -> Optional[Port]:
    if not port_value:
        return None
    normalized = port_value.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return db.query(Port).filter(Port.id == int(normalized)).first()

    alias = normalized.lower()
    alias = alias[5:] if alias.startswith("port_") else alias
    alias = alias.replace("_", " ").strip()
    compact_alias = alias.replace(" ", "")

    return (
        db.query(Port)
        .filter(
            or_(
                Port.name.ilike(normalized),
                Port.code.ilike(normalized),
                Port.name.ilike(f"%{alias}%"),
                Port.code.ilike(f"%{alias}%"),
                func.replace(func.lower(Port.name), " ", "").ilike(f"%{compact_alias}%"),
                func.replace(func.lower(Port.code), " ", "").ilike(f"%{compact_alias}%"),
            )
        )
        .first()
    )


def provider_documents_valid(documents: Optional[Any]) -> bool:
    if not documents:
        return False
    if isinstance(documents, list):
        if not documents:
            return False
        return all(
            (doc.get("status") or doc.get("verification_status") or "valid").lower()
            in {"valid", "approved", "verified", "active"}
            for doc in documents
            if isinstance(doc, dict)
        )
    if isinstance(documents, dict):
        status = (documents.get("status") or documents.get("verification_status") or "valid").lower()
        return status in {"valid", "approved", "verified", "active"}
    return True


VEHICLE_CATEGORY_ALIASES = {
    "ac": {"ac", "cab", "car", "sedan", "hatchback", "mini", "standard", "economy"},
    "premium": {"premium", "premium_ac", "executive", "luxury"},
    "xl": {"xl", "suv", "van", "traveller", "tempo", "muv", "six_seater"},
}


def normalize_vehicle_category(value: Optional[str]) -> str:
    normalized = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if not normalized:
        return ""
    for category, aliases in VEHICLE_CATEGORY_ALIASES.items():
        if normalized == category or normalized in aliases:
            return category
        if any(alias in normalized for alias in aliases):
            return category
    return normalized


def vehicle_category_matches(
    db: Session,
    port_id: int,
    requested_vehicle_type: str,
    requested_vehicle_name: str,
    driver_vehicle_type: Optional[str],
) -> bool:
    if not driver_vehicle_type:
        return False

    requested_normalized = normalize_vehicle_category(requested_vehicle_type)
    driver_normalized = normalize_vehicle_category(driver_vehicle_type)
    if requested_normalized and requested_normalized == driver_normalized:
        return True

    categories = (
        db.query(PricingVehicleCategory)
        .filter(
            PricingVehicleCategory.port_id == port_id,
            PricingVehicleCategory.is_active == True,
        )
        .all()
    )
    if not categories:
        return requested_normalized == driver_normalized

    requested_label = f"{requested_vehicle_type} {requested_vehicle_name}".lower()
    requested_category = None
    for category in categories:
        if category.code.lower() in requested_label or category.name.lower() in requested_label:
            requested_category = category
            break
    if not requested_category:
        for category in categories:
            if requested_vehicle_type.lower() in (category.code or "").lower():
                requested_category = category
                break

    if not requested_category:
        return requested_normalized == driver_normalized

    driver_normalized = (driver_vehicle_type or "").lower()
    return (
        requested_category.name.lower() == driver_normalized
        or requested_category.code.lower() == driver_normalized
        or requested_category.name.lower() in driver_normalized
        or requested_category.code.lower() in driver_normalized
    )


def provider_has_active_drivers(provider: AggregatorProfile) -> bool:
    return any(
        (driver.status or "").strip().lower() != "offline"
        for driver in (provider.drivers or [])
    )


def provider_has_matching_drivers(
    db: Session,
    provider: AggregatorProfile,
    vehicle_type: str,
    vehicle_name: str,
) -> bool:
    active_drivers = [
        driver
        for driver in (provider.drivers or [])
        if (driver.status or "").lower() != "offline"
    ]
    if not active_drivers:
        return False
    return any(
        vehicle_category_matches(
            db,
            provider.operating_port_id,
            vehicle_type,
            vehicle_name,
            driver.vehicle_type,
        )
        for driver in active_drivers
    )


def get_eligible_providers_for_ride(
    db: Session,
    ride_type: RideType,
    port_value: Optional[str],
    vehicle_type: str,
    vehicle_name: str,
) -> List[AggregatorProfile]:
    port = resolve_port(db, port_value)
    if not port:
        return []

    provider_types = RIDE_TYPE_PROVIDER_TYPE_ALIASES.get(
        ride_type,
        [RIDE_TYPE_TO_PROVIDER_TYPE[ride_type]],
    )
    providers = (
        db.query(AggregatorProfile)
        .options(joinedload(AggregatorProfile.drivers))
        .filter(
            AggregatorProfile.operating_port_id == port.id,
            AggregatorProfile.provider_type.in_(provider_types),
            func.lower(func.coalesce(AggregatorProfile.status, "")) == "active",
        )
        .all()
    )

    matched = [
        provider
        for provider in providers
        if provider_has_matching_drivers(db, provider, vehicle_type, vehicle_name)
    ]
    if matched:
        return matched

    return [provider for provider in providers if provider_has_active_drivers(provider)]


def is_provider_eligible_for_booking(
    db: Session,
    booking: CabBooking,
    provider: AggregatorProfile,
) -> bool:
    if not booking.ride_type:
        return False

    provider_types = RIDE_TYPE_PROVIDER_TYPE_ALIASES.get(
        booking.ride_type,
        [RIDE_TYPE_TO_PROVIDER_TYPE[booking.ride_type]],
    )
    if (provider.provider_type or "") not in provider_types:
        return False

    if (provider.status or "").strip().lower() != "active":
        return False

    port = resolve_port(db, booking.port)
    if not port or provider.operating_port_id != port.id:
        return False

    provider_with_drivers = (
        db.query(AggregatorProfile)
        .options(joinedload(AggregatorProfile.drivers))
        .filter(AggregatorProfile.id == provider.id)
        .first()
    )
    if not provider_with_drivers:
        return False

    vehicle_type = booking.vehicle_type.value if booking.vehicle_type else ""
    return provider_has_matching_drivers(
        db,
        provider_with_drivers,
        vehicle_type,
        booking.vehicle_name,
    )


def is_ride_type_available(
    db: Session,
    ride_type: RideType,
    port_value: Optional[str],
    vehicle_type: str = "ac",
    vehicle_name: str = "Cab AC",
) -> bool:
    return bool(
        get_eligible_providers_for_ride(
            db,
            ride_type,
            port_value,
            vehicle_type,
            vehicle_name,
        )
    )


def get_ride_availability(db: Session, port_value: Optional[str]) -> Dict[str, Any]:
    flexible_available = is_ride_type_available(
        db, RideType.FLEXIBLE_RIDE, port_value
    )
    guaranteed_available = is_ride_type_available(
        db, RideType.GUARANTEED_COORDINATED_RIDE, port_value
    )

    available_rides = []
    if flexible_available:
        available_rides.append(
            {
                "ride_type": RideType.FLEXIBLE_RIDE.value,
                "label": RIDE_TYPE_LABELS[RideType.FLEXIBLE_RIDE],
                "description": "HeyPorts partnered drivers",
            }
        )
    if guaranteed_available:
        available_rides.append(
            {
                "ride_type": RideType.GUARANTEED_COORDINATED_RIDE.value,
                "label": RIDE_TYPE_LABELS[RideType.GUARANTEED_COORDINATED_RIDE],
                "description": "Cab aggregators",
            }
        )

    return {
        "flexible_ride_available": flexible_available,
        "guaranteed_ride_available": guaranteed_available,
        "available_rides": available_rides,
        "message": None
        if available_rides
        else "No rides available currently",
    }


def find_provider_for_ride(
    db: Session,
    ride_type: RideType,
    port_value: Optional[str],
    vehicle_type: str,
    vehicle_name: str,
) -> AggregatorProfile:
    providers = get_eligible_providers_for_ride(
        db,
        ride_type,
        port_value,
        vehicle_type,
        vehicle_name,
    )
    if not providers:
        port = resolve_port(db, port_value)
        if not port:
            raise HTTPException(status_code=400, detail="Port not found for booking")
        raise HTTPException(
            status_code=400,
            detail=(
                f"No active provider available for {RIDE_TYPE_LABELS[ride_type]}"
                f" at port '{port.name}'"
            ),
        )

    return providers[0]


def get_booking_by_identifier(db: Session, booking_identifier: str) -> CabBooking:
    query = db.query(CabBooking).options(
        joinedload(CabBooking.crew),
        joinedload(CabBooking.provider),
        joinedload(CabBooking.assigned_driver),
    )
    if booking_identifier.isdigit():
        booking = query.filter(CabBooking.id == int(booking_identifier)).first()
    else:
        booking = query.filter(CabBooking.booking_id == booking_identifier).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    return booking


def serialize_booking(booking: CabBooking) -> Dict[str, Any]:
    provider = booking.provider or (
        booking.aggregator if booking.aggregator_id else None
    )
    driver = booking.assigned_driver
    return {
        "id": booking.id,
        "booking_id": booking.booking_id,
        "ride_type": booking.ride_type.value if booking.ride_type else None,
        "ride_type_label": RIDE_TYPE_LABELS.get(booking.ride_type)
        if booking.ride_type
        else None,
        "port": booking.port,
        "crew": {
            "id": booking.crew.id if booking.crew else None,
            "name": booking.crew.full_name if booking.crew else None,
            "hp_id": booking.crew.hpid if booking.crew else None,
            "vessel": booking.crew.vessel if booking.crew else None,
        },
        "pickup_address": booking.pickup_address,
        "drop_address": booking.drop_address,
        "vehicle_type": booking.vehicle_type.value if booking.vehicle_type else None,
        "vehicle_name": booking.vehicle_name,
        "vehicle_category": booking.vehicle_category,
        "estimated_price": float(booking.estimated_price),
        "num_passengers": booking.num_passengers,
        "status": booking.status.value,
        "status_label": STATUS_LABELS.get(booking.status, booking.status.value),
        "provider_id": booking.provider_id or booking.aggregator_id,
        "provider_name": provider.company_name if provider else booking.aggregator_name,
        "provider_type": provider.provider_type if provider else None,
        "provider_response_status": booking.provider_response_status,
        "provider_response_at": booking.provider_response_at,
        "assigned_driver_id": booking.assigned_driver_id or booking.driver_id,
        "driver_name": booking.driver_name or (driver.name if driver else None),
        "driver_phone": booking.driver_phone or (driver.phone if driver else None),
        "driver_plate": booking.driver_plate or (driver.vehicle_number if driver else None),
        "driver_assigned_at": booking.driver_assigned_at,
        "driver_accepted_at": booking.driver_accepted_at,
        "trip_started_at": booking.trip_started_at or booking.started_at,
        "trip_completed_at": booking.trip_completed_at or booking.completed_at,
        "otp": booking.otp,
        "helpline_number": booking.helpline_number or booking.agent_number,
        "scheduled_time": booking.scheduled_time,
        "created_at": booking.created_at,
        "updated_at": booking.updated_at,
    }


def ensure_provider_access(booking: CabBooking, user: User) -> AggregatorProfile:
    if user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can perform this action")
    profile = user.aggregator_profile
    if not profile:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")
    provider_id = booking.provider_id or booking.aggregator_id
    if provider_id != profile.id:
        raise HTTPException(status_code=403, detail="Booking not assigned to your provider account")
    return profile


def ensure_driver_access(booking: CabBooking, driver: Driver) -> None:
    assigned_id = booking.assigned_driver_id or booking.driver_id
    if assigned_id != driver.id:
        raise HTTPException(status_code=403, detail="Ride not assigned to you")


def get_eligible_drivers(
    db: Session,
    booking: CabBooking,
    provider: AggregatorProfile,
) -> List[Driver]:
    drivers = (
        db.query(Driver)
        .filter(Driver.aggregator_id == provider.id)
        .all()
    )
    eligible = []
    for driver in drivers:
        if (driver.status or "").lower() != "available":
            continue
        if not vehicle_category_matches(
            db,
            provider.operating_port_id,
            booking.vehicle_type.value,
            booking.vehicle_name,
            driver.vehicle_type,
        ):
            continue
        eligible.append(driver)
    return eligible


def accept_booking(db: Session, booking: CabBooking, user: User) -> CabBooking:
    if user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can perform this action")
    profile = user.aggregator_profile
    if not profile:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    now = datetime.utcnow()
    pending_status = BookingStatus.PENDING_PROVIDER_RESPONSE.value
    rejected_status = BookingStatus.PROVIDER_REJECTED.value
    updated_rows = (
        db.query(CabBooking)
        .filter(
            CabBooking.id == booking.id,
            or_(
                and_(
                    func.lower(cast(CabBooking.status, String)) == pending_status,
                    CabBooking.provider_id.is_(None),
                    CabBooking.aggregator_id.is_(None),
                ),
                and_(
                    func.lower(cast(CabBooking.status, String)) == rejected_status,
                    CabBooking.driver_id.is_(None),
                    CabBooking.assigned_driver_id.is_(None),
                ),
            ),
        )
        .update(
            {
                CabBooking.status: BookingStatus.PROVIDER_ACCEPTED,
                CabBooking.provider_response_status: "accepted",
                CabBooking.provider_response_at: now,
                CabBooking.provider_id: profile.id,
                CabBooking.aggregator_id: profile.id,
                CabBooking.aggregator_name: profile.company_name,
            },
            synchronize_session=False,
        )
    )

    if updated_rows == 0:
        latest = (
            db.query(
                CabBooking.provider_id,
                CabBooking.aggregator_id,
                cast(CabBooking.status, String).label("status"),
            )
            .filter(CabBooking.id == booking.id)
            .first()
        )
        if latest and (latest.provider_id or latest.aggregator_id):
            raise HTTPException(status_code=409, detail="Booking already accepted by another provider")
        status_value = ((latest.status or "") if latest else "").lower()
        raise HTTPException(
            status_code=400,
            detail=f"Booking cannot be accepted in current state: status={status_value or 'unknown'}",
        )

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.PROVIDER_ACCEPTED,
        actor_id=profile.id,
        actor_type="provider",
        metadata={"provider_name": profile.company_name},
        event_time=now,
    )
    db.commit()
    refreshed = db.query(CabBooking).filter(CabBooking.id == booking.id).first()
    if not refreshed:
        raise HTTPException(status_code=404, detail="Booking not found after acceptance")
    return refreshed


def reject_booking(db: Session, booking: CabBooking, user: User) -> CabBooking:
    profile = ensure_provider_access(booking, user)
    if booking.status != BookingStatus.PENDING_PROVIDER_RESPONSE:
        raise HTTPException(status_code=400, detail="Booking is not awaiting provider response")

    now = datetime.utcnow()
    booking.status = BookingStatus.PROVIDER_REJECTED
    booking.provider_response_status = "rejected"
    booking.provider_response_at = now
    booking.provider_id = profile.id
    booking.aggregator_id = profile.id

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.PROVIDER_REJECTED,
        actor_id=profile.id,
        actor_type="provider",
        metadata={"provider_name": profile.company_name},
        event_time=now,
    )
    db.commit()
    db.refresh(booking)
    return booking


def assign_driver_to_booking(
    db: Session,
    booking: CabBooking,
    user: User,
    driver_id: int,
) -> CabBooking:
    profile = ensure_provider_access(booking, user)
    if booking.status != BookingStatus.PROVIDER_ACCEPTED:
        raise HTTPException(status_code=400, detail="Booking must be accepted before assigning a driver")

    driver = (
        db.query(Driver)
        .filter(Driver.id == driver_id, Driver.aggregator_id == profile.id)
        .first()
    )
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")

    if (driver.status or "").lower() != "available":
        raise HTTPException(status_code=400, detail="Driver is not available")

    if not vehicle_category_matches(
        db,
        profile.operating_port_id,
        booking.vehicle_type.value,
        booking.vehicle_name,
        driver.vehicle_type,
    ):
        raise HTTPException(status_code=400, detail="Driver vehicle category does not match booking")

    now = datetime.utcnow()
    booking.driver_id = driver.id
    booking.assigned_driver_id = driver.id
    booking.driver_name = driver.name
    booking.driver_phone = driver.phone
    booking.driver_plate = driver.vehicle_number
    booking.driver_assigned_at = now
    booking.status = BookingStatus.DRIVER_ASSIGNED

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.DRIVER_ASSIGNED,
        actor_id=profile.id,
        actor_type="provider",
        metadata={
            "driver_id": driver.id,
            "driver_name": driver.name,
            "vehicle_number": driver.vehicle_number,
        },
        event_time=now,
    )
    db.commit()
    db.refresh(booking)
    return booking


def driver_accept_booking(db: Session, booking: CabBooking, driver: Driver) -> CabBooking:
    ensure_driver_access(booking, driver)
    if booking.status != BookingStatus.DRIVER_ASSIGNED:
        raise HTTPException(status_code=400, detail="Booking is not awaiting driver acceptance")

    now = datetime.utcnow()
    booking.status = BookingStatus.DRIVER_ACCEPTED
    booking.driver_accepted_at = now

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.DRIVER_ACCEPTED,
        actor_id=driver.id,
        actor_type="driver",
        metadata={"driver_name": driver.name},
        event_time=now,
    )
    db.commit()
    db.refresh(booking)
    return booking


def start_trip(db: Session, booking: CabBooking, driver: Driver) -> CabBooking:
    ensure_driver_access(booking, driver)
    if booking.status not in {BookingStatus.DRIVER_ACCEPTED, BookingStatus.DRIVER_ASSIGNED}:
        raise HTTPException(status_code=400, detail="Trip cannot be started in current status")

    now = datetime.utcnow()
    booking.status = BookingStatus.ON_TRIP
    booking.trip_started_at = now
    booking.started_at = now
    driver.status = "Busy"

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.TRIP_STARTED,
        actor_id=driver.id,
        actor_type="driver",
        event_time=now,
    )
    db.commit()
    db.refresh(booking)
    return booking


def complete_trip(db: Session, booking: CabBooking, driver: Driver) -> CabBooking:
    ensure_driver_access(booking, driver)
    if booking.status != BookingStatus.ON_TRIP:
        raise HTTPException(status_code=400, detail="Trip is not active")

    now = datetime.utcnow()
    booking.status = BookingStatus.COMPLETED
    booking.trip_completed_at = now
    booking.completed_at = now
    driver.status = "Available"

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.TRIP_COMPLETED,
        actor_id=driver.id,
        actor_type="driver",
        event_time=now,
    )
    db.commit()
    db.refresh(booking)
    return booking


def list_bookings_for_user(
    db: Session,
    user: User,
    *,
    status: Optional[str] = None,
    provider_id: Optional[int] = None,
    provider_type: Optional[str] = None,
    port: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[CabBooking]:
    query = db.query(CabBooking).options(
        joinedload(CabBooking.crew),
        joinedload(CabBooking.provider),
        joinedload(CabBooking.assigned_driver),
    )

    if user.role == "crew":
        profile = db.query(CrewProfile).filter(CrewProfile.user_id == user.id).first()
        if not profile:
            return []
        query = query.filter(CabBooking.crew_id == profile.id)
    elif user.role == "aggregator":
        profile = user.aggregator_profile
        if not profile:
            return []
        query = query.filter(
            or_(
                CabBooking.provider_id == profile.id,
                CabBooking.aggregator_id == profile.id,
            )
        )
    elif user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Unauthorized to list bookings")

    if status:
        try:
            query = query.filter(CabBooking.status == BookingStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    if provider_id:
        query = query.filter(
            or_(
                CabBooking.provider_id == provider_id,
                CabBooking.aggregator_id == provider_id,
            )
        )

    if provider_type:
        query = query.join(
            AggregatorProfile,
            or_(
                CabBooking.provider_id == AggregatorProfile.id,
                CabBooking.aggregator_id == AggregatorProfile.id,
            ),
        ).filter(AggregatorProfile.provider_type == provider_type)

    if port:
        query = query.filter(CabBooking.port.ilike(f"%{port}%"))

    if date_from:
        query = query.filter(CabBooking.created_at >= date_from)
    if date_to:
        query = query.filter(CabBooking.created_at <= date_to)

    return query.order_by(CabBooking.created_at.desc()).all()


def get_dashboard_metrics(db: Session, port_id: Optional[int] = None) -> Dict[str, int]:
    query = db.query(CabBooking)
    if port_id:
        port_obj = db.query(Port).filter(Port.id == port_id).first()
        if port_obj:
            query = query.filter(CabBooking.port.ilike(f"%{port_obj.name}%"))

    return {
        "pending_provider_response": query.filter(
            CabBooking.status == BookingStatus.PENDING_PROVIDER_RESPONSE
        ).count(),
        "accepted_by_provider": query.filter(
            CabBooking.status == BookingStatus.PROVIDER_ACCEPTED
        ).count(),
        "rejected_by_provider": query.filter(
            CabBooking.status == BookingStatus.PROVIDER_REJECTED
        ).count(),
        "assigned_trips": query.filter(
            CabBooking.status.in_(
                [
                    BookingStatus.DRIVER_ASSIGNED,
                    BookingStatus.DRIVER_ACCEPTED,
                ]
            )
        ).count(),
        "active_trips": query.filter(CabBooking.status == BookingStatus.ON_TRIP).count(),
        "completed_trips": query.filter(
            CabBooking.status == BookingStatus.COMPLETED
        ).count(),
        "cancelled_trips": query.filter(
            CabBooking.status.in_(
                [BookingStatus.CANCELLED, BookingStatus.PROVIDER_REJECTED]
            )
        ).count(),
    }


def get_provider_dashboard_metrics(db: Session, provider: AggregatorProfile) -> Dict[str, int]:
    base = db.query(CabBooking).filter(
        or_(
            CabBooking.provider_id == provider.id,
            CabBooking.aggregator_id == provider.id,
        )
    )
    return {
        "new_requests": base.filter(
            CabBooking.status == BookingStatus.PENDING_PROVIDER_RESPONSE
        ).count(),
        "accepted": base.filter(
            CabBooking.status == BookingStatus.PROVIDER_ACCEPTED
        ).count(),
        "active_trips": base.filter(CabBooking.status == BookingStatus.ON_TRIP).count(),
        "completed": base.filter(CabBooking.status == BookingStatus.COMPLETED).count(),
        "cancelled": base.filter(
            CabBooking.status.in_(
                [BookingStatus.CANCELLED, BookingStatus.PROVIDER_REJECTED]
            )
        ).count(),
        "assigned": base.filter(
            CabBooking.status.in_(
                [BookingStatus.DRIVER_ASSIGNED, BookingStatus.DRIVER_ACCEPTED]
            )
        ).count(),
    }
