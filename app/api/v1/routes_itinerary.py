"""
Itinerary Suggestion API
========================
POST /api/v1/itinerary/suggest
GET /api/v1/itinerary/tags
"""

from __future__ import annotations

import math
import random
import statistics
from typing import List, Optional, Tuple
from itertools import permutations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models.vendors import Vendors
from app.db.models.port import Port
from app.db.models.pricing_controls import PricingDuration, PricingRideType, PricingRule
from app.db.models.vendor_tag import VendorTag
from app.db.session import get_db


# ── Helpers ──────────────────────────────────────────────────────────────────

from datetime import datetime

DAY_ABBREV = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
SHORT_TO_FULL = {"m": "Mon", "mon": "Mon", "t": "Tue", "tue": "Tue", "w": "Wed", "wed": "Wed", "th": "Thu", "thu": "Thu", "f": "Fri", "fri": "Fri", "sa": "Sat", "sat": "Sat", "su": "Sun", "sun": "Sun"}


def _normalize_days(raw) -> list[str] | None:
    if not raw:
        return None
    if isinstance(raw, list):
        return raw if raw else None
    if isinstance(raw, str):
        cleaned = raw.strip()
        if not cleaned or cleaned.lower() == "all":
            return None
        parts = [p.strip() for p in cleaned.split(",") if p.strip()]
        if not parts:
            return None
        result = []
        for p in parts:
            low = p.lower()
            result.append(SHORT_TO_FULL.get(low, p))
        return result
    return None


def _parse_hhmm(time_str: str | None) -> tuple[int, int] | None:
    if not time_str or not isinstance(time_str, str):
        return None
    parts = time_str.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        h, m = int(parts[0]), int(parts[1])
        return (h, m)
    except (ValueError, TypeError):
        return None


def vendor_is_currently_open(other_information: dict | None) -> bool:
    if not other_information or not isinstance(other_information, dict):
        return True
    today = DAY_ABBREV.get(datetime.now().weekday(), "")
    working_days = _normalize_days(other_information.get("working_days"))
    if working_days and today not in working_days:
        return False
    opening = _parse_hhmm(other_information.get("open_time"))
    closing = _parse_hhmm(other_information.get("close_time"))
    if not opening and not closing:
        return True
    now = datetime.now()
    now_minutes = now.hour * 60 + now.minute
    if opening and closing:
        open_min = opening[0] * 60 + opening[1]
        close_min = closing[0] * 60 + closing[1]
        return open_min <= now_minutes < close_min
    if closing:
        close_min = closing[0] * 60 + closing[1]
        return now_minutes < close_min
    if opening:
        open_min = opening[0] * 60 + opening[1]
        return now_minutes >= open_min
    return True

router = APIRouter()

# ── Constants ────────────────────────────────────────────────────────────────

MAX_ITINERARIES = 6
MAX_STOPS_PER_ITINERARY = 8
DEFAULT_TRAVEL_SPEED_KMPH = 20.0
DEFAULT_PORT_DISTANCE_KM = 3.0
MAX_STOP_DWELL_HOURS = 4.0
MIN_STOP_DWELL_HOURS = 0.25

# Tags that can appear multiple times in an itinerary
MULTI_VISIT_TAGS = {"fun_zone", "explore_places"}

# Fallback valid tags
FALLBACK_VALID_TAGS = [
    "food", "pubs", "sightseeing", "shopping", 
    "relax", "nightlife", "sim_card", "currency",
    "massage", "wellness", "utility"
]

# Default time by category when not specified
DEFAULT_TIME_BY_CATEGORY = {
    "restaurant": 1.0,
    "pub": 1.5,
    "hotel": 2.0,
    "sightseeing": 1.0,
    "shopping": 1.5,
    "relax": 2.0,
}


# ── Schemas ──────────────────────────────────────────────────────────────────

class ItinerarySuggestIn(BaseModel):
    port_id: Optional[int] = None
    port: Optional[str] = None
    hours: float = Field(..., gt=0, description="Total hours the user has available")
    tags: List[str] = Field(..., min_length=1, description="Activity tags the user wants")


class ItineraryStop(BaseModel):
    vendor_id: int
    name: str
    category: str
    tags: List[str]
    avg_time_hours: float
    distance_from_port: float
    rating: float
    price_per_person: Optional[float] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    image_url: Optional[str] = None
    timings: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    description: Optional[str] = None
    travel_minutes_from_prev: Optional[float] = None
    travel_minutes_to_port: Optional[float] = None
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    working_days: Optional[str] = None


