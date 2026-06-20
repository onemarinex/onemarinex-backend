"""
Itinerary Suggestion API
========================
POST /api/v1/itinerary/suggest

User sends:
  - port_id  (or port name/code)
  - hours    total time available (float)
  - tags     list of activity tags they want
              e.g. ["food", "pubs", "sightseeing", "shopping",
                     "relax", "nightlife", "sim_card", "currency"]

Returns:
  Up to 6 itinerary options, each a curated ordered list of stops
  (vendor items) whose total avg_time_spent_hours fits within the
  user's available hours.

Matching rules (in priority order):
  1. Vendors whose tags overlap with requested tags AND have an
     avg_time_spent_hours configured.
  2. Vendors whose tags overlap with requested tags (time unknown –
     fallback default applied per category).
  3. If fewer than 3 items survive, top-rated vendors from the
     requested-tag categories fill the remainder.

Itinerary building:
  - Sort surviving candidates by rating descending.
  - Use a sliding window / greedy pack to build up to 6 distinct
    itineraries that respect the hours budget.
  - Itinerary 1 = highest-rated pack within budget.
  - Subsequent itineraries swap in the next-best vendor per category
    to give variety.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models.vendors import Vendors
from app.db.models.port import Port
from app.db.models.pricing_controls import PricingDuration, PricingRideType, PricingRule
from app.db.models.vendor_tag import VendorTag
from app.db.session import get_db

router = APIRouter()

# ── constants ────────────────────────────────────────────────────────────────

FALLBACK_VALID_TAGS = [
    "food",
    "pubs",
    "sightseeing",
    "shopping",
    "relax",
    "nightlife",
    "sim_card",
    "currency",
]

# Fallback time (hours) to budget for a stop when no avg_time_spent_hours is set.
DEFAULT_TIME_BY_CATEGORY: Dict[str, float] = {
    "restaurant": 1.0,
    "pub": 1.5,
    "hotel": 2.0,
    "sightseeing": 1.0,
}

MAX_ITINERARIES = 6
MAX_STOPS_PER_ITINERARY = 8
DEFAULT_TRAVEL_SPEED_KMPH = 24.0
MAX_STOP_DWELL_HOURS = 4.0
MULTI_VISIT_TAGS = {"fun_zone", "explore_places"}
FOOD_BUCKET_TAGS = {"food", "restaurant", "cafe", "dining"}
NIGHTLIFE_BUCKET_TAGS = {"pub", "bar", "nightlife"}
SPA_BUCKET_TAGS = {"relax", "spa", "wellness"}
CURRENCY_BUCKET_TAGS = {"currency", "currency_exchange", "forex"}
SIM_BUCKET_TAGS = {"sim_card", "sim"}
REQUEST_TAG_CATEGORY_HINTS: Dict[str, set[str]] = {
    "food": {"restaurant", "cafe"},
    "bar": {"pub", "bar", "nightlife"},
    "explore_places": {"sightseeing", "attraction", "landmark", "beach", "park"},
    "fun_zone": {"pub", "bar", "nightlife", "entertainment", "activity"},
}
REQUEST_TAG_TEXT_HINTS: Dict[str, set[str]] = {
    "food": {"food", "restaurant", "cafe", "dining", "meal", "buffet"},
    "bar": {"bar", "pub", "brew", "brewery", "lounge", "cocktail", "nightlife"},
    "fun_zone": {
        "fun", "pub", "bar", "brew", "brewery", "lounge", "club", "nightlife",
        "sports bar", "arcade", "music", "party", "hangout", "drinks",
    },
    "explore_places": {"sightseeing", "beach", "park", "museum", "view", "landmark", "attraction"},
}


# ── schemas ──────────────────────────────────────────────────────────────────

class ItinerarySuggestIn(BaseModel):
    port_id: Optional[int] = None
    port: Optional[str] = None          # name or code – alternative to port_id
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
    price_per_person: Optional[float]
    address: Optional[str]
    phone: Optional[str]
    image_url: Optional[str]
    timings: Optional[str]
    lat: Optional[float]
    lng: Optional[float]
    description: Optional[str]
    travel_minutes_from_prev: Optional[float] = None
    travel_minutes_to_port: Optional[float] = None


class ItineraryOption(BaseModel):
    itinerary_number: int
    label: str
    total_hours: float
    total_distance_km: float
    estimated_travel_minutes: float
    total_stops: int
    highlights: List[str]         # tag names covered by this itinerary
    stops: List[ItineraryStop]


class ItinerarySuggestOut(BaseModel):
    port_id: Optional[int]
    port_name: Optional[str]
    requested_hours: float
    requested_tags: List[str]
    itineraries: List[ItineraryOption]
    fallback_used: bool   # True when we fell back to untagged/default fills


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


# ── helpers ──────────────────────────────────────────────────────────────────

def _resolve_port(db: Session, port_id: Optional[int], port: Optional[str]) -> Optional[Port]:
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


def _get_active_tag_slugs(db: Session) -> List[str]:
    try:
        VendorTag.__table__.create(bind=db.get_bind(), checkfirst=True)
        tags = (
            db.query(VendorTag)
            .filter(VendorTag.is_active.is_(True))
            .order_by(VendorTag.sort_order.asc(), VendorTag.name.asc())
            .all()
        )
        values = [str(item.slug or "").strip().lower() for item in tags if item.slug]
        return [item for item in values if item]
    except Exception:
        return FALLBACK_VALID_TAGS


def _vendor_to_stop(v: Vendors) -> ItineraryStop:
    other = v.other_information or {}
    raw_tags = other.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
    raw_tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]

    avg_time = other.get("avg_time_spent_hours")
    if avg_time is None:
        avg_time = DEFAULT_TIME_BY_CATEGORY.get(str(v.category or "").strip().lower(), 1.0)
    else:
        try:
            avg_time = float(avg_time)
        except (TypeError, ValueError):
            avg_time = DEFAULT_TIME_BY_CATEGORY.get(str(v.category or "").strip().lower(), 1.0)

    price = other.get("price_per_person")
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None

    image_url = None
    if v.images and len(v.images) > 0:
        image_url = v.images[0]

    try:
        distance_from_port = float(v.distance_from_port or 0.0)
    except (TypeError, ValueError):
        distance_from_port = 0.0

    return ItineraryStop(
        vendor_id=v.id,
        name=v.name,
        category=str(v.category or ""),
        tags=raw_tags,
        avg_time_hours=max(0.25, float(avg_time or 1.0)),
        distance_from_port=max(0.0, distance_from_port),
        rating=v.rating or 0.0,
        price_per_person=price,
        address=v.location_name,
        phone=v.phone,
        image_url=image_url,
        timings=other.get("timings"),
        lat=v.lat,
        lng=v.lng,
        description=other.get("about") or other.get("description"),
        travel_minutes_from_prev=None,
        travel_minutes_to_port=None,
    )


def _normalize_tag(value: str) -> str:
    tag = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in tag:
        tag = tag.replace("__", "_")
    # Canonical aliases used by superadmin tag slugs.
    if tag in {"funzone"}:
        tag = "fun_zone"
    if tag in {"exploreplaces", "explore_place"}:
        tag = "explore_places"
    # Keep canonical plural slugs intact.
    if tag in {"fun_zone", "explore_places"}:
        return tag
    # Handle common singular/plural variance (restaurant/restaurants, pub/pubs).
    if len(tag) > 3 and tag.endswith("s") and not tag.endswith("ss"):
        tag = tag[:-1]
    return tag


def _normalize_requested_tags(raw_tags: List[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for tag in raw_tags:
        n = _normalize_tag(tag)
        if not n or n in seen:
            continue
        seen.add(n)
        normalized.append(n)
    return normalized


def _tags_overlap(vendor_tags: List[str], requested_tags: List[str]) -> bool:
    vt = {_normalize_tag(t) for t in vendor_tags}
    rt = {_normalize_tag(t) for t in requested_tags}
    return bool(vt & rt)


def _tag_overlap_score(vendor_tags: List[str], requested_tags: List[str]) -> int:
    vt = {_normalize_tag(t) for t in vendor_tags}
    rt = {_normalize_tag(t) for t in requested_tags}
    return len(vt & rt)


def _matches_requested_tag(stop: ItineraryStop, requested_tag: str) -> bool:
    requested = _normalize_tag(requested_tag)
    if not requested:
        return False
    stop_tags = {_normalize_tag(tag) for tag in (stop.tags or []) if tag}
    if requested in stop_tags:
        return True

    category = _normalize_tag(stop.category or "")
    hinted_categories = REQUEST_TAG_CATEGORY_HINTS.get(requested, set())
    if category and category in hinted_categories:
        return True

    # Data in some ports may store nightlife venues under generic categories
    # (for example "restaurant") while descriptions still contain strong clues.
    text_hints = REQUEST_TAG_TEXT_HINTS.get(requested, set())
    if text_hints:
        text_parts = [
            stop.name or "",
            stop.category or "",
            stop.address or "",
            stop.description or "",
            " ".join(stop.tags or []),
        ]
        searchable = " ".join(text_parts).lower().replace("_", " ").replace("-", " ")
        for hint in text_hints:
            if hint and hint in searchable:
                return True
    return False


def _matched_requested_tags_for_stop(stop: ItineraryStop, requested_tags: List[str]) -> List[str]:
    return [tag for tag in requested_tags if _matches_requested_tag(stop, tag)]


def _project_stop_tags_for_response(stop: ItineraryStop, requested_tags: List[str]) -> ItineraryStop:
    projected = (stop.model_copy(deep=True) if hasattr(stop, "model_copy") else stop.copy(deep=True))
    projected.tags = _matched_requested_tags_for_stop(stop, requested_tags)
    return projected


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    a = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def _safe_port_distance_km(stop: ItineraryStop, fallback_km: float) -> float:
    value = float(stop.distance_from_port or 0.0)
    if value > 0:
        return value
    return fallback_km


def _leg_distance_km(a: ItineraryStop, b: ItineraryStop, fallback_km: float) -> float:
    if a.lat is not None and a.lng is not None and b.lat is not None and b.lng is not None:
        direct = _haversine_km(float(a.lat), float(a.lng), float(b.lat), float(b.lng))
        if direct > 0:
            return direct
    a_port = _safe_port_distance_km(a, fallback_km)
    b_port = _safe_port_distance_km(b, fallback_km)
    # Approximate inter-stop road leg when geocoordinates are unavailable.
    return max(0.5, abs(a_port - b_port) + (min(a_port, b_port) * 0.35))


def _route_distance_km(stops: List[ItineraryStop], fallback_km: float) -> float:
    if not stops:
        return 0.0
    total = _safe_port_distance_km(stops[0], fallback_km)
    for idx in range(1, len(stops)):
        total += _leg_distance_km(stops[idx - 1], stops[idx], fallback_km)
    total += _safe_port_distance_km(stops[-1], fallback_km)
    return total


def _stop_dedupe_key(stop: ItineraryStop) -> tuple:
    name_key = (stop.name or "").strip().lower()
    addr_key = (stop.address or "").strip().lower()
    if name_key or addr_key:
        return ("name_addr", name_key, addr_key)
    if stop.vendor_id:
        return ("id", int(stop.vendor_id))
    return ("raw", id(stop))


def _dedupe_stops(stops: List[ItineraryStop]) -> List[ItineraryStop]:
    seen: set[tuple] = set()
    result: List[ItineraryStop] = []
    for stop in stops:
        key = _stop_dedupe_key(stop)
        if key in seen:
            continue
        seen.add(key)
        result.append(stop)
    return result


def _stretch_stop_durations(
    stops: List[ItineraryStop],
    target_hours: float,
    travel_hours: float,
) -> List[ItineraryStop]:
    if not stops:
        return stops
    # Keep single-stop plans realistic; do not inflate one venue to fill hours.
    if len(stops) < 2:
        return stops

    current_dwell = sum(float(s.avg_time_hours or 0.0) for s in stops)
    target_dwell = max(current_dwell, target_hours - max(0.0, travel_hours))
    total_capacity = sum(max(0.0, MAX_STOP_DWELL_HOURS - float(s.avg_time_hours or 0.0)) for s in stops)
    slack = min(max(0.0, target_dwell - current_dwell), total_capacity)
    if slack <= 0:
        return stops

    grown: List[ItineraryStop] = [
        (s.model_copy(deep=True) if hasattr(s, "model_copy") else s.copy(deep=True))
        for s in stops
    ]
    remaining = slack
    while remaining > 0.001:
        growable = [
            idx
            for idx, stop in enumerate(grown)
            if float(stop.avg_time_hours or 0.0) < MAX_STOP_DWELL_HOURS - 0.001
        ]
        if not growable:
            break
        add_each = remaining / len(growable)
        progressed = False
        for idx in growable:
            current = float(grown[idx].avg_time_hours or 0.0)
            cap = max(0.0, MAX_STOP_DWELL_HOURS - current)
            add = min(cap, add_each)
            if add <= 0:
                continue
            grown[idx].avg_time_hours = round(current + add, 2)
            remaining -= add
            progressed = True
        if not progressed:
            break
    return grown


def _annotate_leg_travel_minutes(
    stops: List[ItineraryStop],
    fallback_port_distance_km: float,
    speed_kmph: float,
) -> tuple[List[ItineraryStop], float, float]:
    if not stops:
        return [], 0.0, 0.0

    effective_speed = max(5.0, speed_kmph)
    annotated: List[ItineraryStop] = [
        (s.model_copy(deep=True) if hasattr(s, "model_copy") else s.copy(deep=True))
        for s in stops
    ]

    total_km = 0.0
    total_travel_minutes = 0.0

    first_leg_km = _safe_port_distance_km(annotated[0], fallback_port_distance_km)
    first_leg_minutes = max(3.0, (first_leg_km / effective_speed) * 60.0)
    annotated[0].travel_minutes_from_prev = round(first_leg_minutes, 1)
    total_km += first_leg_km
    total_travel_minutes += first_leg_minutes

    for idx in range(1, len(annotated)):
        leg_km = _leg_distance_km(annotated[idx - 1], annotated[idx], fallback_port_distance_km)
        leg_minutes = max(3.0, (leg_km / effective_speed) * 60.0)
        annotated[idx].travel_minutes_from_prev = round(leg_minutes, 1)
        total_km += leg_km
        total_travel_minutes += leg_minutes

    return_km = _safe_port_distance_km(annotated[-1], fallback_port_distance_km)
    return_minutes = max(3.0, (return_km / effective_speed) * 60.0)
    annotated[-1].travel_minutes_to_port = round(return_minutes, 1)
    total_km += return_km
    total_travel_minutes += return_minutes
    return annotated, total_km, total_travel_minutes


def _trim_stop_durations_to_budget(
    stops: List[ItineraryStop],
    target_hours: float,
    travel_minutes: float,
) -> List[ItineraryStop]:
    if not stops:
        return stops

    budget_minutes = max(0.0, target_hours * 60.0)
    dwell_minutes = sum(float(stop.avg_time_hours or 0.0) * 60.0 for stop in stops)
    overflow = (dwell_minutes + travel_minutes) - budget_minutes
    if overflow <= 0.1:
        return stops

    trimmed: List[ItineraryStop] = [
        (s.model_copy(deep=True) if hasattr(s, "model_copy") else s.copy(deep=True))
        for s in stops
    ]

    while overflow > 0.1:
        adjustable = [
            idx
            for idx, stop in enumerate(trimmed)
            if float(stop.avg_time_hours or 0.0) * 60.0 > 15.0
        ]
        if not adjustable:
            break
        reduce_each = overflow / len(adjustable)
        progressed = False
        for idx in adjustable:
            current_minutes = float(trimmed[idx].avg_time_hours or 0.0) * 60.0
            reducible = max(0.0, current_minutes - 15.0)
            reduction = min(reducible, reduce_each)
            if reduction <= 0:
                continue
            trimmed[idx].avg_time_hours = round((current_minutes - reduction) / 60.0, 2)
            overflow -= reduction
            progressed = True
        if not progressed:
            break

    return trimmed


def _max_repeats_for_category(hours_budget: float, category: str, tags: Optional[List[str]] = None) -> int:
    # Scale repeats with how much time is available, with a sane floor/ceiling.
    if hours_budget <= 4:
        return 1
    elif hours_budget <= 8:
        return 2
    else:
        return 3


def _repeat_bucket_key_for_stop(stop, requested_tags):
    requested = [_normalize_tag(tag) for tag in (requested_tags or []) if tag]
    for requested_tag in requested:
        if _matches_requested_tag(stop, requested_tag):
            return f"tag:{requested_tag}:{_category_bucket_key(stop.category, None)}"
    return f"category:{_category_bucket_key(stop.category, None)}"

def _max_repeats_for_bucket(bucket_key: str, hours_budget: float, stop: ItineraryStop) -> int:
    if bucket_key.startswith("tag:"):
        tag = bucket_key.split(":", 1)[1]
        if tag in MULTI_VISIT_TAGS:
            return MAX_STOPS_PER_ITINERARY
    return _max_repeats_for_category(hours_budget, stop.category, stop.tags)


def _category_bucket_key(category: str, tags: Optional[List[str]] = None) -> str:
    normalized_category = _normalize_tag(category or "")
    normalized_tags = {_normalize_tag(tag) for tag in (tags or []) if tag}
    # Prioritize primary category to avoid multi-tag rows collapsing everything
    # into one bucket (for example, a sightseeing place tagged with "food").
    if normalized_category in FOOD_BUCKET_TAGS:
        return "bucket_food"
    if normalized_category in NIGHTLIFE_BUCKET_TAGS:
        return "bucket_nightlife"
    if normalized_category in SPA_BUCKET_TAGS:
        return "bucket_spa"
    if normalized_category in CURRENCY_BUCKET_TAGS:
        return "bucket_currency"
    if normalized_category in SIM_BUCKET_TAGS:
        return "bucket_sim"

    # Fallback to tags when category is unknown or generic.
    if normalized_tags & FOOD_BUCKET_TAGS:
        return "bucket_food"
    if normalized_tags & NIGHTLIFE_BUCKET_TAGS:
        return "bucket_nightlife"
    if normalized_tags & SPA_BUCKET_TAGS:
        return "bucket_spa"
    if normalized_tags & CURRENCY_BUCKET_TAGS:
        return "bucket_currency"
    if normalized_tags & SIM_BUCKET_TAGS:
        return "bucket_sim"

    return normalized_category or "bucket_misc"


import statistics

def _resolve_package_constraints(
    db: Session,
    port_id: Optional[int],
    requested_hours: float,
) -> tuple[Optional[float], float]:
    if not port_id:
        return None, DEFAULT_TRAVEL_SPEED_KMPH

    ride_type = (
        db.query(PricingRideType)
        .filter(PricingRideType.code == "package_trip")
        .first()
    )
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

    requested_minutes = int(round(requested_hours * 60))
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

    km_limits = [float(rule.included_km) for rule in rules if rule.included_km is not None and rule.included_km > 0]

    km_cap = None
    if km_limits:
        median_km = statistics.median(km_limits)
        # Drop outlier rules (e.g. stray/bad data rows) that sit far below
        # the rest of the pool instead of letting one bad row crush the cap.
        filtered_km_limits = [k for k in km_limits if k >= median_km * 0.5]
        km_cap = min(filtered_km_limits) if filtered_km_limits else median_km

    speed_candidates: List[float] = []
    for rule in rules:
        metadata = rule.pricing_metadata or {}
        if not isinstance(metadata, dict):
            continue
        for key in ("speed_kmph", "avg_speed_kmph", "assumed_speed_kmph"):
            value = metadata.get(key)
            try:
                parsed = float(value)
                if parsed > 0:
                    speed_candidates.append(parsed)
            except (TypeError, ValueError):
                continue
    speed_kmph = min(speed_candidates) if speed_candidates else DEFAULT_TRAVEL_SPEED_KMPH
    return km_cap, speed_kmph

@router.get("/tags", response_model=List[ItineraryTagOut])
def list_itinerary_tags(db: Session = Depends(get_db)):
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
    resolved_port = _resolve_port(db, port_id, port)
    requested_tags = {
        item.strip().lower()
        for item in (tags or "").split(",")
        if item.strip()
    }

    query = db.query(Vendors).filter(Vendors.status == "Active")
    if resolved_port:
        query = query.filter(Vendors.port_id == resolved_port.id)

    vendors = query.order_by(Vendors.rating.desc()).all()
    stops = [_vendor_to_stop(item) for item in vendors]
    if requested_tags:
        stops = [item for item in stops if _tags_overlap(item.tags, list(requested_tags))]

    return ItineraryCatalogOut(
        port_id=resolved_port.id if resolved_port else None,
        port_name=resolved_port.name if resolved_port else None,
        total=len(stops),
        vendors=stops,
    )


import math
import statistics
from typing import List, Optional, Dict

MULTI_VISIT_TAGS = {"fun_zone", "explore_places"}
MAX_STOPS_PER_ITINERARY = 8
MAX_ITINERARIES = 6
DEFAULT_TRAVEL_SPEED_KMPH = 20.0
DEFAULT_PORT_DISTANCE_KM = 3.0


# ── Tag handling ────────────────────────────────────────────────────────────

def itn_clean_tag(tag: str) -> str:
    return tag.strip().lower().replace(" ", "_").replace("-", "_")


def itn_vendor_tag_set(stop) -> set:
    """Exact set of tags a vendor actually has. No category fallback."""
    return {itn_clean_tag(t) for t in (stop.tags or []) if t and str(t).strip()}


def itn_real_matches(stop, requested_tags: List[str]) -> List[str]:
    """Strict match: a tag counts only if it's literally on the vendor."""
    tag_set = itn_vendor_tag_set(stop)
    return [t for t in requested_tags if t in tag_set]


