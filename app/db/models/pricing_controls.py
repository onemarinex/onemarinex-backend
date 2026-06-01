from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.db.base import Base


class PricingRideType(Base):
    __tablename__ = "pricing_ride_types"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, nullable=True)
    pricing_mode = Column(String(32), nullable=False, default="distance")
    supports_duration = Column(Boolean, nullable=False, default=False)
    supports_adjustments = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingVehicleCategory(Base):
    __tablename__ = "pricing_vehicle_categories"
    __table_args__ = (
        UniqueConstraint("port_id", "code", name="uq_pvc_port_code"),
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    icon_url = Column(String(512), nullable=True)
    seating_capacity = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingDuration(Base):
    __tablename__ = "pricing_durations"
    __table_args__ = (
        UniqueConstraint(
            "port_id",
            "ride_type_id",
            "duration_minutes",
            name="uq_pd_port_ride_minutes",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    ride_type_id = Column(Integer, ForeignKey("pricing_ride_types.id"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    duration_minutes = Column(Integer, nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    sort_order = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingProviderSetting(Base):
    __tablename__ = "pricing_provider_settings"
    __table_args__ = (
        UniqueConstraint(
            "port_id",
            "ride_type_id",
            "provider_type",
            name="uq_pps_port_ride_provider",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    ride_type_id = Column(Integer, ForeignKey("pricing_ride_types.id"), nullable=False, index=True)
    provider_type = Column(String(64), nullable=False)
    minimum_bookable_hours = Column(Float, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    config = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingAdjustmentType(Base):
    __tablename__ = "pricing_adjustment_types"
    __table_args__ = (
        UniqueConstraint(
            "port_id",
            "ride_type_id",
            "code",
            name="uq_pat_port_ride_code",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    ride_type_id = Column(Integer, ForeignKey("pricing_ride_types.id"), nullable=False, index=True)
    code = Column(String(64), nullable=False)
    name = Column(String(128), nullable=False)
    adjustment_kind = Column(String(32), nullable=False, default="multiplier")
    default_value = Column(Float, nullable=False, default=1.0)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingRule(Base):
    __tablename__ = "pricing_rules"

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    ride_type_id = Column(Integer, ForeignKey("pricing_ride_types.id"), nullable=False, index=True)
    provider_type = Column(String(64), nullable=False, index=True)
    vehicle_category_id = Column(Integer, ForeignKey("pricing_vehicle_categories.id"), nullable=False, index=True)
    duration_id = Column(Integer, ForeignKey("pricing_durations.id"), nullable=True, index=True)
    base_fare = Column(Float, nullable=False, default=0)
    minimum_fare = Column(Float, nullable=True)
    price_per_km = Column(Float, nullable=True)
    price_per_minute = Column(Float, nullable=True)
    free_waiting_minutes = Column(Float, nullable=True)
    extra_waiting_charge = Column(Float, nullable=True)
    cancellation_fee = Column(Float, nullable=True)
    included_km = Column(Float, nullable=True)
    price_per_extra_km = Column(Float, nullable=True)
    price_per_extra_minute = Column(Float, nullable=True)
    price_per_extra_stop = Column(Float, nullable=True)
    platform_commission_pct = Column(Float, nullable=True)
    adjustments = Column(JSON, nullable=True)
    pricing_metadata = Column(JSON, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    version = Column(Integer, nullable=False, default=1)
    copied_from_rule_id = Column(Integer, ForeignKey("pricing_rules.id"), nullable=True)
    created_by = Column(String(255), nullable=True)
    updated_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingDurationVisibility(Base):
    __tablename__ = "pricing_duration_visibilities"
    __table_args__ = (
        UniqueConstraint(
            "port_id",
            "ride_type_id",
            "provider_type",
            "duration_id",
            name="uq_pdv_port_ride_provider_duration",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    ride_type_id = Column(Integer, ForeignKey("pricing_ride_types.id"), nullable=False, index=True)
    provider_type = Column(String(64), nullable=False, index=True)
    duration_id = Column(Integer, ForeignKey("pricing_durations.id"), nullable=False, index=True)
    is_visible = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingVehicleVisibility(Base):
    __tablename__ = "pricing_vehicle_visibilities"
    __table_args__ = (
        UniqueConstraint(
            "port_id",
            "ride_type_id",
            "provider_type",
            "duration_id",
            "vehicle_category_id",
            name="uq_pvv_port_ride_provider_duration_vehicle",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=False, index=True)
    ride_type_id = Column(Integer, ForeignKey("pricing_ride_types.id"), nullable=False, index=True)
    provider_type = Column(String(64), nullable=False, index=True)
    duration_id = Column(Integer, ForeignKey("pricing_durations.id"), nullable=False, index=True)
    vehicle_category_id = Column(Integer, ForeignKey("pricing_vehicle_categories.id"), nullable=False, index=True)
    is_visible = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class PricingAuditLog(Base):
    __tablename__ = "pricing_audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    port_id = Column(Integer, ForeignKey("ports.id"), nullable=True, index=True)
    entity_type = Column(String(64), nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    action = Column(String(64), nullable=False)
    created_by = Column(String(255), nullable=True)
    previous_values = Column(JSON, nullable=True)
    current_values = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