class ItineraryOption(BaseModel):
    itinerary_number: int
    label: str
    total_hours: float
    total_distance_km: float
    estimated_travel_minutes: float
    total_stops: int
    highlights: List[str]
    stops: List[ItineraryStop]


class ItinerarySuggestOut(BaseModel):
    port_id: Optional[int]
    port_name: Optional[str]
    requested_hours: float
    requested_tags: List[str]
    itineraries: List[ItineraryOption]
    fallback_used: bool


class ItineraryTagOut(BaseModel):
    id: int
    name: str
    slug: str
    image_url: Optional[str]
    sort_order: int


class ItineraryCatalogOut(BaseModel):
    port_id: Optional[int]
    port_name: Optional[str]
    total: int
    vendors: List[ItineraryStop]


# ── Core Helpers ────────────────────────────────────────────────────────────

def clean_tag(tag: str) -> str:
    """Normalize a tag string."""
    return tag.strip().lower().replace(" ", "_").replace("-", "_")


def get_vendor_tags_from_db(vendor: Vendors) -> set:
    """Extract tags from vendor's other_information."""
    other = vendor.other_information or {}
    if not isinstance(other, dict):
        return set()
    
    raw_tags = other.get("tags") or []
    if not isinstance(raw_tags, list):
        return set()
    
    return {clean_tag(str(t)) for t in raw_tags if t and str(t).strip()}


def get_stop_tags(stop: ItineraryStop) -> set:
    """Extract tags from an ItineraryStop."""
    return {clean_tag(t) for t in (stop.tags or []) if t and str(t).strip()}


def vendor_matches_tags(vendor: Vendors, requested_tags: List[str]) -> bool:
    """Check if vendor has any of the requested tags."""
    vendor_tags = get_vendor_tags_from_db(vendor)
    requested_set = {clean_tag(t) for t in requested_tags}
    return bool(vendor_tags & requested_set)


def get_matching_tags_for_stop(stop: ItineraryStop, requested_tags: List[str]) -> List[str]:
    """Get the requested tags that this stop has."""
    stop_tags = get_stop_tags(stop)
    requested_set = {clean_tag(t) for t in requested_tags}
    return [t for t in requested_tags if clean_tag(t) in stop_tags]


def vendor_to_stop(vendor: Vendors) -> ItineraryStop:
    """Convert a Vendors DB model to ItineraryStop."""
    other = vendor.other_information or {}
    if not isinstance(other, dict):
        other = {}
    
    # Extract tags
    raw_tags = other.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [clean_tag(str(t)) for t in raw_tags if t and str(t).strip()]
    
    # Extract time
    avg_time = other.get("avg_time_spent_hours")
    if avg_time is None:
        avg_time = DEFAULT_TIME_BY_CATEGORY.get(
            str(vendor.category or "").strip().lower(), 1.0
        )
    else:
        try:
            avg_time = float(avg_time)
        except (TypeError, ValueError):
            avg_time = 1.0
    
    # Extract price
    price = other.get("price_per_person")
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
    
    # Extract image
    image_url = None
    if vendor.images:
        if isinstance(vendor.images, list) and vendor.images:
            first = vendor.images[0]
            if isinstance(first, dict):
                image_url = first.get("url")
            else:
                image_url = str(first)
        elif isinstance(vendor.images, dict):
            image_url = vendor.images.get("primary") or vendor.images.get("url")
    
    return ItineraryStop(
        vendor_id=vendor.id,
        name=vendor.name,
        category=str(vendor.category or ""),
        tags=tags,
        avg_time_hours=max(MIN_STOP_DWELL_HOURS, float(avg_time)),
        distance_from_port=float(vendor.distance_from_port or 0.0),
        rating=float(vendor.rating or 0.0),
        price_per_person=price,
        address=vendor.location_name,
        phone=vendor.phone,
        image_url=image_url,
        timings=other.get("timings"),
        lat=float(vendor.lat) if vendor.lat is not None else None,
        lng=float(vendor.lng) if vendor.lng is not None else None,
        description=other.get("about") or other.get("description"),
        travel_minutes_from_prev=None,
        travel_minutes_to_port=None,
        open_time=other.get("open_time"),
        close_time=other.get("close_time"),
        working_days=other.get("working_days"),
    )


def find_port(db: Session, port_id: Optional[int], port: Optional[str]) -> Optional[Port]:
    """Find port by ID or name/code."""
    if port_id:
        return db.query(Port).filter(Port.id == port_id).first()
    if port:
        norm = port.strip()
        if norm.isdigit():
            return db.query(Port).filter(Port.id == int(norm)).first()
        return (
            db.query(Port)
            .filter((Port.name.ilike(norm)) | (Port.code.ilike(norm)))
            .first()
        )
    return None