# ── Distance / route helpers ─────────────────────────────────────────────────

def itn_haversine_km(lat1, lng1, lat2, lng2) -> float:
    if None in (lat1, lng1, lat2, lng2):
        return 0.0
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def itn_route_km(stops: List, fallback_leg_km: float) -> float:
    """Port -> stop1 -> stop2 -> ... -> back to port."""
    if not stops:
        return 0.0
    total = 0.0
    prev_lat, prev_lng = None, None
    for s in stops:
        if prev_lat is not None and s.lat is not None and s.lng is not None:
            leg = itn_haversine_km(prev_lat, prev_lng, s.lat, s.lng)
            total += leg if leg > 0 else fallback_leg_km
        else:
            total += s.distance_from_port if s.distance_from_port else fallback_leg_km
        prev_lat, prev_lng = s.lat, s.lng
    last = stops[-1]
    total += last.distance_from_port if last.distance_from_port else fallback_leg_km
    return total


# ── Repeat-cap bucketing ──────────────────────────────────────────────────────

def itn_stop_bucket(stop, requested_tags: List[str], hours_budget: float) -> tuple[str, int, List[str]]:
    """
    Returns (bucket_key, max_repeats_for_that_bucket, matched_requested_tags).
    - fun_zone / explore_places: unlimited repeats (up to MAX_STOPS_PER_ITINERARY)
    - any other matched tag: max 1 occurrence in the itinerary
    - no tag match at all: filler bucket by category, capped modestly
    """
    matched = itn_real_matches(stop, requested_tags)

    if matched:
        multi_visit_hit = next((t for t in matched if t in MULTI_VISIT_TAGS), None)
        primary = multi_visit_hit or matched[0]
        cap = MAX_STOPS_PER_ITINERARY if primary in MULTI_VISIT_TAGS else 1
        return f"tag:{primary}", cap, matched

    category = (stop.category or "uncategorized").strip().lower()
    cap = 1 if hours_budget <= 4 else (2 if hours_budget <= 8 else 3)
    return f"category:{category}", cap, []


