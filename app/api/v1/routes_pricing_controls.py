from datetime import date, datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.db.models.port import Port
from app.db.models.pricing_controls import (
    PricingAdjustmentType,
    PricingAuditLog,
    PricingDuration,
    PricingDurationVisibility,
    PricingProviderSetting,
    PricingRideType,
    PricingRule,
    PricingVehicleCategory,
    PricingVehicleVisibility,
)
from app.db.models.user import User
from app.db.session import get_db

router = APIRouter()

PROVIDER_TYPES = ["partner_drivers", "aggregators"]
SUPPORTED_RIDE_TYPE_CODES = ["coordinated_transfer", "package_trip"]

DEFAULT_RIDE_TYPES = [
    {
        "code": "coordinated_transfer",
        "name": "Coordinated Transfers",
        "description": "Distance and time based transfer pricing.",
        "pricing_mode": "distance",
        "supports_duration": False,
        "supports_adjustments": True,
        "is_active": True,
        "sort_order": 1,
    },
    {
        "code": "package_trip",
        "name": "Package Trips",
        "description": "Duration based package pricing.",
        "pricing_mode": "package",
        "supports_duration": True,
        "supports_adjustments": True,
        "is_active": True,
        "sort_order": 2,
    },
]

DEFAULT_ADJUSTMENTS = {
    "coordinated_transfer": [
        {"code": "night_multiplier", "name": "Night Multiplier", "default_value": 1.0},
        {"code": "rain_multiplier", "name": "Rain Multiplier", "default_value": 1.0},
        {"code": "traffic_multiplier", "name": "Traffic Multiplier", "default_value": 1.0},
    ],
    "package_trip": [
        {"code": "festival_multiplier", "name": "Festival Multiplier", "default_value": 1.0},
        {"code": "holiday_multiplier", "name": "Holiday Multiplier", "default_value": 1.0},
    ],
}

DEFAULT_PACKAGE_DURATIONS = [
    ("2 Hours", 120),
    ("3 Hours", 180),
    ("4 Hours", 240),
    ("5 Hours", 300),
    ("6 Hours", 360),
]


class VehicleCategoryIn(BaseModel):
    port_id: int
    code: Optional[str] = None
    name: str
    icon_url: Optional[str] = None
    seating_capacity: int = Field(..., ge=1)
    description: Optional[str] = None
    is_active: bool = True


class DurationIn(BaseModel):
    port_id: int
    ride_type_code: str
    name: str
    duration_minutes: int = Field(..., gt=0)
    description: Optional[str] = None
    is_active: bool = True
    sort_order: int = 0


class ProviderSettingItem(BaseModel):
    provider_type: str
    minimum_bookable_hours: Optional[float] = Field(default=None, ge=0)
    is_active: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class ProviderSettingBulkIn(BaseModel):
    port_id: int
    ride_type_code: str
    settings: List[ProviderSettingItem]


class AdjustmentTypeIn(BaseModel):
    port_id: int
    ride_type_code: str
    code: Optional[str] = None
    name: str
    adjustment_kind: str = "multiplier"
    default_value: float = 1.0
    description: Optional[str] = None
    is_active: bool = True


class AdjustmentValueIn(BaseModel):
    adjustment_type_id: Optional[int] = None
    code: str
    value: float
    is_active: bool = True


class PricingRuleIn(BaseModel):
    port_id: int
    ride_type_code: str
    provider_type: str
    vehicle_category_id: int
    duration_id: Optional[int] = None
    base_fare: float = 0
    minimum_fare: Optional[float] = None
    price_per_km: Optional[float] = None
    price_per_minute: Optional[float] = None
    free_waiting_minutes: Optional[float] = None
    extra_waiting_charge: Optional[float] = None
    cancellation_fee: Optional[float] = None
    included_km: Optional[float] = None
    price_per_extra_km: Optional[float] = None
    price_per_extra_minute: Optional[float] = None
    price_per_extra_stop: Optional[float] = None
    platform_commission_pct: float = 0
    adjustments: List[AdjustmentValueIn] = Field(default_factory=list)
    pricing_metadata: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool = True


class DurationVisibilityItem(BaseModel):
    provider_type: str
    duration_id: int
    is_visible: bool


class DurationVisibilityBulkIn(BaseModel):
    port_id: int
    ride_type_code: str
    items: List[DurationVisibilityItem]


class VehicleVisibilityItem(BaseModel):
    provider_type: str
    duration_id: int
    vehicle_category_id: int
    is_visible: bool


class VehicleVisibilityBulkIn(BaseModel):
    port_id: int
    ride_type_code: str
    items: List[VehicleVisibilityItem]


class PricingPreviewIn(BaseModel):
    port_id: int
    ride_type_code: str
    provider_type: str
    vehicle_category_id: int
    duration_id: Optional[int] = None
    distance_km: float = 0
    trip_minutes: float = 0
    waiting_minutes: float = 0
    extra_minutes: float = 0
    extra_stop_count: int = 0
    adjustment_values: Dict[str, float] = Field(default_factory=dict)


class BulkCopyIn(BaseModel):
    source_port_id: int
    target_port_id: int
    modules: List[str]


def verify_superadmin(current_user: User) -> None:
    if current_user.role != "superadmin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superadmins can access this resource",
        )


def normalize_code(value: str) -> str:
    return "_".join(value.strip().lower().replace("/", " ").replace("-", " ").split())


def serialize_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def serialize_model(instance: Any) -> Dict[str, Any]:
    return {
        column.name: serialize_value(getattr(instance, column.name))
        for column in instance.__table__.columns
    }


def get_port_or_404(db: Session, port_id: int) -> Port:
    port = db.get(Port, port_id)
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")
    return port