def get_distance_speed(db: Session, port_id: Optional[int], hours: float) -> Tuple[Optional[float], float]:
    """Get distance cap and speed from pricing rules."""
    if not port_id:
        return None, DEFAULT_TRAVEL_SPEED_KMPH
    
    ride_type = db.query(PricingRideType).filter(PricingRideType.code == "package_trip").first()
    if not ride_type:
        return None, DEFAULT_TRAVEL_SPEED_KMPH
    
    durations = (
        db.query(PricingDuration)
        .filter(
            PricingDuration.port_id == port_id,
            PricingDuration.ride_type_id == ride_type.id,
            PricingDuration.is_active.is_(True),
        )
        .all()
    )
    if not durations:
        return None, DEFAULT_TRAVEL_SPEED_KMPH
    
    requested_minutes = int(round(hours * 60))
    selected = min(durations, key=lambda d: abs((d.duration_minutes or 0) - requested_minutes))
    
    rules = (
        db.query(PricingRule)
        .filter(
            PricingRule.port_id == port_id,
            PricingRule.ride_type_id == ride_type.id,
            PricingRule.duration_id == selected.id,
            PricingRule.is_active.is_(True),
            PricingRule.is_archived.is_(False),
        )
        .all()
    )
    
    # Distance cap
    km_limits = [
        float(r.included_km) for r in rules 
        if r.included_km is not None and r.included_km > 0
    ]
    km_cap = None
    if km_limits:
        median_km = statistics.median(km_limits)
        filtered = [k for k in km_limits if k >= median_km * 0.5]
        km_cap = min(filtered) if filtered else median_km
    
    # Speed
    speeds = []
    for rule in rules:
        meta = rule.pricing_metadata or {}
        if isinstance(meta, dict):
            for key in ("speed_kmph", "avg_speed_kmph", "assumed_speed_kmph"):
                try:
                    val = float(meta.get(key))
                    if val > 0:
                        speeds.append(val)
                except (TypeError, ValueError):
                    continue
    
    speed_kmph = min(speeds) if speeds else DEFAULT_TRAVEL_SPEED_KMPH
    return km_cap, speed_kmph