# ── Vendor -> stop projection ─────────────────────────────────────────────────

def itn_vendor_to_stop(v) -> ItineraryStop:
    info = v.other_information or {}
    if not isinstance(info, dict):
        info = {}

    raw_tags = info.get("tags") or []
    if not isinstance(raw_tags, list):
        raw_tags = []

    images = v.images or {}
    if isinstance(images, dict):
        image_url = images.get("primary") or images.get("url") or None
    elif isinstance(images, list) and images:
        first = images[0]
        image_url = first.get("url") if isinstance(first, dict) else first
    else:
        image_url = None

    return ItineraryStop(
        vendor_id=v.id,
        name=v.name,
        category=v.category,
        tags=[itn_clean_tag(t) for t in raw_tags if t and str(t).strip()],
        avg_time_hours=float(info.get("avg_time_hours") or info.get("duration_hours") or 1.0),
        distance_from_port=v.distance_from_port,
        rating=v.rating or 0.0,
        price_per_person=info.get("price_per_person") or info.get("price"),
        address=v.location_name,
        phone=v.phone,
        image_url=image_url,
        timings=info.get("timings"),
        lat=v.lat,
        lng=v.lng,
        description=info.get("description"),
    )
def itn_unique_stops(stops: List[ItineraryStop]) -> List[ItineraryStop]:
    seen = set()
    result = []
    for s in stops:
        if s.vendor_id in seen:
            continue
        seen.add(s.vendor_id)
        result.append(s)
    return result