def get_ride_type_or_404(db: Session, ride_type_code: str) -> PricingRideType:
    ride_type = (
        db.query(PricingRideType)
        .filter(PricingRideType.code == ride_type_code)
        .first()
    )
    if not ride_type:
        raise HTTPException(status_code=404, detail="Ride type not found")
    return ride_type


def log_audit(
    db: Session,
    *,
    entity_type: str,
    entity_id: int,
    action: str,
    current_user: User,
    previous_values: Optional[Dict[str, Any]] = None,
    current_values: Optional[Dict[str, Any]] = None,
    port_id: Optional[int] = None,
) -> None:
    audit = PricingAuditLog(
        port_id=port_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        created_by=current_user.email,
        previous_values=previous_values or {},
        current_values=current_values or {},
    )
    db.add(audit)


def ensure_default_catalog(db: Session, port_id: int) -> None:
    get_port_or_404(db, port_id)

    created = False
    for item in DEFAULT_RIDE_TYPES:
        existing = db.query(PricingRideType).filter(PricingRideType.code == item["code"]).first()
        if not existing:
            db.add(PricingRideType(**item))
            created = True
    if created:
        db.flush()

    package_ride = get_ride_type_or_404(db, "package_trip")
    coordinated_ride = get_ride_type_or_404(db, "coordinated_transfer")

    for provider_type, min_hours in (("partner_drivers", 1), ("aggregators", 4)):
        existing = (
            db.query(PricingProviderSetting)
            .filter(
                PricingProviderSetting.port_id == port_id,
                PricingProviderSetting.ride_type_id == package_ride.id,
                PricingProviderSetting.provider_type == provider_type,
            )
            .first()
        )
        if not existing:
            db.add(
                PricingProviderSetting(
                    port_id=port_id,
                    ride_type_id=package_ride.id,
                    provider_type=provider_type,
                    minimum_bookable_hours=min_hours,
                    is_active=True,
                    config={},
                )
            )
            created = True

    for provider_type in PROVIDER_TYPES:
        existing = (
            db.query(PricingProviderSetting)
            .filter(
                PricingProviderSetting.port_id == port_id,
                PricingProviderSetting.ride_type_id == coordinated_ride.id,
                PricingProviderSetting.provider_type == provider_type,
            )
            .first()
        )
        if not existing:
            db.add(
                PricingProviderSetting(
                    port_id=port_id,
                    ride_type_id=coordinated_ride.id,
                    provider_type=provider_type,
                    minimum_bookable_hours=None,
                    is_active=True,
                    config={},
                )
            )
            created = True

    existing_durations = (
        db.query(PricingDuration)
        .filter(
            PricingDuration.port_id == port_id,
            PricingDuration.ride_type_id == package_ride.id,
        )
        .count()
    )
    if existing_durations == 0:
        for order, (name, minutes) in enumerate(DEFAULT_PACKAGE_DURATIONS, start=1):
            db.add(
                PricingDuration(
                    port_id=port_id,
                    ride_type_id=package_ride.id,
                    name=name,
                    duration_minutes=minutes,
                    is_active=True,
                    sort_order=order,
                )
            )
        created = True
        db.flush()

    for ride_code, adjustments in DEFAULT_ADJUSTMENTS.items():
        ride_type = get_ride_type_or_404(db, ride_code)
        for adjustment in adjustments:
            exists = (
                db.query(PricingAdjustmentType)
                .filter(
                    PricingAdjustmentType.port_id == port_id,
                    PricingAdjustmentType.ride_type_id == ride_type.id,
                    PricingAdjustmentType.code == adjustment["code"],
                )
                .first()
            )
            if not exists:
                db.add(
                    PricingAdjustmentType(
                        port_id=port_id,
                        ride_type_id=ride_type.id,
                        code=adjustment["code"],
                        name=adjustment["name"],
                        default_value=adjustment["default_value"],
                        adjustment_kind="multiplier",
                        is_active=True,
                    )
                )
                created = True

    if created:
        db.commit()