# ── Distance Calculations ──────────────────────────────────────────────────

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate distance between two points in km."""
    if None in (lat1, lng1, lat2, lng2):
        return 0.0
    
    R = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def distance_between_stops(stop1: ItineraryStop, stop2: ItineraryStop, fallback_km: float) -> float:
    """Calculate distance between two stops."""
    if stop1.lat is not None and stop1.lng is not None and stop2.lat is not None and stop2.lng is not None:
        return haversine_km(stop1.lat, stop1.lng, stop2.lat, stop2.lng)
    # Fallback: use distance from port as approximation
    return abs(float(stop1.distance_from_port or 0) - float(stop2.distance_from_port or 0)) + fallback_km


def route_distance_for_order(stops: List[ItineraryStop], fallback_km: float) -> float:
    """
    Calculate total route distance: port -> stop1 -> stop2 -> ... -> port.
    This is the exact distance for a given order of stops.
    """
    if not stops:
        return 0.0
    
    total = 0.0
    
    # Port to first stop
    total += float(stops[0].distance_from_port or fallback_km)
    
    # Between stops
    for i in range(len(stops) - 1):
        total += distance_between_stops(stops[i], stops[i + 1], fallback_km)
    
    # Last stop to port
    total += float(stops[-1].distance_from_port or fallback_km)
    
    return total


def find_optimal_order(stops: List[ItineraryStop], fallback_km: float) -> Tuple[List[ItineraryStop], float]:
    """
    Find the optimal order of stops to minimize travel distance.
    Uses a greedy nearest-neighbor approach for efficiency.
    For small sets (<=4), tries all permutations for optimal result.
    """
    if len(stops) <= 1:
        return stops, route_distance_for_order(stops, fallback_km)
    
    # For small sets, try all permutations to find the absolute best
    if len(stops) <= 4:
        best_order = None
        best_distance = float('inf')
        
        for perm in permutations(stops):
            dist = route_distance_for_order(list(perm), fallback_km)
            if dist < best_distance:
                best_distance = dist
                best_order = list(perm)
        
        return best_order, best_distance
    
    # For larger sets, use greedy nearest-neighbor
    # Start with the stop closest to port
    remaining = stops.copy()
    start = min(remaining, key=lambda s: s.distance_from_port or float('inf'))
    remaining.remove(start)
    
    ordered = [start]
    total_distance = float(start.distance_from_port or fallback_km)
    
    # Greedily add nearest neighbor
    while remaining:
        last = ordered[-1]
        nearest = min(remaining, key=lambda s: distance_between_stops(last, s, fallback_km))
        remaining.remove(nearest)
        ordered.append(nearest)
        total_distance += distance_between_stops(last, nearest, fallback_km)
    
    # Return to port
    total_distance += float(ordered[-1].distance_from_port or fallback_km)
    
    return ordered, total_distance


# ── Itinerary Generation ────────────────────────────────────────────────────

def validate_no_repeated_tags(stops: List[ItineraryStop], requested_tags: List[str]) -> bool:
    """Validate that non-multi-visit tags appear only once."""
    all_tags = []
    for stop in stops:
        stop_tags = get_matching_tags_for_stop(stop, requested_tags)
        all_tags.extend(stop_tags)
    
    tag_counts = {}
    for tag in all_tags:
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    for tag, count in tag_counts.items():
        if count > 1 and tag not in MULTI_VISIT_TAGS:
            return False
    
    return True


def stop_dwell_bounds(stop: ItineraryStop) -> Tuple[float, float]:
    """Get min and max dwell time in minutes for a stop."""
    category = clean_tag(stop.category or "")
    
    if category in {"restaurant", "food", "cafe", "dining"}:
        return 45.0, 120.0
    if category in {"pub", "bar", "nightlife", "fun_zone", "entertainment"}:
        return 60.0, 150.0
    if category in {"sightseeing", "attraction", "landmark", "beach", "park", "museum"}:
        return 45.0, 180.0
    if category in {"shopping", "market"}:
        return 30.0, 120.0
    if category in {"spa", "relax", "wellness"}:
        return 45.0, 150.0
    
    return 40.0, 120.0


def stop_weight(stop: ItineraryStop, requested_tags: List[str]) -> float:
    """Calculate weight for a stop based on tag matches, rating, and distance."""
    matched_count = len(get_matching_tags_for_stop(stop, requested_tags))
    rating = max(0.0, float(stop.rating or 0.0))
    distance = max(0.0, float(stop.distance_from_port or 0.0))
    distance_factor = 1.0 / (1.0 + (distance / 25.0))
    return max(0.1, (1.0 + (0.9 * matched_count) + (0.12 * rating)) * distance_factor)


def apply_dynamic_dwell(
    stops: List[ItineraryStop],
    hours_budget: float,
    travel_minutes: float,
    requested_tags: List[str],
) -> List[ItineraryStop]:
    """Adjust dwell times to fill the available budget."""
    if not stops:
        return stops
    
    budget_minutes = max(0.0, hours_budget * 60.0)
    reserve_minutes = max(10.0, min(30.0, budget_minutes * 0.05))
    target_dwell = max(0.0, budget_minutes - max(0.0, travel_minutes) - reserve_minutes)
    
    # Get bounds and weights for each stop
    mins, maxs, weights = [], [], []
    for stop in stops:
        min_m, max_m = stop_dwell_bounds(stop)
        mins.append(min_m)
        maxs.append(max(max_m, min_m))
        weights.append(stop_weight(stop, requested_tags))
    
    floor_sum = sum(mins)
    if target_dwell <= 0.0:
        target_dwell = floor_sum
    
    # Allocate dwell times
    if target_dwell < floor_sum:
        # Scale down proportionally
        ratio = target_dwell / floor_sum if floor_sum > 0 else 0.0
        dwell = [max(15.0, m * ratio) for m in mins]
        current = sum(dwell)
        if current > target_dwell and current > 0:
            shrink = target_dwell / current
            dwell = [max(15.0, d * shrink) for d in dwell]
    else:
        # Start with minimums and distribute extra
        dwell = mins[:]
        extra = min(
            target_dwell - floor_sum,
            sum(maxs[i] - mins[i] for i in range(len(stops)))
        )
        
        while extra > 0.01:
            growable = [i for i in range(len(stops)) if dwell[i] < maxs[i] - 0.01]
            if not growable:
                break
            
            weight_sum = sum(weights[i] for i in growable) or float(len(growable))
            for i in growable:
                share = extra * (weights[i] / weight_sum)
                cap = maxs[i] - dwell[i]
                add = min(cap, share)
                if add > 0:
                    dwell[i] += add
                    extra -= add
    
    # Apply to stops
    result = []
    for i, stop in enumerate(stops):
        new_stop = stop.model_copy(deep=True) if hasattr(stop, "model_copy") else stop.copy(deep=True)
        new_stop.avg_time_hours = round(max(MIN_STOP_DWELL_HOURS, dwell[i] / 60.0), 2)
        result.append(new_stop)
    
    return result


def pack_itinerary(
    candidates: List[ItineraryStop],
    hours_budget: float,
    km_budget: Optional[float],
    speed_kmph: float,
    fallback_km: float,
    requested_tags: List[str],
) -> Optional[Tuple[List[ItineraryStop], float, float]]:
    """
    Pack the best possible itinerary from candidates.
    Returns (stops_in_optimal_order, total_km, travel_minutes) or None.
    """
    if not candidates:
        return None
    
    effective_speed = max(5.0, speed_kmph)
    best_stops = None
    best_score = -1
    best_route_km = float('inf')
    
    # Try different starting points
    max_starts = min(20, len(candidates))
    for start_idx in range(max_starts):
        # Rotate candidates to get different starting points
        ordered = candidates[start_idx:] + candidates[:start_idx]
        
        current_stops = []
        used_ids = set()
        tag_counts = {}
        
        # Build itinerary greedily
        for stop in ordered:
            if len(current_stops) >= MAX_STOPS_PER_ITINERARY:
                break
            
            vendor_id = int(stop.vendor_id)
            if vendor_id in used_ids:
                continue
            
            # Check tag constraints (non-multi-visit tags only once)
            matched = get_matching_tags_for_stop(stop, requested_tags)
            skip = False
            for tag in matched:
                if tag not in MULTI_VISIT_TAGS and tag_counts.get(tag, 0) >= 1:
                    skip = True
                    break
            if skip:
                continue
            
            # Test if we can add this stop
            proposed_stops = current_stops + [stop]
            
            # Find optimal order for the proposed stops
            optimized_stops, route_km = find_optimal_order(proposed_stops, fallback_km)
            
            if km_budget is not None and route_km > km_budget * 1.1:
                continue
            
            travel_hours = route_km / effective_speed
            total_hours = sum(s.avg_time_hours for s in proposed_stops) + travel_hours
            
            if total_hours <= hours_budget * 1.05:
                current_stops.append(stop)
                used_ids.add(vendor_id)
                for tag in matched:
                    if tag not in MULTI_VISIT_TAGS:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
        
        # Score this itinerary
        if current_stops:
            # Find optimal order for the final set
            optimized_stops, route_km = find_optimal_order(current_stops, fallback_km)
            travel_hours = route_km / effective_speed
            total_hours = sum(s.avg_time_hours for s in current_stops) + travel_hours
            
            # Score: prefer more stops and more time fill, penalize distance
            score = total_hours * 10 + len(current_stops) * 5 - (route_km / 5)
            
            # Prefer itineraries that fill the budget well and have shorter routes
            if total_hours <= hours_budget and score > best_score:
                best_score = score
                best_stops = optimized_stops  # Store already optimized order
                best_route_km = route_km
    
    if not best_stops:
        return None
    
    # Recalculate with the optimized order
    total_km = route_distance_for_order(best_stops, fallback_km)
    travel_minutes = (total_km / effective_speed) * 60
    
    # Apply dynamic dwell times
    tuned_stops = apply_dynamic_dwell(
        best_stops,
        hours_budget,
        travel_minutes,
        requested_tags,
    )
    
    # Re-optimize order after dwell time adjustments (dwell doesn't affect distance)
    # But we already have the optimal order, so just use it
    
    return (tuned_stops, total_km, travel_minutes)


def label_for(stops: List[ItineraryStop], number: int) -> str:
    """Generate a label for an itinerary."""
    if not stops:
        return f"Option {number}"
    if len(stops) == 1:
        return f"Option {number}: {stops[0].name}"
    return f"Option {number}: {stops[0].name} + {len(stops) - 1} more"


# ── API Endpoints ──────────────────────────────────────────────────────────

@router.get("/tags", response_model=List[ItineraryTagOut])
def list_itinerary_tags(db: Session = Depends(get_db)):
    """Get all available tags for itineraries."""
    try:
        VendorTag.__table__.create(bind=db.get_bind(), checkfirst=True)
        rows = (
            db.query(VendorTag)
            .filter(VendorTag.is_active.is_(True))
            .order_by(VendorTag.sort_order.asc(), VendorTag.name.asc())
            .all()
        )
        return [
            ItineraryTagOut(
                id=item.id,
                name=item.name,
                slug=item.slug,
                image_url=item.image_url,
                sort_order=item.sort_order,
            )
            for item in rows
        ]
    except Exception:
        return [
            ItineraryTagOut(
                id=index + 1,
                name=slug.replace("_", " ").title(),
                slug=slug,
                image_url=None,
                sort_order=index,
            )
            for index, slug in enumerate(FALLBACK_VALID_TAGS)
        ]

@router.get("/catalog", response_model=ItineraryCatalogOut)
def itinerary_catalog(
    port_id: Optional[int] = None,
    port: Optional[str] = None,
    tags: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Get catalog of vendors for a port, optionally filtered by tags."""
    resolved_port = find_port(db, port_id, port)
    requested_tags = {
        item.strip().lower()
        for item in (tags or "").split(",")
        if item.strip()
    }

    query = db.query(Vendors).filter(Vendors.status == "Active")
    if resolved_port:
        query = query.filter(Vendors.port_id == resolved_port.id)

    vendors = query.order_by(Vendors.rating.desc()).all()
    stops = [vendor_to_stop(item) for item in vendors]
    
    if requested_tags:
        stops = [
            item for item in stops 
            if get_matching_tags_for_stop(item, list(requested_tags))
        ]

    return ItineraryCatalogOut(
        port_id=resolved_port.id if resolved_port else None,
        port_name=resolved_port.name if resolved_port else None,
        total=len(stops),
        vendors=stops,
    )