# ── Port / pricing resolution ─────────────────────────────────────────────────

def itn_find_port(db: Session, port_id: Optional[int], port_code: Optional[str]):
    if port_id:
        port = db.query(Port).filter(Port.id == port_id).first()
        if port:
            return port
    if port_code:
        port = db.query(Port).filter(Port.code == port_code).first()
        if port:
            return port
    return None


def itn_resolve_distance_speed(db: Session, port_id: Optional[int], requested_hours: float) -> tuple[Optional[float], float]:
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

    requested_minutes = int(round(requested_hours * 60))
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

    km_limits = [float(r.included_km) for r in rules if r.included_km and r.included_km > 0]
    km_cap = None
    if km_limits:
        median_km = statistics.median(km_limits)
        filtered = [k for k in km_limits if k >= median_km * 0.5]
        km_cap = min(filtered) if filtered else median_km

    speeds = []
    for r in rules:
        meta = r.pricing_metadata or {}
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


# ── Itinerary builder ──────────────────────────────────────────────────────────

def itn_label_for(stops: List[ItineraryStop], number: int) -> str:
    if not stops:
        return f"Option {number}"
    if len(stops) == 1:
        return f"Option {number}: {stops[0].name}"
    return f"Option {number}: {stops[0].name} + {len(stops) - 1} more"