def get_rule_or_404(db: Session, rule_id: int) -> PricingRule:
    rule = db.get(PricingRule, rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Pricing rule not found")
    return rule


def build_rule_snapshot(db: Session, rule: PricingRule) -> Dict[str, Any]:
    vehicle = db.get(PricingVehicleCategory, rule.vehicle_category_id)
    duration = db.get(PricingDuration, rule.duration_id) if rule.duration_id else None
    ride_type = db.get(PricingRideType, rule.ride_type_id)
    return {
        **serialize_model(rule),
        "vehicle_category_name": vehicle.name if vehicle else None,
        "vehicle_category_code": vehicle.code if vehicle else None,
        "duration_name": duration.name if duration else None,
        "ride_type_code": ride_type.code if ride_type else None,
        "ride_type_name": ride_type.name if ride_type else None,
    }


def compute_preview(rule: PricingRule, ride_type: PricingRideType, payload: PricingPreviewIn) -> Dict[str, Any]:
    adjustment_multiplier = 1.0
    applied_adjustments: List[Dict[str, Any]] = []
    for adjustment in rule.adjustments or []:
        code = adjustment.get("code")
        if not adjustment.get("is_active", True):
            continue
        value = payload.adjustment_values.get(code, adjustment.get("value", 1.0))
        applied_adjustments.append({"code": code, "value": value})
        if code and "multiplier" in code:
            adjustment_multiplier *= value

    if ride_type.pricing_mode == "package":
        included_km = rule.included_km or 0
        extra_km = max(payload.distance_km - included_km, 0)
        extra_km_charge = extra_km * (rule.price_per_extra_km or 0)
        extra_minute_charge = payload.extra_minutes * (rule.price_per_extra_minute or 0)
        extra_stop_charge = payload.extra_stop_count * (rule.price_per_extra_stop or 0)
        subtotal = rule.base_fare + extra_km_charge + extra_minute_charge + extra_stop_charge
        subtotal *= adjustment_multiplier
        commission_amount = subtotal * ((rule.platform_commission_pct or 0) / 100)
        return {
            "base_fare": round(rule.base_fare, 2),
            "included_km": included_km,
            "extra_km_charge": round(extra_km_charge, 2),
            "extra_minute_charge": round(extra_minute_charge, 2),
            "extra_stop_charge": round(extra_stop_charge, 2),
            "commission_amount": round(commission_amount, 2),
            "final_customer_price": round(subtotal + commission_amount, 2),
            "adjustment_multiplier": round(adjustment_multiplier, 4),
            "applied_adjustments": applied_adjustments,
        }

    waiting_charge = max(payload.waiting_minutes - (rule.free_waiting_minutes or 0), 0) * (rule.extra_waiting_charge or 0)
    subtotal = rule.base_fare + (payload.distance_km * (rule.price_per_km or 0)) + (payload.trip_minutes * (rule.price_per_minute or 0)) + waiting_charge
    subtotal = max(subtotal, rule.minimum_fare or 0)
    subtotal *= adjustment_multiplier
    commission_amount = subtotal * ((rule.platform_commission_pct or 0) / 100)
    return {
        "base_fare": round(rule.base_fare, 2),
        "minimum_fare": round(rule.minimum_fare or 0, 2),
        "distance_charge": round(payload.distance_km * (rule.price_per_km or 0), 2),
        "time_charge": round(payload.trip_minutes * (rule.price_per_minute or 0), 2),
        "waiting_charge": round(waiting_charge, 2),
        "commission_amount": round(commission_amount, 2),
        "final_customer_price": round(subtotal + commission_amount, 2),
        "adjustment_multiplier": round(adjustment_multiplier, 4),
        "applied_adjustments": applied_adjustments,
    }


def upsert_duration_visibility(db: Session, port_id: int, ride_type_id: int, items: List[DurationVisibilityItem]) -> None:
    for item in items:
        record = (
            db.query(PricingDurationVisibility)
            .filter(
                PricingDurationVisibility.port_id == port_id,
                PricingDurationVisibility.ride_type_id == ride_type_id,
                PricingDurationVisibility.provider_type == item.provider_type,
                PricingDurationVisibility.duration_id == item.duration_id,
            )
            .first()
        )
        if record:
            record.is_visible = item.is_visible
        else:
            db.add(
                PricingDurationVisibility(
                    port_id=port_id,
                    ride_type_id=ride_type_id,
                    provider_type=item.provider_type,
                    duration_id=item.duration_id,
                    is_visible=item.is_visible,
                )
            )


def upsert_vehicle_visibility(db: Session, port_id: int, ride_type_id: int, items: List[VehicleVisibilityItem]) -> None:
    for item in items:
        record = (
            db.query(PricingVehicleVisibility)
            .filter(
                PricingVehicleVisibility.port_id == port_id,
                PricingVehicleVisibility.ride_type_id == ride_type_id,
                PricingVehicleVisibility.provider_type == item.provider_type,
                PricingVehicleVisibility.duration_id == item.duration_id,
                PricingVehicleVisibility.vehicle_category_id == item.vehicle_category_id,
            )
            .first()
        )
        if record:
            record.is_visible = item.is_visible
        else:
            db.add(
                PricingVehicleVisibility(
                    port_id=port_id,
                    ride_type_id=ride_type_id,
                    provider_type=item.provider_type,
                    duration_id=item.duration_id,
                    vehicle_category_id=item.vehicle_category_id,
                    is_visible=item.is_visible,
                )
            )


@router.get("/pricing-controls/bootstrap")
def get_pricing_bootstrap(
    port_id: int = Query(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ensure_default_catalog(db, port_id)

    ride_types = (
        db.query(PricingRideType)
        .filter(PricingRideType.code.in_(SUPPORTED_RIDE_TYPE_CODES))
        .order_by(PricingRideType.sort_order.asc())
        .all()
    )
    vehicle_categories = (
        db.query(PricingVehicleCategory)
        .filter(PricingVehicleCategory.port_id == port_id)
        .order_by(PricingVehicleCategory.name.asc())
        .all()
    )
    durations = (
        db.query(PricingDuration)
        .filter(PricingDuration.port_id == port_id)
        .order_by(PricingDuration.sort_order.asc(), PricingDuration.duration_minutes.asc())
        .all()
    )
    provider_settings = (
        db.query(PricingProviderSetting)
        .filter(PricingProviderSetting.port_id == port_id)
        .all()
    )
    adjustment_types = (
        db.query(PricingAdjustmentType)
        .filter(PricingAdjustmentType.port_id == port_id)
        .order_by(PricingAdjustmentType.name.asc())
        .all()
    )
    rules = (
        db.query(PricingRule)
        .filter(PricingRule.port_id == port_id)
        .order_by(PricingRule.updated_at.desc())
        .all()
    )
    duration_visibility = (
        db.query(PricingDurationVisibility)
        .filter(PricingDurationVisibility.port_id == port_id)
        .all()
    )
    vehicle_visibility = (
        db.query(PricingVehicleVisibility)
        .filter(PricingVehicleVisibility.port_id == port_id)
        .all()
    )

    return {
        "provider_types": PROVIDER_TYPES,
        "ride_types": [serialize_model(item) for item in ride_types],
        "vehicle_categories": [serialize_model(item) for item in vehicle_categories],
        "durations": [serialize_model(item) for item in durations],
        "provider_settings": [serialize_model(item) for item in provider_settings],
        "adjustment_types": [serialize_model(item) for item in adjustment_types],
        "rules": [build_rule_snapshot(db, item) for item in rules],
        "duration_visibility": [serialize_model(item) for item in duration_visibility],
        "vehicle_visibility": [serialize_model(item) for item in vehicle_visibility],
    }


@router.get("/pricing-controls/vehicle-categories")
def list_vehicle_categories(
    port_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    return (
        db.query(PricingVehicleCategory)
        .filter(PricingVehicleCategory.port_id == port_id)
        .order_by(PricingVehicleCategory.name.asc())
        .all()
    )


@router.post("/pricing-controls/vehicle-categories")
def create_vehicle_category(
    payload: VehicleCategoryIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    get_port_or_404(db, payload.port_id)
    code = payload.code or normalize_code(payload.name)
    category = PricingVehicleCategory(
        port_id=payload.port_id,
        code=code,
        name=payload.name,
        icon_url=payload.icon_url,
        seating_capacity=payload.seating_capacity,
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(category)
    db.flush()
    log_audit(
        db,
        entity_type="vehicle_category",
        entity_id=category.id,
        action="create",
        current_user=current_user,
        current_values=serialize_model(category),
        port_id=payload.port_id,
    )
    db.commit()
    db.refresh(category)
    return category


@router.put("/pricing-controls/vehicle-categories/{category_id}")
def update_vehicle_category(
    category_id: int,
    payload: VehicleCategoryIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    category = db.get(PricingVehicleCategory, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Vehicle category not found")
    previous = serialize_model(category)
    category.port_id = payload.port_id
    category.code = payload.code or normalize_code(payload.name)
    category.name = payload.name
    category.icon_url = payload.icon_url
    category.seating_capacity = payload.seating_capacity
    category.description = payload.description
    category.is_active = payload.is_active
    log_audit(
        db,
        entity_type="vehicle_category",
        entity_id=category.id,
        action="update",
        current_user=current_user,
        previous_values=previous,
        current_values=serialize_model(category),
        port_id=category.port_id,
    )
    db.commit()
    db.refresh(category)
    return category


@router.delete("/pricing-controls/vehicle-categories/{category_id}")
def delete_vehicle_category(
    category_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    category = db.get(PricingVehicleCategory, category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Vehicle category not found")
    snapshot = serialize_model(category)
    db.query(PricingVehicleVisibility).filter(PricingVehicleVisibility.vehicle_category_id == category_id).delete()
    db.query(PricingRule).filter(PricingRule.vehicle_category_id == category_id).delete()
    db.delete(category)
    log_audit(
        db,
        entity_type="vehicle_category",
        entity_id=category_id,
        action="delete",
        current_user=current_user,
        previous_values=snapshot,
        port_id=snapshot.get("port_id"),
    )
    db.commit()
    return {"ok": True}


@router.get("/pricing-controls/durations")
def list_durations(
    port_id: int,
    ride_type_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    query = db.query(PricingDuration).filter(PricingDuration.port_id == port_id)
    if ride_type_code:
        ride_type = get_ride_type_or_404(db, ride_type_code)
        query = query.filter(PricingDuration.ride_type_id == ride_type.id)
    return query.order_by(PricingDuration.sort_order.asc(), PricingDuration.duration_minutes.asc()).all()


@router.post("/pricing-controls/durations")
def create_duration(
    payload: DurationIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    duration = PricingDuration(
        port_id=payload.port_id,
        ride_type_id=ride_type.id,
        name=payload.name,
        duration_minutes=payload.duration_minutes,
        description=payload.description,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    db.add(duration)
    db.flush()
    log_audit(
        db,
        entity_type="duration",
        entity_id=duration.id,
        action="create",
        current_user=current_user,
        current_values=serialize_model(duration),
        port_id=payload.port_id,
    )
    db.commit()
    db.refresh(duration)
    return duration


@router.put("/pricing-controls/durations/{duration_id}")
def update_duration(
    duration_id: int,
    payload: DurationIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    duration = db.get(PricingDuration, duration_id)
    if not duration:
        raise HTTPException(status_code=404, detail="Duration not found")
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    previous = serialize_model(duration)
    duration.port_id = payload.port_id
    duration.ride_type_id = ride_type.id
    duration.name = payload.name
    duration.duration_minutes = payload.duration_minutes
    duration.description = payload.description
    duration.is_active = payload.is_active
    duration.sort_order = payload.sort_order
    log_audit(
        db,
        entity_type="duration",
        entity_id=duration.id,
        action="update",
        current_user=current_user,
        previous_values=previous,
        current_values=serialize_model(duration),
        port_id=duration.port_id,
    )
    db.commit()
    db.refresh(duration)
    return duration


@router.delete("/pricing-controls/durations/{duration_id}")
def delete_duration(
    duration_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    duration = db.get(PricingDuration, duration_id)
    if not duration:
        raise HTTPException(status_code=404, detail="Duration not found")
    snapshot = serialize_model(duration)
    db.query(PricingVehicleVisibility).filter(PricingVehicleVisibility.duration_id == duration_id).delete()
    db.query(PricingDurationVisibility).filter(PricingDurationVisibility.duration_id == duration_id).delete()
    db.query(PricingRule).filter(PricingRule.duration_id == duration_id).delete()
    db.delete(duration)
    log_audit(
        db,
        entity_type="duration",
        entity_id=duration_id,
        action="delete",
        current_user=current_user,
        previous_values=snapshot,
        port_id=snapshot.get("port_id"),
    )
    db.commit()
    return {"ok": True}


@router.get("/pricing-controls/provider-settings")
def list_provider_settings(
    port_id: int,
    ride_type_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, ride_type_code)
    return (
        db.query(PricingProviderSetting)
        .filter(
            PricingProviderSetting.port_id == port_id,
            PricingProviderSetting.ride_type_id == ride_type.id,
        )
        .all()
    )


@router.put("/pricing-controls/provider-settings")
def upsert_provider_settings(
    payload: ProviderSettingBulkIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    updated_records = []
    for item in payload.settings:
        if item.provider_type not in PROVIDER_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported provider type: {item.provider_type}")
        record = (
            db.query(PricingProviderSetting)
            .filter(
                PricingProviderSetting.port_id == payload.port_id,
                PricingProviderSetting.ride_type_id == ride_type.id,
                PricingProviderSetting.provider_type == item.provider_type,
            )
            .first()
        )
        previous = serialize_model(record) if record else {}
        if not record:
            record = PricingProviderSetting(
                port_id=payload.port_id,
                ride_type_id=ride_type.id,
                provider_type=item.provider_type,
            )
            db.add(record)
            db.flush()
        record.minimum_bookable_hours = item.minimum_bookable_hours
        record.is_active = item.is_active
        record.config = item.config
        updated_records.append(record)
        log_audit(
            db,
            entity_type="provider_setting",
            entity_id=record.id,
            action="update",
            current_user=current_user,
            previous_values=previous,
            current_values=serialize_model(record),
            port_id=payload.port_id,
        )
    db.commit()
    return [serialize_model(item) for item in updated_records]


@router.get("/pricing-controls/adjustment-types")
def list_adjustment_types(
    port_id: int,
    ride_type_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, ride_type_code)
    return (
        db.query(PricingAdjustmentType)
        .filter(
            PricingAdjustmentType.port_id == port_id,
            PricingAdjustmentType.ride_type_id == ride_type.id,
        )
        .order_by(PricingAdjustmentType.name.asc())
        .all()
    )


@router.post("/pricing-controls/adjustment-types")
def create_adjustment_type(
    payload: AdjustmentTypeIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    adjustment = PricingAdjustmentType(
        port_id=payload.port_id,
        ride_type_id=ride_type.id,
        code=payload.code or normalize_code(payload.name),
        name=payload.name,
        adjustment_kind=payload.adjustment_kind,
        default_value=payload.default_value,
        description=payload.description,
        is_active=payload.is_active,
    )
    db.add(adjustment)
    db.flush()
    log_audit(
        db,
        entity_type="adjustment_type",
        entity_id=adjustment.id,
        action="create",
        current_user=current_user,
        current_values=serialize_model(adjustment),
        port_id=payload.port_id,
    )
    db.commit()
    db.refresh(adjustment)
    return adjustment


@router.put("/pricing-controls/adjustment-types/{adjustment_id}")
def update_adjustment_type(
    adjustment_id: int,
    payload: AdjustmentTypeIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    adjustment = db.get(PricingAdjustmentType, adjustment_id)
    if not adjustment:
        raise HTTPException(status_code=404, detail="Adjustment type not found")
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    previous = serialize_model(adjustment)
    adjustment.port_id = payload.port_id
    adjustment.ride_type_id = ride_type.id
    adjustment.code = payload.code or normalize_code(payload.name)
    adjustment.name = payload.name
    adjustment.adjustment_kind = payload.adjustment_kind
    adjustment.default_value = payload.default_value
    adjustment.description = payload.description
    adjustment.is_active = payload.is_active
    log_audit(
        db,
        entity_type="adjustment_type",
        entity_id=adjustment.id,
        action="update",
        current_user=current_user,
        previous_values=previous,
        current_values=serialize_model(adjustment),
        port_id=payload.port_id,
    )
    db.commit()
    db.refresh(adjustment)
    return adjustment


@router.delete("/pricing-controls/adjustment-types/{adjustment_id}")
def delete_adjustment_type(
    adjustment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    adjustment = db.get(PricingAdjustmentType, adjustment_id)
    if not adjustment:
        raise HTTPException(status_code=404, detail="Adjustment type not found")
    snapshot = serialize_model(adjustment)
    db.delete(adjustment)
    log_audit(
        db,
        entity_type="adjustment_type",
        entity_id=adjustment_id,
        action="delete",
        current_user=current_user,
        previous_values=snapshot,
        port_id=snapshot.get("port_id"),
    )
    db.commit()
    return {"ok": True}


@router.get("/pricing-controls/rules")
def list_pricing_rules(
    port_id: int,
    ride_type_code: Optional[str] = None,
    provider_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    query = db.query(PricingRule).filter(PricingRule.port_id == port_id)
    if ride_type_code:
        ride_type = get_ride_type_or_404(db, ride_type_code)
        query = query.filter(PricingRule.ride_type_id == ride_type.id)
    if provider_type:
        query = query.filter(PricingRule.provider_type == provider_type)
    return [build_rule_snapshot(db, item) for item in query.order_by(PricingRule.updated_at.desc()).all()]


@router.post("/pricing-controls/rules")
def create_pricing_rule(
    payload: PricingRuleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    rule = PricingRule(
        port_id=payload.port_id,
        ride_type_id=ride_type.id,
        provider_type=payload.provider_type,
        vehicle_category_id=payload.vehicle_category_id,
        duration_id=payload.duration_id,
        base_fare=payload.base_fare,
        minimum_fare=payload.minimum_fare,
        price_per_km=payload.price_per_km,
        price_per_minute=payload.price_per_minute,
        free_waiting_minutes=payload.free_waiting_minutes,
        extra_waiting_charge=payload.extra_waiting_charge,
        cancellation_fee=payload.cancellation_fee,
        included_km=payload.included_km,
        price_per_extra_km=payload.price_per_extra_km,
        price_per_extra_minute=payload.price_per_extra_minute,
        price_per_extra_stop=payload.price_per_extra_stop,
        platform_commission_pct=payload.platform_commission_pct,
        adjustments=[item.model_dump() for item in payload.adjustments],
        pricing_metadata=payload.pricing_metadata,
        is_active=payload.is_active,
        is_archived=False,
        version=1,
        created_by=current_user.email,
        updated_by=current_user.email,
    )
    db.add(rule)
    db.flush()
    log_audit(
        db,
        entity_type="pricing_rule",
        entity_id=rule.id,
        action="create",
        current_user=current_user,
        current_values=serialize_model(rule),
        port_id=payload.port_id,
    )
    db.commit()
    db.refresh(rule)
    return build_rule_snapshot(db, rule)


@router.put("/pricing-controls/rules/{rule_id}")
def update_pricing_rule(
    rule_id: int,
    payload: PricingRuleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    rule = get_rule_or_404(db, rule_id)
    previous = serialize_model(rule)
    rule.port_id = payload.port_id
    rule.ride_type_id = ride_type.id
    rule.provider_type = payload.provider_type
    rule.vehicle_category_id = payload.vehicle_category_id
    rule.duration_id = payload.duration_id
    rule.base_fare = payload.base_fare
    rule.minimum_fare = payload.minimum_fare
    rule.price_per_km = payload.price_per_km
    rule.price_per_minute = payload.price_per_minute
    rule.free_waiting_minutes = payload.free_waiting_minutes
    rule.extra_waiting_charge = payload.extra_waiting_charge
    rule.cancellation_fee = payload.cancellation_fee
    rule.included_km = payload.included_km
    rule.price_per_extra_km = payload.price_per_extra_km
    rule.price_per_extra_minute = payload.price_per_extra_minute
    rule.price_per_extra_stop = payload.price_per_extra_stop
    rule.platform_commission_pct = payload.platform_commission_pct
    rule.adjustments = [item.model_dump() for item in payload.adjustments]
    rule.pricing_metadata = payload.pricing_metadata
    rule.is_active = payload.is_active
    rule.updated_by = current_user.email
    rule.version += 1
    log_audit(
        db,
        entity_type="pricing_rule",
        entity_id=rule.id,
        action="update",
        current_user=current_user,
        previous_values=previous,
        current_values=serialize_model(rule),
        port_id=payload.port_id,
    )
    db.commit()
    db.refresh(rule)
    return build_rule_snapshot(db, rule)


@router.post("/pricing-controls/rules/{rule_id}/duplicate")
def duplicate_pricing_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    rule = get_rule_or_404(db, rule_id)
    duplicate = PricingRule(
        port_id=rule.port_id,
        ride_type_id=rule.ride_type_id,
        provider_type=rule.provider_type,
        vehicle_category_id=rule.vehicle_category_id,
        duration_id=rule.duration_id,
        base_fare=rule.base_fare,
        minimum_fare=rule.minimum_fare,
        price_per_km=rule.price_per_km,
        price_per_minute=rule.price_per_minute,
        free_waiting_minutes=rule.free_waiting_minutes,
        extra_waiting_charge=rule.extra_waiting_charge,
        cancellation_fee=rule.cancellation_fee,
        included_km=rule.included_km,
        price_per_extra_km=rule.price_per_extra_km,
        price_per_extra_minute=rule.price_per_extra_minute,
        price_per_extra_stop=rule.price_per_extra_stop,
        platform_commission_pct=rule.platform_commission_pct,
        adjustments=rule.adjustments,
        pricing_metadata=rule.pricing_metadata,
        is_active=False,
        is_archived=False,
        version=1,
        copied_from_rule_id=rule.id,
        created_by=current_user.email,
        updated_by=current_user.email,
    )
    db.add(duplicate)
    db.flush()
    log_audit(
        db,
        entity_type="pricing_rule",
        entity_id=duplicate.id,
        action="duplicate",
        current_user=current_user,
        previous_values={"source_rule_id": rule.id},
        current_values=serialize_model(duplicate),
        port_id=duplicate.port_id,
    )
    db.commit()
    db.refresh(duplicate)
    return build_rule_snapshot(db, duplicate)


@router.post("/pricing-controls/rules/{rule_id}/archive")
def archive_pricing_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    rule = get_rule_or_404(db, rule_id)
    previous = serialize_model(rule)
    rule.is_archived = True
    rule.is_active = False
    rule.updated_by = current_user.email
    rule.version += 1
    log_audit(
        db,
        entity_type="pricing_rule",
        entity_id=rule.id,
        action="archive",
        current_user=current_user,
        previous_values=previous,
        current_values=serialize_model(rule),
        port_id=rule.port_id,
    )
    db.commit()
    return {"ok": True}


@router.put("/pricing-controls/visibility/durations")
def update_duration_visibility(
    payload: DurationVisibilityBulkIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    previous = [
        serialize_model(item)
        for item in db.query(PricingDurationVisibility).filter(
            PricingDurationVisibility.port_id == payload.port_id,
            PricingDurationVisibility.ride_type_id == ride_type.id,
        )
    ]
    upsert_duration_visibility(db, payload.port_id, ride_type.id, payload.items)
    log_audit(
        db,
        entity_type="duration_visibility",
        entity_id=payload.port_id,
        action="bulk_update",
        current_user=current_user,
        previous_values={"items": previous},
        current_values={"items": [item.model_dump() for item in payload.items]},
        port_id=payload.port_id,
    )
    db.commit()
    return {"ok": True}


@router.put("/pricing-controls/visibility/vehicles")
def update_vehicle_visibility(
    payload: VehicleVisibilityBulkIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    previous = [
        serialize_model(item)
        for item in db.query(PricingVehicleVisibility).filter(
            PricingVehicleVisibility.port_id == payload.port_id,
            PricingVehicleVisibility.ride_type_id == ride_type.id,
        )
    ]
    upsert_vehicle_visibility(db, payload.port_id, ride_type.id, payload.items)
    log_audit(
        db,
        entity_type="vehicle_visibility",
        entity_id=payload.port_id,
        action="bulk_update",
        current_user=current_user,
        previous_values={"items": previous},
        current_values={"items": [item.model_dump() for item in payload.items]},
        port_id=payload.port_id,
    )
    db.commit()
    return {"ok": True}


@router.post("/pricing-controls/preview")
def preview_pricing(
    payload: PricingPreviewIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    ride_type = get_ride_type_or_404(db, payload.ride_type_code)
    rule = (
        db.query(PricingRule)
        .filter(
            PricingRule.port_id == payload.port_id,
            PricingRule.ride_type_id == ride_type.id,
            PricingRule.provider_type == payload.provider_type,
            PricingRule.vehicle_category_id == payload.vehicle_category_id,
            PricingRule.duration_id == payload.duration_id,
            PricingRule.is_active.is_(True),
            PricingRule.is_archived.is_(False),
        )
        .order_by(PricingRule.updated_at.desc())
        .first()
    )
    if not rule:
        raise HTTPException(status_code=404, detail="No active pricing rule found")
    preview = compute_preview(rule, ride_type, payload)
    return {
        "rule": build_rule_snapshot(db, rule),
        "preview": preview,
    }


@router.get("/pricing-controls/history")
def get_pricing_history(
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    query = db.query(PricingAuditLog)
    if entity_type:
        query = query.filter(PricingAuditLog.entity_type == entity_type)
    if entity_id:
        query = query.filter(PricingAuditLog.entity_id == entity_id)
    if port_id:
        query = query.filter(PricingAuditLog.port_id == port_id)
    return query.order_by(PricingAuditLog.created_at.desc()).limit(200).all()


@router.post("/pricing-controls/history/{log_id}/restore")
def restore_from_history(
    log_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    log = db.get(PricingAuditLog, log_id)
    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")

    model_map = {
        "vehicle_category": PricingVehicleCategory,
        "duration": PricingDuration,
        "adjustment_type": PricingAdjustmentType,
        "pricing_rule": PricingRule,
        "provider_setting": PricingProviderSetting,
    }
    model = model_map.get(log.entity_type)
    if not model or not log.previous_values:
        raise HTTPException(status_code=400, detail="Restore not supported for this audit entry")

    entity = db.get(model, log.entity_id)
    if not entity:
        raise HTTPException(status_code=404, detail="Target entity not found")

    previous = serialize_model(entity)
    for key, value in log.previous_values.items():
        if key in {"id", "created_at", "updated_at"}:
            continue
        if hasattr(entity, key):
            setattr(entity, key, value)

    log_audit(
        db,
        entity_type=log.entity_type,
        entity_id=log.entity_id,
        action="restore",
        current_user=current_user,
        previous_values=previous,
        current_values=serialize_model(entity),
        port_id=log.port_id,
    )
    db.commit()
    return {"ok": True}


@router.post("/pricing-controls/bulk-copy")
def bulk_copy_pricing_configuration(
    payload: BulkCopyIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    verify_superadmin(current_user)
    get_port_or_404(db, payload.source_port_id)
    get_port_or_404(db, payload.target_port_id)
    ensure_default_catalog(db, payload.source_port_id)
    ensure_default_catalog(db, payload.target_port_id)

    ride_type_by_id = {item.id: item for item in db.query(PricingRideType).all()}

    source_categories = db.query(PricingVehicleCategory).filter(PricingVehicleCategory.port_id == payload.source_port_id).all()
    category_map: Dict[int, int] = {}
    if "vehicle_categories" in payload.modules:
        for source in source_categories:
            existing = (
                db.query(PricingVehicleCategory)
                .filter(
                    PricingVehicleCategory.port_id == payload.target_port_id,
                    PricingVehicleCategory.code == source.code,
                )
                .first()
            )
            if not existing:
                existing = PricingVehicleCategory(
                    port_id=payload.target_port_id,
                    code=source.code,
                    name=source.name,
                    icon_url=source.icon_url,
                    seating_capacity=source.seating_capacity,
                    description=source.description,
                    is_active=source.is_active,
                )
                db.add(existing)
                db.flush()
            else:
                existing.name = source.name
                existing.icon_url = source.icon_url
                existing.seating_capacity = source.seating_capacity
                existing.description = source.description
                existing.is_active = source.is_active
            category_map[source.id] = existing.id

    source_durations = db.query(PricingDuration).filter(PricingDuration.port_id == payload.source_port_id).all()
    duration_map: Dict[int, int] = {}
    if "durations" in payload.modules:
        for source in source_durations:
            ride_type = ride_type_by_id[source.ride_type_id]
            existing = (
                db.query(PricingDuration)
                .filter(
                    PricingDuration.port_id == payload.target_port_id,
                    PricingDuration.ride_type_id == source.ride_type_id,
                    PricingDuration.duration_minutes == source.duration_minutes,
                )
                .first()
            )
            if not existing:
                existing = PricingDuration(
                    port_id=payload.target_port_id,
                    ride_type_id=source.ride_type_id,
                    name=source.name,
                    duration_minutes=source.duration_minutes,
                    description=source.description,
                    is_active=source.is_active,
                    sort_order=source.sort_order,
                )
                db.add(existing)
                db.flush()
            else:
                existing.name = source.name
                existing.description = source.description
                existing.is_active = source.is_active
                existing.sort_order = source.sort_order
            duration_map[source.id] = existing.id

    if "provider_settings" in payload.modules:
        settings = db.query(PricingProviderSetting).filter(PricingProviderSetting.port_id == payload.source_port_id).all()
        for source in settings:
            existing = (
                db.query(PricingProviderSetting)
                .filter(
                    PricingProviderSetting.port_id == payload.target_port_id,
                    PricingProviderSetting.ride_type_id == source.ride_type_id,
                    PricingProviderSetting.provider_type == source.provider_type,
                )
                .first()
            )
            if not existing:
                existing = PricingProviderSetting(
                    port_id=payload.target_port_id,
                    ride_type_id=source.ride_type_id,
                    provider_type=source.provider_type,
                )
                db.add(existing)
            existing.minimum_bookable_hours = source.minimum_bookable_hours
            existing.is_active = source.is_active
            existing.config = source.config

    if "adjustment_types" in payload.modules:
        adjustments = db.query(PricingAdjustmentType).filter(PricingAdjustmentType.port_id == payload.source_port_id).all()
        for source in adjustments:
            existing = (
                db.query(PricingAdjustmentType)
                .filter(
                    PricingAdjustmentType.port_id == payload.target_port_id,
                    PricingAdjustmentType.ride_type_id == source.ride_type_id,
                    PricingAdjustmentType.code == source.code,
                )
                .first()
            )
            if not existing:
                existing = PricingAdjustmentType(
                    port_id=payload.target_port_id,
                    ride_type_id=source.ride_type_id,
                    code=source.code,
                )
                db.add(existing)
            existing.name = source.name
            existing.adjustment_kind = source.adjustment_kind
            existing.default_value = source.default_value
            existing.description = source.description
            existing.is_active = source.is_active

    if "pricing_rules" in payload.modules:
        rules = db.query(PricingRule).filter(PricingRule.port_id == payload.source_port_id).all()
        for source in rules:
            vehicle_category_id = category_map.get(source.vehicle_category_id)
            duration_id = duration_map.get(source.duration_id) if source.duration_id else None
            if not vehicle_category_id:
                continue
            existing = (
                db.query(PricingRule)
                .filter(
                    PricingRule.port_id == payload.target_port_id,
                    PricingRule.ride_type_id == source.ride_type_id,
                    PricingRule.provider_type == source.provider_type,
                    PricingRule.vehicle_category_id == vehicle_category_id,
                    PricingRule.duration_id == duration_id,
                    PricingRule.is_archived.is_(False),
                )
                .first()
            )
            if not existing:
                existing = PricingRule(
                    port_id=payload.target_port_id,
                    ride_type_id=source.ride_type_id,
                    provider_type=source.provider_type,
                    vehicle_category_id=vehicle_category_id,
                    duration_id=duration_id,
                    created_by=current_user.email,
                )
                db.add(existing)
            existing.base_fare = source.base_fare
            existing.minimum_fare = source.minimum_fare
            existing.price_per_km = source.price_per_km
            existing.price_per_minute = source.price_per_minute
            existing.free_waiting_minutes = source.free_waiting_minutes
            existing.extra_waiting_charge = source.extra_waiting_charge
            existing.cancellation_fee = source.cancellation_fee
            existing.included_km = source.included_km
            existing.price_per_extra_km = source.price_per_extra_km
            existing.price_per_extra_minute = source.price_per_extra_minute
            existing.price_per_extra_stop = source.price_per_extra_stop
            existing.platform_commission_pct = source.platform_commission_pct
            existing.adjustments = source.adjustments
            existing.pricing_metadata = source.pricing_metadata
            existing.is_active = source.is_active
            existing.is_archived = source.is_archived
            existing.updated_by = current_user.email

    if "visibility" in payload.modules:
        db.query(PricingDurationVisibility).filter(PricingDurationVisibility.port_id == payload.target_port_id).delete()
        db.query(PricingVehicleVisibility).filter(PricingVehicleVisibility.port_id == payload.target_port_id).delete()
        for source in db.query(PricingDurationVisibility).filter(PricingDurationVisibility.port_id == payload.source_port_id).all():
            mapped_duration_id = duration_map.get(source.duration_id)
            if not mapped_duration_id:
                continue
            db.add(
                PricingDurationVisibility(
                    port_id=payload.target_port_id,
                    ride_type_id=source.ride_type_id,
                    provider_type=source.provider_type,
                    duration_id=mapped_duration_id,
                    is_visible=source.is_visible,
                )
            )
        for source in db.query(PricingVehicleVisibility).filter(PricingVehicleVisibility.port_id == payload.source_port_id).all():
            mapped_duration_id = duration_map.get(source.duration_id)
            mapped_vehicle_id = category_map.get(source.vehicle_category_id)
            if not mapped_duration_id or not mapped_vehicle_id:
                continue
            db.add(
                PricingVehicleVisibility(
                    port_id=payload.target_port_id,
                    ride_type_id=source.ride_type_id,
                    provider_type=source.provider_type,
                    duration_id=mapped_duration_id,
                    vehicle_category_id=mapped_vehicle_id,
                    is_visible=source.is_visible,
                )
            )

    log_audit(
        db,
        entity_type="bulk_copy",
        entity_id=payload.target_port_id,
        action="copy",
        current_user=current_user,
        previous_values={"source_port_id": payload.source_port_id},
        current_values={"modules": payload.modules},
        port_id=payload.target_port_id,
    )
    db.commit()
    return {"ok": True}