@router.post("/suggest", response_model=ItinerarySuggestOut)
def suggest_itinerary(body: ItinerarySuggestIn, db: Session = Depends(get_db)):
    """Suggest itineraries based on user's tags and time."""
    
    # ── 1. Normalize requested tags ──
    requested_tags = list(dict.fromkeys(
        clean_tag(t) for t in body.tags if t and t.strip()
    ))
    if not requested_tags:
        raise HTTPException(status_code=422, detail="No tags provided")
    
    # ── 2. Find port ──
    port = find_port(db, body.port_id, body.port)
    if not port:
        raise HTTPException(status_code=404, detail="Port not found")
    
    # ── 3. Get distance cap and speed ──
    km_cap, speed_kmph = get_distance_speed(db, port.id, body.hours)
    
    # ── 4. Get all vendors for this port ──
    vendors = (
        db.query(Vendors)
        .filter(Vendors.status == "Active", Vendors.port_id == port.id)
        .all()
    )
    
    # ── 4a. Filter out currently closed facilities ──
    # vendors = [v for v in vendors if vendor_is_currently_open(v.other_information)]
    
    if not vendors:
        return ItinerarySuggestOut(
            port_id=port.id,
            port_name=port.name,
            requested_hours=body.hours,
            requested_tags=requested_tags,
            itineraries=[],
            fallback_used=True,
        )
    
    # ── 5. Filter vendors by requested tags ──
    matched_vendors = [v for v in vendors if vendor_matches_tags(v, requested_tags)]
    
    if not matched_vendors:
        return ItinerarySuggestOut(
            port_id=port.id,
            port_name=port.name,
            requested_hours=body.hours,
            requested_tags=requested_tags,
            itineraries=[],
            fallback_used=True,
        )
    
    # ── 6. Convert to stops ──
    all_stops = [vendor_to_stop(v) for v in matched_vendors]
    
    # Deduplicate
    seen_ids = set()
    stops = []
    for stop in all_stops:
        if stop.vendor_id not in seen_ids:
            seen_ids.add(stop.vendor_id)
            stops.append(stop)
    
    # Sort by rating for quality
    stops.sort(key=lambda s: s.rating or 0, reverse=True)
    
    # ── 7. Calculate fallback distance ──
    positive_distances = [
        float(s.distance_from_port) for s in stops 
        if s.distance_from_port and s.distance_from_port > 0
    ]
    fallback_km = (
        sum(positive_distances) / len(positive_distances) 
        if positive_distances 
        else DEFAULT_PORT_DISTANCE_KM
    )
    
    # ── 8. Helper: Check if we should skip min fill requirement ──
    def should_skip_min_fill(stops_result: List[ItineraryStop], requested_tags: List[str]) -> bool:
        """
        Skip 50% min fill when only single-occurrence tags are requested.
        This allows itineraries with only 1 bar or 1 food to be returned.
        """
        # Check if any requested tag allows multiple visits
        has_multi_visit = any(
            tag in MULTI_VISIT_TAGS 
            for tag in requested_tags
        )
        
        # If we have multi-visit tags, we should be able to fill more time
        if has_multi_visit:
            return False
        
        # Check if all requested tags are covered
        covered_tags = set()
        for stop in stops_result:
            matched = get_matching_tags_for_stop(stop, requested_tags)
            covered_tags.update(matched)
        
        # If we've covered all requested tags, skip the 50% rule
        # Because we literally can't add more of these tags
        return len(covered_tags) == len(requested_tags)
    
    # ── 9. Helper: Dynamic duplicate threshold ──
    def is_duplicate_itinerary(combo_key: tuple, used_combos: set) -> bool:
        """Check if itinerary is too similar to existing ones."""
        if not used_combos:
            return False
        
        # For small itineraries, use more lenient thresholds
        if len(combo_key) <= 2:
            similarity_threshold = 0.5 if len(combo_key) == 1 else 0.6
        else:
            similarity_threshold = 0.7
        
        for existing in used_combos:
            intersection = len(set(existing) & set(combo_key))
            union = len(set(existing) | set(combo_key))
            similarity = intersection / union if union > 0 else 0
            if similarity >= similarity_threshold:
                return True
        
        return False
    
    # ── 10. Generate itineraries ──
    itineraries = []
    used_combos = set()
    used_tag_sets = set()
    
    # Fixed random seed for reproducibility
    random.seed(42)
    
    # Track if we've found any valid itinerary
    found_any = False
    
    # Try many combinations
    max_attempts = 2000
    
    for attempt in range(max_attempts):
        if len(itineraries) >= MAX_ITINERARIES:
            break
        
        # Shuffle with different seed each time
        shuffled = stops.copy()
        random.seed(attempt * 31 + 17)
        random.shuffle(shuffled)
        
        # Take a slice of candidates
        slice_size = min(max(5, len(shuffled) // 2 + attempt % 8), len(shuffled))
        start_idx = (attempt * 7) % max(1, len(shuffled) - slice_size)
        candidates = shuffled[start_idx:start_idx + slice_size]
        
        if len(candidates) < 2:
            candidates = shuffled[:min(10, len(shuffled))]
        
        # Try different budget targets (60-100% - wider range for single tags)
        if len(requested_tags) == 1 and requested_tags[0] not in MULTI_VISIT_TAGS:
            # For single non-multi tags, allow lower fill targets
            budget_factor = 0.60 + (attempt % 40) * 0.01
        else:
            budget_factor = 0.85 + (attempt % 16) * 0.01
        
        adjusted_budget = body.hours * budget_factor
        
        result = pack_itinerary(
            candidates=candidates,
            hours_budget=adjusted_budget,
            km_budget=km_cap,
            speed_kmph=speed_kmph,
            fallback_km=fallback_km,
            requested_tags=requested_tags,
        )
        
        if not result:
            continue
        
        stops_result, total_km, travel_minutes = result
        
        # Validate no repeated tags
        if not validate_no_repeated_tags(stops_result, requested_tags):
            continue
        
        total_hours = round(
            sum(s.avg_time_hours for s in stops_result) + travel_minutes / 60,
            2
        )
        
        # ── 11. Conditional acceptance logic ──
        skip_min_fill = should_skip_min_fill(stops_result, requested_tags)
        
        # Accept if:
        # - Either we skip min fill, OR it fills at least 50% of budget
        # - And it doesn't exceed budget by >15%
        if not skip_min_fill and total_hours < body.hours * 0.5:
            continue
        if total_hours > body.hours * 1.15:
            continue
        
        # Create unique key
        combo_key = tuple(sorted(int(s.vendor_id) for s in stops_result))
        
        # Check for duplicates using dynamic threshold
        if is_duplicate_itinerary(combo_key, used_combos):
            continue
        
        # Get tag combination
        tag_combo = tuple(sorted(set(
            t for s in stops_result
            for t in get_matching_tags_for_stop(s, requested_tags)
        )))
        
        # Check if we have similar tag combinations
        if tag_combo in used_tag_sets:
            # Only allow if it's significantly different in vendors
            is_similar = False
            for existing in used_combos:
                existing_set = set(existing)
                current_set = set(combo_key)
                if len(existing_set & current_set) / len(current_set) > 0.6:
                    is_similar = True
                    break
            if is_similar:
                continue
        
        # Add to results
        used_combos.add(combo_key)
        used_tag_sets.add(tag_combo)
        found_any = True
        
        highlights = sorted({
            t for s in stops_result
            for t in get_matching_tags_for_stop(s, requested_tags)
        })
        
        # Calculate diversity score for sorting later
        tag_diversity = len(set(
            t for s in stops_result
            for t in s.tags
        ))
        
        # Create ItineraryOption with diversity score stored temporarily
        itinerary = ItineraryOption(
            itinerary_number=len(itineraries) + 1,
            label=label_for(stops_result, len(itineraries) + 1),
            total_hours=total_hours,
            total_distance_km=round(total_km, 2),
            estimated_travel_minutes=round(travel_minutes, 1),
            total_stops=len(stops_result),
            highlights=highlights,
            stops=stops_result,
        )
        # Store diversity score as attribute for sorting
        itinerary._diversity_score = tag_diversity
        itineraries.append(itinerary)
    
    # ── 12. Fallback: Try more aggressive approach ──
    if len(itineraries) < 3 and len(stops) >= 3:
        for attempt in range(500):
            if len(itineraries) >= MAX_ITINERARIES:
                break
            
            shuffled = stops.copy()
            random.seed(attempt * 137 + 89)
            random.shuffle(shuffled)
            
            # Use more candidates
            candidates = shuffled[:min(20, len(shuffled))]
            
            # Try different budget fills with wider range
            if len(requested_tags) == 1 and requested_tags[0] not in MULTI_VISIT_TAGS:
                budget_factor = 0.50 + (attempt % 50) * 0.01
            else:
                budget_factor = 0.75 + (attempt % 26) * 0.01
            
            adjusted_budget = body.hours * budget_factor
            
            result = pack_itinerary(
                candidates=candidates,
                hours_budget=adjusted_budget,
                km_budget=km_cap,
                speed_kmph=speed_kmph,
                fallback_km=fallback_km,
                requested_tags=requested_tags,
            )
            
            if not result:
                continue
            
            stops_result, total_km, travel_minutes = result
            
            if not validate_no_repeated_tags(stops_result, requested_tags):
                continue
            
            total_hours = round(
                sum(s.avg_time_hours for s in stops_result) + travel_minutes / 60,
                2
            )
            
            # ── 13. More lenient fallback acceptance ──
            skip_min_fill = should_skip_min_fill(stops_result, requested_tags)
            
            # More lenient in fallback
            if not skip_min_fill and total_hours < body.hours * 0.4:
                continue
            if total_hours > body.hours * 1.2:
                continue
            
            combo_key = tuple(sorted(int(s.vendor_id) for s in stops_result))
            
            # Check for duplicates
            if is_duplicate_itinerary(combo_key, used_combos):
                continue
            
            used_combos.add(combo_key)
            found_any = True
            
            highlights = sorted({
                t for s in stops_result
                for t in get_matching_tags_for_stop(s, requested_tags)
            })
            
            tag_diversity = len(set(
                t for s in stops_result
                for t in s.tags
            ))
            
            itinerary = ItineraryOption(
                itinerary_number=len(itineraries) + 1,
                label=label_for(stops_result, len(itineraries) + 1),
                total_hours=total_hours,
                total_distance_km=round(total_km, 2),
                estimated_travel_minutes=round(travel_minutes, 1),
                total_stops=len(stops_result),
                highlights=highlights,
                stops=stops_result,
            )
            itinerary._diversity_score = tag_diversity
            itineraries.append(itinerary)
    
    # ── 14. Sort and finalize ──
    
    # Primary sort: Prefer itineraries that cover more requested tags
    def get_cover_score(itin):
        covered = len(set(
            t for s in itin.stops
            for t in get_matching_tags_for_stop(s, requested_tags)
        ))
        return covered / len(requested_tags) if requested_tags else 0
    
    # Secondary sort: Closer to requested hours
    def get_hours_score(itin):
        return abs(itin.total_hours - body.hours)
    
    # Tertiary sort: More diverse tags (more interesting)
    def get_diversity_score(itin):
        return getattr(itin, '_diversity_score', 0)
    
    # Sort by: coverage (desc), hours fit (asc), diversity (desc)
    itineraries.sort(
        key=lambda i: (
            -get_cover_score(i),      # More requested tags covered first
            get_hours_score(i),       # Closer to requested hours
            -get_diversity_score(i)   # More diverse tags
        )
    )
    
    # Reassign numbers and clean up
    for idx, itin in enumerate(itineraries):
        itin.itinerary_number = idx + 1
        itin.label = label_for(itin.stops, idx + 1)
        
        # Remove hidden field if it exists
        if hasattr(itin, '_diversity_score'):
            delattr(itin, '_diversity_score')
    
    # ── 15. Return response ──
    return ItinerarySuggestOut(
        port_id=port.id,
        port_name=port.name,
        requested_hours=body.hours,
        requested_tags=requested_tags,
        itineraries=itineraries[:MAX_ITINERARIES],
        fallback_used=not found_any,
    )