def _itn_stop_dwell_bounds_minutes(stop: ItineraryStop) -> tuple[float, float]:
    category = itn_clean_tag(stop.category or "")
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


def _itn_stop_weight(stop: ItineraryStop, requested_tags: List[str]) -> float:
    matched_count = len(itn_real_matches(stop, requested_tags))
    rating = max(0.0, float(stop.rating or 0.0))
    distance = max(0.0, float(stop.distance_from_port or 0.0))
    distance_factor = 1.0 / (1.0 + (distance / 25.0))
    return max(0.1, (1.0 + (0.9 * matched_count) + (0.12 * rating)) * distance_factor)


def _itn_apply_dynamic_dwell(
    stops: List[ItineraryStop],
    hours_budget: float,
    travel_minutes: float,
    requested_tags: List[str],
) -> List[ItineraryStop]:
    if not stops:
        return stops

    tuned: List[ItineraryStop] = [
        (s.model_copy(deep=True) if hasattr(s, "model_copy") else s.copy(deep=True))
        for s in stops
    ]

    budget_minutes = max(0.0, hours_budget * 60.0)
    reserve_minutes = max(10.0, min(30.0, budget_minutes * 0.05))
    target_dwell = max(0.0, budget_minutes - max(0.0, travel_minutes) - reserve_minutes)

    mins: List[float] = []
    maxs: List[float] = []
    weights: List[float] = []
    for stop in tuned:
        min_m, max_m = _itn_stop_dwell_bounds_minutes(stop)
        mins.append(min_m)
        maxs.append(max(max_m, min_m))
        weights.append(_itn_stop_weight(stop, requested_tags))

    floor_sum = sum(mins)
    if target_dwell <= 0.0:
        target_dwell = floor_sum

    # If budget is too tight for category mins, scale down proportionally
    # with a hard floor to keep stops meaningful.
    if target_dwell < floor_sum:
        ratio = target_dwell / floor_sum if floor_sum > 0 else 0.0
        dwell = [max(15.0, m * ratio) for m in mins]
        current = sum(dwell)
        if current > target_dwell and current > 0:
            shrink = target_dwell / current
            dwell = [max(15.0, d * shrink) for d in dwell]
    else:
        dwell = mins[:]
        extra = min(target_dwell - floor_sum, sum(maxs[idx] - mins[idx] for idx in range(len(tuned))))
        while extra > 0.01:
            growable = [idx for idx in range(len(tuned)) if dwell[idx] < maxs[idx] - 0.01]
            if not growable:
                break
            weight_sum = sum(weights[idx] for idx in growable) or float(len(growable))
            progressed = False
            for idx in growable:
                share = extra * (weights[idx] / weight_sum)
                cap = maxs[idx] - dwell[idx]
                add = min(cap, share)
                if add <= 0:
                    continue
                dwell[idx] += add
                extra -= add
                progressed = True
            if not progressed:
                break

    for idx, stop in enumerate(tuned):
        stop.avg_time_hours = round(max(0.25, dwell[idx] / 60.0), 2)

    # Final guardrail after rounding.
    return _trim_stop_durations_to_budget(tuned, hours_budget, travel_minutes)

