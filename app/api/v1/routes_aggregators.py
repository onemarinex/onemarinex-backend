from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, String, or_, and_
from typing import List, Optional, Dict, Any
from datetime import datetime, date

from app.db.session import get_db
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.driver_magic_link import DriverMagicLink
from app.db.models.driver import Driver
from app.db.models.crew_profile import CrewProfile
from app.db.models.pricing_controls import PricingDuration, PricingRideType, PricingRule, PricingVehicleCategory
from app.db.models.booking_timeline import BookingTimeline, TimelineEventType
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from pydantic import BaseModel, EmailStr
from app.services.timeline_service import create_timeline_event

router = APIRouter()

PRICING_PROVIDER_TYPE_BY_PROFILE_TYPE = {
    "partnered_driver": "partner_drivers",
    "aggregator": "aggregators",
}

# --- Schemas ---

class CrewShortOut(BaseModel):
    name: str
    hp_id: str
    vessel: str

class BookingDashboardOut(BaseModel):
    id: int
    booking_id: str
    crew: CrewShortOut
    pickup_address: str
    drop_address: str
    vehicle_name: str
    vehicle_type: Optional[str] = None
    ride_type: Optional[str] = None
    ride_type_label: Optional[str] = None
    estimated_price: float
    status: str
    status_label: Optional[str] = None
    created_at: datetime
    scheduled_time: Optional[datetime]
    driver_name: Optional[str]
    num_passengers: int
    magic_link_path: Optional[str] = None

class AggregatorDashboardData(BaseModel):
    stats: Dict[str, int]
    new_requests: List[BookingDashboardOut]
    accepted_requests: List[BookingDashboardOut]
    active_trips: List[BookingDashboardOut]
    completed_requests: List[BookingDashboardOut]
    cancelled_requests: List[BookingDashboardOut]
    pending_requests: List[BookingDashboardOut]

class DriverAssignIn(BaseModel):
    booking_id: str
    driver_id: int
    itinerary_stops: Optional[List[Dict[str, Any]]] = None

class AggregatorProfileOut(BaseModel):
    id: int
    name: Optional[str]
    email: EmailStr
    mobile_number: Optional[str]
    company_name: str
    contact_person: Optional[str]
    operating_port: str
    gst_number: Optional[str]
    status: str
    profile_image: Optional[str]
    aggregator_identifier: Optional[str]

    class Config:
        from_attributes = True

# --- Routes ---