def itn_pack_itinerary(
    candidates: List[ItineraryStop],
    hours_budget: float,
    km_budget: Optional[float],
    speed_kmph: float,
    fallback_leg_km: float,
    requested_tags: List[str],
    seed_offset: int,
) -> Optional[tuple[List[ItineraryStop], float, float]]:

    effective_speed = max(5.0, speed_kmph)
    
    best_combination = None
    best_score = -1  # Score = total_hours * 10 + num_stops
    
    # Try starting from each candidate
    for start_idx in range(min(len(candidates), 15)):  # More starting points
        current_stops = []
        used_ids = set()
        tag_counts = {}
        
        ordered = candidates[start_idx:] + candidates[:start_idx]
        
        # First pass: Add as many stops as possible
        for stop in ordered:
            if len(current_stops) >= MAX_STOPS_PER_ITINERARY:
                break
            
            vendor_id = int(stop.vendor_id)
            if vendor_id in used_ids:
                continue
            
            # Check tag constraints
            matched = itn_real_matches(stop, requested_tags)
            skip = False
            for tag in matched:
                if tag not in MULTI_VISIT_TAGS and tag_counts.get(tag, 0) >= 1:
                    skip = True
                    break
            if skip:
                continue
            
            proposed = current_stops + [stop]
            route_km = itn_route_km(proposed, fallback_leg_km)
            
            if km_budget is not None and route_km > km_budget * 1.1:
                continue
            
            travel_hours = route_km / effective_speed
            total_hours = sum(s.avg_time_hours for s in proposed) + travel_hours
            
            # Allow up to 5% over budget (we'll trim if needed)
            if total_hours <= hours_budget * 1.05:
                current_stops.append(stop)
                used_ids.add(vendor_id)
                for tag in matched:
                    if tag not in MULTI_VISIT_TAGS:
                        tag_counts[tag] = tag_counts.get(tag, 0) + 1
        
        # Second pass: Try to add more stops that were skipped
        if len(current_stops) < MAX_STOPS_PER_ITINERARY:
            for stop in ordered:
                if len(current_stops) >= MAX_STOPS_PER_ITINERARY:
                    break
                
                vendor_id = int(stop.vendor_id)
                if vendor_id in used_ids:
                    continue
                
                proposed = current_stops + [stop]
                route_km = itn_route_km(proposed, fallback_leg_km)
                
                if km_budget is not None and route_km > km_budget * 1.1:
                    continue
                
                travel_hours = route_km / effective_speed
                total_hours = sum(s.avg_time_hours for s in proposed) + travel_hours
                
                if total_hours <= hours_budget * 1.05:
                    current_stops.append(stop)
                    used_ids.add(vendor_id)
                    # Don't worry about tag counts in second pass (filler)
        
        # Third pass: If we have room, try adding any remaining stops
        if len(current_stops) < MAX_STOPS_PER_ITINERARY:
            for stop in ordered:
                if len(current_stops) >= MAX_STOPS_PER_ITINERARY:
                    break
                
                vendor_id = int(stop.vendor_id)
                if vendor_id in used_ids:
                    continue
                
                # Check if adding this stop would exceed budget by too much
                proposed = current_stops + [stop]
                route_km = itn_route_km(proposed, fallback_leg_km)
                
                if km_budget is not None and route_km > km_budget * 1.1:
                    continue
                
                travel_hours = route_km / effective_speed
                total_hours = sum(s.avg_time_hours for s in proposed) + travel_hours
                
                # Allow up to 10% over, we'll trim later
                if total_hours <= hours_budget * 1.1:
                    current_stops.append(stop)
                    used_ids.add(vendor_id)
        
        # If over budget, try removing stops starting from smallest
        if current_stops:
            route_km = itn_route_km(current_stops, fallback_leg_km)
            travel_hours = route_km / effective_speed
            total_hours = sum(s.avg_time_hours for s in current_stops) + travel_hours
            
            # If over budget, remove smallest stops until under
            if total_hours > hours_budget:
                # Sort by duration ascending
                sorted_by_duration = sorted(current_stops, key=lambda s: s.avg_time_hours)
                for stop_to_remove in sorted_by_duration:
                    if len(current_stops) <= 2:  # Keep at least 2 stops
                        break
                    
                    test_stops = [s for s in current_stops if s.vendor_id != stop_to_remove.vendor_id]
                    test_km = itn_route_km(test_stops, fallback_leg_km)
                    test_travel = test_km / effective_speed
                    test_hours = sum(s.avg_time_hours for s in test_stops) + test_travel
                    
                    if test_hours <= hours_budget:
                        current_stops = test_stops
                        total_hours = test_hours
                        route_km = test_km
                        travel_hours = test_travel
                        break
            
            # Score: prefer more stops AND more total hours
            # This ensures we maximize both
            score = total_hours * 10 + len(current_stops) * 5
            
            if total_hours <= hours_budget and score > best_score:
                best_score = score
                best_combination = current_stops.copy()
    
    # If we found something, return it
    if best_combination:
        total_km = itn_route_km(best_combination, fallback_leg_km)
        travel_minutes = (total_km / effective_speed) * 60
        tuned_stops = _itn_apply_dynamic_dwell(
            best_combination,
            hours_budget,
            travel_minutes,
            requested_tags,
        )
        return (tuned_stops, total_km, travel_minutes)
    
    return None