@router.get("/profile", response_model=AggregatorProfileOut)
def get_aggregator_profile(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only aggregators can access this profile")
    
    agg_profile = current_user.aggregator_profile
    if not agg_profile:
        raise HTTPException(status_code=404, detail="Aggregator profile not found")
    
    return {
        "id": agg_profile.id,
        "name": current_user.name,
        "email": current_user.email,
        "mobile_number": current_user.mobile_number,
        "company_name": agg_profile.company_name,
        "contact_person": agg_profile.contact_person,
        "operating_port": agg_profile.operating_port.name,
        "gst_number": agg_profile.gst_number,
        "status": agg_profile.status,
        "profile_image": agg_profile.profile_image,
        "aggregator_identifier": agg_profile.aggregator_identifier
	    }


@router.get("/pricing-plans")
def get_pricing_plans(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can view pricing")

    agg_profile = current_user.aggregator_profile
    if not agg_profile:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    pricing_provider_type = PRICING_PROVIDER_TYPE_BY_PROFILE_TYPE.get(
        agg_profile.provider_type or "aggregator",
        "aggregators",
    )

    ride_types = (
        db.query(PricingRideType)
        .filter(PricingRideType.code.in_(["coordinated_transfer", "package_trip"]))
        .order_by(PricingRideType.sort_order.asc())
        .all()
    )
    ride_type_by_id = {ride.id: ride for ride in ride_types}
    vehicle_categories = (
        db.query(PricingVehicleCategory)
        .filter(
            PricingVehicleCategory.port_id == agg_profile.operating_port_id,
            PricingVehicleCategory.is_active == True,
        )
        .order_by(PricingVehicleCategory.name.asc())
        .all()
    )
    vehicle_by_id = {vehicle.id: vehicle for vehicle in vehicle_categories}
    durations = (
        db.query(PricingDuration)
        .filter(
            PricingDuration.port_id == agg_profile.operating_port_id,
            PricingDuration.is_active == True,
        )
        .order_by(PricingDuration.sort_order.asc(), PricingDuration.duration_minutes.asc())
        .all()
    )
    duration_by_id = {duration.id: duration for duration in durations}
    rules = (
        db.query(PricingRule)
        .filter(
            PricingRule.port_id == agg_profile.operating_port_id,
            PricingRule.provider_type == pricing_provider_type,
            PricingRule.is_archived == False,
        )
        .order_by(PricingRule.updated_at.desc())
        .all()
    )

    def serialize_rule(rule: PricingRule):
        ride_type = ride_type_by_id.get(rule.ride_type_id)
        vehicle = vehicle_by_id.get(rule.vehicle_category_id)
        duration = duration_by_id.get(rule.duration_id) if rule.duration_id else None
        return {
            "id": rule.id,
            "ride_type_code": ride_type.code if ride_type else None,
            "ride_type_name": ride_type.name if ride_type else None,
            "vehicle_category_id": rule.vehicle_category_id,
            "vehicle_category_name": vehicle.name if vehicle else None,
            "duration_name": duration.name if duration else None,
            "base_fare": rule.base_fare,
            "minimum_fare": rule.minimum_fare,
            "price_per_km": rule.price_per_km,
            "price_per_minute": rule.price_per_minute,
            "free_waiting_minutes": rule.free_waiting_minutes,
            "extra_waiting_charge": rule.extra_waiting_charge,
            "cancellation_fee": rule.cancellation_fee,
            "included_km": rule.included_km,
            "price_per_extra_km": rule.price_per_extra_km,
            "price_per_extra_minute": rule.price_per_extra_minute,
            "price_per_extra_stop": rule.price_per_extra_stop,
            "platform_commission_pct": rule.platform_commission_pct,
            "is_active": rule.is_active,
        }

    return {
        "provider": {
            "id": agg_profile.id,
            "name": agg_profile.company_name,
            "provider_type": agg_profile.provider_type or "aggregator",
            "pricing_provider_type": pricing_provider_type,
            "port_id": agg_profile.operating_port_id,
            "port_name": agg_profile.operating_port.name if agg_profile.operating_port else None,
        },
        "vehicle_categories": [
            {
                "id": vehicle.id,
                "code": vehicle.code,
                "name": vehicle.name,
                "seating_capacity": vehicle.seating_capacity,
                "description": vehicle.description,
            }
            for vehicle in vehicle_categories
        ],
        "durations": [
            {
                "id": duration.id,
                "ride_type_id": duration.ride_type_id,
                "name": duration.name,
                "duration_minutes": duration.duration_minutes,
            }
            for duration in durations
        ],
        "coordinated_transfer_rules": [
            serialize_rule(rule)
            for rule in rules
            if ride_type_by_id.get(rule.ride_type_id) and ride_type_by_id[rule.ride_type_id].code == "coordinated_transfer"
        ],
        "package_trip_rules": [
            serialize_rule(rule)
            for rule in rules
            if ride_type_by_id.get(rule.ride_type_id) and ride_type_by_id[rule.ride_type_id].code == "package_trip"
        ],
    }

@router.get("/dashboard", response_model=AggregatorDashboardData)
def get_aggregator_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only aggregators can access the dashboard")

    agg_profile = current_user.aggregator_profile
    if not agg_profile:
        raise HTTPException(status_code=404, detail="Aggregator profile not found")

    status_labels = {
        "pending_provider_response": "Pending Provider Response",
        "provider_accepted": "Provider Accepted",
        "provider_rejected": "Provider Rejected",
        "driver_assigned": "Driver Assigned",
        "driver_accepted": "Driver Accepted",
        "on_trip": "On Trip",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "pending": "Pending",
        "confirmed": "Confirmed",
        "arrived": "Arrived",
        "in_progress": "In Progress",
    }
    ride_type_labels = {
        "flexible_ride": "Flexible Ride",
        "guaranteed_coordinated_ride": "Guaranteed Coordinated Ride",
    }

    rows = (
        db.query(
            CabBooking.id,
            CabBooking.booking_id,
            CabBooking.pickup_address,
            CabBooking.drop_address,
            CabBooking.vehicle_name,
            cast(CabBooking.vehicle_type, String).label("vehicle_type"),
            cast(CabBooking.ride_type, String).label("ride_type"),
            CabBooking.estimated_price,
            cast(CabBooking.status, String).label("status"),
            CabBooking.created_at,
            CabBooking.updated_at,
            CabBooking.completed_at,
            CabBooking.scheduled_time,
            CabBooking.driver_name,
            CabBooking.num_passengers,
            CabBooking.provider_id,
            CabBooking.aggregator_id,
            CabBooking.port,
            DriverMagicLink.token.label("magic_token"),
            CrewProfile.full_name.label("crew_name"),
            CrewProfile.hpid.label("crew_hpid"),
            CrewProfile.vessel.label("crew_vessel"),
        )
        .outerjoin(DriverMagicLink, DriverMagicLink.booking_id == CabBooking.id)
        .outerjoin(CrewProfile, CabBooking.crew_id == CrewProfile.id)
        .filter(
            or_(
                CabBooking.provider_id == agg_profile.id,
                CabBooking.aggregator_id == agg_profile.id,
                and_(
                    CabBooking.provider_id.is_(None),
                    CabBooking.aggregator_id.is_(None),
                    func.lower(cast(CabBooking.status, String)) == "pending_provider_response",
                ),
            )
        )
        .order_by(CabBooking.created_at.desc())
        .all()
    )

    visible_booking_ids = [row.id for row in rows]
    declined_by_me: set[int] = set()
    if visible_booking_ids:
        declined_by_me = {
            booking_id
            for (booking_id,) in (
                db.query(BookingTimeline.booking_id)
                .filter(
                    BookingTimeline.booking_id.in_(visible_booking_ids),
                    BookingTimeline.actor_type == "provider",
                    BookingTimeline.actor_id == agg_profile.id,
                    func.upper(BookingTimeline.event_type) == TimelineEventType.PROVIDER_REJECTED.value,
                )
                .distinct()
                .all()
            )
        }

    def normalized_status(row: Any) -> str:
        return (row.status or "").lower()

    def transform(row: Any) -> BookingDashboardOut:
        ride_type_value = (row.ride_type or "").lower() if row.ride_type else None
        status_value = normalized_status(row)
        return BookingDashboardOut(
            id=row.id,
            booking_id=row.booking_id,
            crew=CrewShortOut(
                name=row.crew_name or "Unknown",
                hp_id=row.crew_hpid or "",
                vessel=row.crew_vessel or "",
            ),
            pickup_address=row.pickup_address,
            drop_address=row.drop_address,
            vehicle_name=row.vehicle_name,
            vehicle_type=(row.vehicle_type or "").lower() if row.vehicle_type else None,
            ride_type=ride_type_value,
            ride_type_label=ride_type_labels.get(ride_type_value),
            estimated_price=float(row.estimated_price),
            status=status_value,
            status_label=status_labels.get(status_value, row.status),
            created_at=row.created_at,
            scheduled_time=row.scheduled_time,
            driver_name=row.driver_name,
            num_passengers=row.num_passengers,
            magic_link_path=f"/magic-link/{row.magic_token}" if row.magic_token else None,
        )

    def row_visible_to_provider(row: Any) -> bool:
        # Direct assignment visibility
        if row.provider_id == agg_profile.id or row.aggregator_id == agg_profile.id:
            return True

        # Broadcast visibility only for unassigned pending requests
        if normalized_status(row) != "pending_provider_response":
            return False
        if row.provider_id is not None or row.aggregator_id is not None:
            return False
        if row.id in declined_by_me:
            return False
        return True

    visible_rows = [row for row in rows if row_visible_to_provider(row)]

    def pick(status_values: set[str], limit: Optional[int] = None, sort_by_updated: bool = False) -> List[Any]:
        filtered = [r for r in visible_rows if normalized_status(r) in status_values]
        if sort_by_updated:
            filtered = sorted(filtered, key=lambda r: r.updated_at or r.created_at, reverse=True)
        if limit is not None:
            return filtered[:limit]
        return filtered

    new_requests_rows = pick({"pending_provider_response"})
    accepted_rows = pick({"provider_accepted"})
    active_rows = pick({"driver_assigned", "driver_accepted", "on_trip"})
    completed_rows = pick({"completed"}, limit=50, sort_by_updated=True)
    cancelled_rows = pick({"cancelled", "provider_rejected"}, limit=50, sort_by_updated=True)

    today = datetime.utcnow().date()
    stats = {
        "new_requests": len(new_requests_rows),
        "accepted": len(accepted_rows),
        "active_trips": len(pick({"on_trip"})),
        "completed": len(pick({"completed"})),
        "cancelled": len(cancelled_rows),
        "assigned": len(pick({"driver_assigned", "driver_accepted"})),
        "pending_requests": len(new_requests_rows),
        "today_completed_trips": len(
            [r for r in completed_rows if r.completed_at and r.completed_at.date() == today]
        ),
    }

    return AggregatorDashboardData(
        stats=stats,
        new_requests=[transform(r) for r in new_requests_rows],
        accepted_requests=[transform(r) for r in accepted_rows],
        active_trips=[transform(r) for r in active_rows],
        completed_requests=[transform(r) for r in completed_rows],
        cancelled_requests=[transform(r) for r in cancelled_rows],
        pending_requests=[transform(r) for r in new_requests_rows],
    )

@router.post("/dashboard/assign-driver")
def assign_driver(
    body: DriverAssignIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from app.services.booking_service import assign_driver_to_booking, get_booking_by_identifier, serialize_booking
    from app.services.magic_link_service import create_or_refresh_magic_link

    booking = get_booking_by_identifier(db, body.booking_id)
    updated = assign_driver_to_booking(db, booking, current_user, body.driver_id)
    magic_link = create_or_refresh_magic_link(
        db,
        booking=updated,
        aggregator_id=current_user.aggregator_profile.id if current_user.aggregator_profile else None,
        itinerary_stops=body.itinerary_stops,
    )
    return {
        "message": "Driver assigned successfully",
        "booking": serialize_booking(updated),
        "magic_link": {
            "token": magic_link.token,
            "path": f"/magic-link/{magic_link.token}",
        },
    }


@router.post("/dashboard/accept-ride")
def accept_ride(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    from app.services.booking_service import accept_booking, get_booking_by_identifier, serialize_booking

    booking = get_booking_by_identifier(db, booking_id)
    updated = accept_booking(db, booking, current_user)
    return {"message": "Ride accepted successfully", "booking": serialize_booking(updated)}


@router.post("/dashboard/decline-ride")
def decline_ride(
    booking_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.role != "aggregator":
        raise HTTPException(status_code=403, detail="Only fleet providers can perform this action")

    profile = current_user.aggregator_profile
    if not profile:
        raise HTTPException(status_code=404, detail="Fleet provider profile not found")

    booking_query = db.query(
        CabBooking.id,
        CabBooking.provider_id,
        CabBooking.aggregator_id,
        cast(CabBooking.status, String).label("status"),
    )
    if booking_id.isdigit():
        booking = booking_query.filter(CabBooking.id == int(booking_id)).first()
    else:
        booking = booking_query.filter(CabBooking.booking_id == booking_id).first()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")

    assigned_provider_id = booking.provider_id or booking.aggregator_id
    if assigned_provider_id is not None and assigned_provider_id != profile.id:
        raise HTTPException(status_code=403, detail="Booking not assigned to your provider account")

    if (booking.status or "").lower() != BookingStatus.PENDING_PROVIDER_RESPONSE.value:
        raise HTTPException(status_code=400, detail="Booking is not awaiting provider response")

    already_declined = (
        db.query(BookingTimeline.id)
        .filter(
            BookingTimeline.booking_id == booking.id,
            BookingTimeline.actor_type == "provider",
            BookingTimeline.actor_id == profile.id,
            func.upper(BookingTimeline.event_type) == TimelineEventType.PROVIDER_REJECTED.value,
        )
        .first()
    )
    if already_declined:
        return {"message": "Ride already declined by this provider", "booking_id": booking_id}

    create_timeline_event(
        db,
        booking_db_id=booking.id,
        event_type=TimelineEventType.PROVIDER_REJECTED,
        actor_id=profile.id,
        actor_type="provider",
        metadata={"provider_name": profile.company_name},
    )
    db.commit()
    return {
        "message": "Ride declined for this provider; still available to others",
        "booking_id": booking_id,
    }