@router.post("/suggest", response_model=ItinerarySuggestOut)
def suggest_itinerary(body: ItinerarySuggestIn, db: Session = Depends(get_db)):
    print("Received itinerary suggestion request:", body)

    requested_tags = list(dict.fromkeys(
        itn_clean_tag(t) for t in body.tags if t and t.strip()
    ))
    if not requested_tags:
        raise HTTPException(status_code=422, detail="No tags provided")

    port = itn_find_port(db, body.port_id, body.port)
    km_cap, speed_kmph = itn_resolve_distance_speed(db, port.id if port else None, body.hours)

    # Get ALL vendors
    vendor_query = db.query(Vendors).filter(Vendors.status == "Active")
    if port:
        vendor_query = vendor_query.filter(Vendors.port_id == port.id)
    vendors = vendor_query.all()

    all_stops = itn_unique_stops([itn_vendor_to_stop(v) for v in vendors])

    # ONLY use vendors that match the requested tags
    tag_matched = [s for s in all_stops if itn_real_matches(s, requested_tags)]
    
    # If no vendors match the tags, return empty
    if not tag_matched:
        return ItinerarySuggestOut(
            port_id=port.id if port else None,
            port_name=port.name if port else None,
            requested_hours=body.hours,
            requested_tags=requested_tags,
            itineraries=[],
            fallback_used=True,
        )
    
    # Sort by duration (longer stops first)
    tag_matched.sort(key=lambda s: s.avg_time_hours, reverse=True)
    
    positive_distances = [float(s.distance_from_port) for s in all_stops if (s.distance_from_port or 0) > 0]
    fallback_leg_km = sum(positive_distances) / len(positive_distances) if positive_distances else DEFAULT_PORT_DISTANCE_KM

    itineraries = []
    used_combos = set()
    used_tag_combinations = set()  # Track tag combinations for more variety
    
    import random
    random.seed(42)
    
    # More aggressive attempts with higher budget utilization
    max_attempts = 2000  # Increased attempts for better coverage
    
    for attempt in range(max_attempts):
        if len(itineraries) >= MAX_ITINERARIES:
            break
        
        # Create a shuffled copy with different seed for more variety
        shuffled = tag_matched.copy()
        random.seed(attempt * 31 + 17)
        random.shuffle(shuffled)
        
        # Different strategies - more variety
        strategy = attempt % 12  # Increased strategies
        
        if strategy == 0:
            # Take a slice with more stops
            slice_size = min(20, len(shuffled))  # Increased slice size
            start_idx = (attempt * 3) % max(1, len(shuffled) - slice_size)
            candidates = shuffled[start_idx:start_idx + slice_size]
            if len(candidates) < 8:
                candidates = shuffled[:20]
        elif strategy == 1:
            # Longest duration - take more stops
            start_idx = attempt % max(1, len(tag_matched) - 15)
            candidates = tag_matched[start_idx:start_idx + 20]
        elif strategy == 2:
            # Random subset with more stops
            candidates = shuffled[:20]
        elif strategy == 3:
            # Highest rated with more stops
            rating_sorted = sorted(tag_matched, key=lambda s: s.rating or 0, reverse=True)
            start_idx = attempt % max(1, len(rating_sorted) - 15)
            candidates = rating_sorted[start_idx:start_idx + 20]
        elif strategy == 4:
            # Take every Nth stop
            step = 1 + (attempt % 2)
            candidates = shuffled[::step][:20]
        elif strategy == 5:
            # Mix of long and short durations
            long_stops = [s for s in tag_matched if s.avg_time_hours >= 1.5][:10]
            short_stops = [s for s in tag_matched if s.avg_time_hours < 1.5][:10]
            candidates = (long_stops + short_stops)[:20]
            random.shuffle(candidates)
        elif strategy == 6:
            # Focus on different tag combinations
            candidates = shuffled[:15]
        elif strategy == 7:
            # Staggered selection
            candidates = [shuffled[i] for i in range(0, min(20, len(shuffled)), 2)]
        elif strategy == 8:
            # Reverse order
            candidates = shuffled[::-1][:20]
        elif strategy == 9:
            # Evenly distributed stops
            step = max(1, len(shuffled) // 20)
            candidates = shuffled[::step][:20]
        elif strategy == 10:
            # Longer than average stops
            avg_duration = sum(s.avg_time_hours for s in shuffled) / len(shuffled)
            long_stops = [s for s in shuffled if s.avg_time_hours >= avg_duration]
            candidates = long_stops[:20]
        else:
            # Rotate and take slice
            rotate_by = attempt % len(tag_matched)
            rotated = tag_matched[rotate_by:] + tag_matched[:rotate_by]
            candidates = rotated[:20]
        
        # Ensure we have enough candidates
        if len(candidates) < 6:
            candidates = tag_matched[:20]
        
        # CRITICAL FIX: Try to fill the budget more aggressively
        # Start from 95% and go up to 105% to allow slightly over budget
        budget_factor = 0.95 + (attempt % 11) * 0.01  # 0.95 to 1.05
        
        # Also try with different strategy for filling budget
        if attempt > max_attempts // 2:
            # In second half, be more aggressive with filling
            budget_factor = 0.98 + (attempt % 8) * 0.01  # 0.98 to 1.05
        
        adjusted_budget = body.hours * budget_factor
        
        result = itn_pack_itinerary(
            candidates=candidates,
            hours_budget=adjusted_budget,
            km_budget=km_cap,
            speed_kmph=speed_kmph,
            fallback_leg_km=fallback_leg_km,
            requested_tags=requested_tags,
            seed_offset=attempt,
        )
        
        if not result:
            continue
        
        stops, total_km, travel_minutes = result
        
        # Check for repeated tags
        if not validate_no_repeated_tags(stops, requested_tags):
            continue
        
        total_h = round(
            sum(s.avg_time_hours for s in stops) + travel_minutes / 60,
            2
        )
        
        # CRITICAL FIX: Higher minimum threshold to ensure better filling
        # Skip if too empty (less than 75% of budget)
        if total_h < body.hours * 0.75:
            continue
            
        # Skip if exceeds budget (allow up to 10% over to get fuller itineraries)
        if total_h > body.hours * 1.10:
            continue
        
        # Create unique key - sort vendor IDs
        combo_key = tuple(sorted(int(s.vendor_id) for s in stops))
        
        # Skip exact duplicates
        if combo_key in used_combos:
            continue
        
        # STRONGER DEDUPLICATION: Check for significant overlap
        is_duplicate = False
        for existing in used_combos:
            existing_set = set(existing)
            current_set = set(combo_key)
            
            # Calculate Jaccard similarity
            intersection = len(existing_set.intersection(current_set))
            union = len(existing_set.union(current_set))
            similarity = intersection / union if union > 0 else 0
            
            # If more than 70% similar, it's a duplicate
            if similarity >= 0.7:
                is_duplicate = True
                break
        
        if is_duplicate:
            continue
        
        # NEW: Check tag combination variety
        tag_combo = tuple(sorted(set(
            t for s in stops
            for t in itn_real_matches(s, requested_tags)
        )))
        
        # If we already have this exact tag combination, skip
        if tag_combo in used_tag_combinations:
            continue
        
        used_combos.add(combo_key)
        used_tag_combinations.add(tag_combo)
        
        highlights = sorted({
            t for s in stops
            for t in itn_real_matches(s, requested_tags)
        })
        
        itineraries.append(
            ItineraryOption(
                itinerary_number=len(itineraries) + 1,
                label=itn_label_for(stops, len(itineraries) + 1),
                total_hours=total_h,
                total_distance_km=round(total_km, 2),
                estimated_travel_minutes=round(travel_minutes, 1),
                total_stops=len(stops),
                highlights=highlights,
                stops=stops,
            )
        )
    
    # NEW: Add a fallback pass that specifically tries to fill the budget
    if len(itineraries) < MAX_ITINERARIES // 2:
        print(f"Only {len(itineraries)} itineraries found, attempting aggressive fill...")
        
        # Try to create itineraries that fill at least 90% of budget
        for attempt in range(500):
            if len(itineraries) >= MAX_ITINERARIES:
                break
            
            # Use a different selection strategy - try to include more stops
            shuffled = tag_matched.copy()
            random.seed(attempt * 137 + 89)
            random.shuffle(shuffled)
            
            # Try with larger candidate sets
            candidates = shuffled[:25]
            
            # Aggressive budget factor - aim to fill 95-105%
            budget_factor = 0.95 + (attempt % 11) * 0.01
            adjusted_budget = body.hours * budget_factor
            
            result = itn_pack_itinerary(
                candidates=candidates,
                hours_budget=adjusted_budget,
                km_budget=km_cap,
                speed_kmph=speed_kmph,
                fallback_leg_km=fallback_leg_km,
                requested_tags=requested_tags,
                seed_offset=attempt + 5000,
            )
            
            if not result:
                continue
            
            stops, total_km, travel_minutes = result
            
            if not validate_no_repeated_tags(stops, requested_tags):
                continue
            
            total_h = round(
                sum(s.avg_time_hours for s in stops) + travel_minutes / 60,
                2
            )
            
            # Higher threshold for this pass - at least 85%
            if total_h < body.hours * 0.85:
                continue
                
            if total_h > body.hours * 1.10:
                continue
            
            combo_key = tuple(sorted(int(s.vendor_id) for s in stops))
            if combo_key in used_combos:
                continue
            
            is_duplicate = False
            for existing in used_combos:
                existing_set = set(existing)
                current_set = set(combo_key)
                intersection = len(existing_set.intersection(current_set))
                union = len(existing_set.union(current_set))
                similarity = intersection / union if union > 0 else 0
                if similarity >= 0.7:
                    is_duplicate = True
                    break
            
            if is_duplicate:
                continue
            
            used_combos.add(combo_key)
            
            highlights = sorted({
                t for s in stops
                for t in itn_real_matches(s, requested_tags)
            })
            
            itineraries.append(
                ItineraryOption(
                    itinerary_number=len(itineraries) + 1,
                    label=itn_label_for(stops, len(itineraries) + 1),
                    total_hours=total_h,
                    total_distance_km=round(total_km, 2),
                    estimated_travel_minutes=round(travel_minutes, 1),
                    total_stops=len(stops),
                    highlights=highlights,
                    stops=stops,
                )
            )
    
    # Sort by how close they are to the requested budget (prefer those that fill more)
    def time_fill_score(itinerary):
        # Penalize itineraries that are too short more heavily
        if itinerary.total_hours < body.hours:
            # Exponential penalty for being too short
            return (body.hours - itinerary.total_hours) ** 2
        else:
            # Small penalty for going over
            return (itinerary.total_hours - body.hours) * 1.5
    
    itineraries.sort(key=time_fill_score)
    
    # Final filtering for variety
    final_itineraries = []
    used_vendor_sets = set()
    used_tag_sets = set()
    
    # Select diverse itineraries, prioritizing those that fill the budget
    for itin in itineraries:
        if len(final_itineraries) >= MAX_ITINERARIES:
            break
        
        vendor_set = frozenset(int(s.vendor_id) for s in itin.stops)
        tag_set = frozenset(itin.highlights)
        
        # Check vendor overlap
        is_duplicate = False
        for existing_set in used_vendor_sets:
            intersection = len(existing_set.intersection(vendor_set))
            union = len(existing_set.union(vendor_set))
            similarity = intersection / union if union > 0 else 0
            if similarity >= 0.6:
                is_duplicate = True
                break
        
        if is_duplicate:
            continue
        
        # Check tag combination overlap
        for existing_tag_set in used_tag_sets:
            intersection = len(existing_tag_set.intersection(tag_set))
            union = len(existing_tag_set.union(tag_set))
            similarity = intersection / union if union > 0 else 0
            if similarity >= 0.8:
                is_duplicate = True
                break
        
        if is_duplicate:
            continue
        
        used_vendor_sets.add(vendor_set)
        used_tag_sets.add(tag_set)
        final_itineraries.append(itin)
    
    # Reassign itinerary numbers
    for idx, itin in enumerate(final_itineraries):
        itin.itinerary_number = idx + 1
        itin.label = itn_label_for(itin.stops, idx + 1)

    return ItinerarySuggestOut(
        port_id=port.id if port else None,
        port_name=port.name if port else None,
        requested_hours=body.hours,
        requested_tags=requested_tags,
        itineraries=final_itineraries,
        fallback_used=len(tag_matched) < 2,
    )


def validate_no_repeated_tags(stops, requested_tags):
    """
    Validate that no tags are repeated across stops in an itinerary,
    except for 'explore_places' and 'fun_zone' which can appear multiple times.
    """
    ALLOWED_REPEATED_TAGS = {'explore_places', 'fun_zone'}
    
    all_tags = []
    for stop in stops:
        stop_tags = itn_real_matches(stop, requested_tags)
        all_tags.extend(stop_tags)
    
    tag_counts = {}
    for tag in all_tags:
        tag_counts[tag] = tag_counts.get(tag, 0) + 1
    
    for tag, count in tag_counts.items():
        if count > 1 and tag not in ALLOWED_REPEATED_TAGS:
            return False
    
    return True

