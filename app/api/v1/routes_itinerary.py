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
    if tag in {"funzone"}:
        tag = "fun_zone"
    if tag in {"exploreplaces", "explore_place"}:
        tag = "explore_places"
    # Handle common singular/plural variance (restaurant/restaurants, pub/pubs)
    if len(tag) > 3 and tag.endswith("s"):
        tag = tag[:-1]
    return tag


def _tags_overlap(vendor_tags: List[str], requested_tags: List[str]) -> bool:
    vt = {_normalize_tag(t) for t in vendor_tags}
    rt = {_normalize_tag(t) for t in requested_tags}
    return bool(vt & rt)


def _tag_overlap_score(vendor_tags: List[str], requested_tags: List[str]) -> int:
    vt = {_normalize_tag(t) for t in vendor_tags}
    rt = {_normalize_tag(t) for t in requested_tags}
    return len(vt & rt)


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
    _ = hours_budget
    normalized = _normalize_tag((category or "").strip().lower())
    if normalized in MULTI_VISIT_TAGS:
        return MAX_STOPS_PER_ITINERARY
    if tags:
        normalized_tags = {_normalize_tag(tag) for tag in tags if tag}
        if normalized_tags & MULTI_VISIT_TAGS:
            return MAX_STOPS_PER_ITINERARY
    return 1


def _repeat_bucket_key_for_stop(stop: ItineraryStop, requested_tags: Optional[List[str]]) -> str:
    requested = {_normalize_tag(tag) for tag in (requested_tags or []) if tag}
    stop_tags = [_normalize_tag(tag) for tag in (stop.tags or []) if tag]
    matched_tags = sorted({tag for tag in stop_tags if tag in requested})
    if matched_tags:
        return f"tag:{matched_tags[0]}"
    # If no requested tag matched, fall back to normalized category bucket.
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
    km_cap = min(km_limits) if km_limits else None

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


def _build_itinerary(
    candidates: List[ItineraryStop],
    hours_budget: float,
    km_budget: Optional[float],
    speed_kmph: float,
    fallback_port_distance_km: float,
    requested_tags: Optional[List[str]] = None,
    seed_offset: int = 0,
) -> Optional[tuple[List[ItineraryStop], float, float]]:
    """
    Greedy pack: start from seed_offset in the (already-sorted) candidate list,
    wrap around, pick stops until the budget is consumed or list exhausted.
    Returns None if nothing fits.
    """
    stops: List[ItineraryStop] = []
    dwell_hours = 0.0
    n = len(candidates)
    seen_ids: set[int] = set()
    bucket_counts: Dict[str, int] = {}
    effective_speed = max(5.0, speed_kmph)

    for i in range(n):
        idx = (i + seed_offset) % n
        stop = candidates[idx]
        if stop.vendor_id in seen_ids:
            continue
        bucket_key = _repeat_bucket_key_for_stop(stop, requested_tags)
        if bucket_counts.get(bucket_key, 0) >= _max_repeats_for_bucket(bucket_key, hours_budget, stop):
            continue

        proposed_stops = stops + [stop]
        route_km = _route_distance_km(proposed_stops, fallback_port_distance_km)
        travel_hours = route_km / effective_speed
        proposed_total_hours = dwell_hours + stop.avg_time_hours + travel_hours

        if km_budget is not None and route_km > (km_budget + 0.1):
            continue
        if proposed_total_hours <= hours_budget + 0.05:
            stops = proposed_stops
            dwell_hours += stop.avg_time_hours
            seen_ids.add(stop.vendor_id)
            bucket_counts[bucket_key] = bucket_counts.get(bucket_key, 0) + 1
        if len(stops) >= MAX_STOPS_PER_ITINERARY:
            break

    if not stops:
        return None
    total_km = _route_distance_km(stops, fallback_port_distance_km)
    travel_minutes = (total_km / effective_speed) * 60.0
    stretched = _stretch_stop_durations(stops, hours_budget, travel_minutes / 60.0)
    annotated, total_km, travel_minutes = _annotate_leg_travel_minutes(
        stretched,
        fallback_port_distance_km,
        effective_speed,
    )
    trimmed = _trim_stop_durations_to_budget(annotated, hours_budget, travel_minutes)
    return trimmed, total_km, travel_minutes


def _itinerary_label(stops: List[ItineraryStop], idx: int) -> str:
    cats = list({s.category for s in stops})
    if len(cats) == 1:
        return f"Option {idx}: Pure {cats[0].title()} Experience"
    if len(cats) == 2:
        return f"Option {idx}: {cats[0].title()} & {cats[1].title()} Day"
    return f"Option {idx}: Mixed Shore Excursion"


# ── endpoint ─────────────────────────────────────────────────────────────────

@router.post("/suggest", response_model=ItinerarySuggestOut)
def suggest_itinerary(
    body: ItinerarySuggestIn,
    db: Session = Depends(get_db),
):
    """
    Suggest up to 6 personalised shore-leave itineraries based on:
    - how many hours the user has
    - which experience tags they want (food, pubs, sightseeing, shopping,
      relax, nightlife, sim_card, currency)
    - which port they are at

    Tags not in the allowed list are silently ignored.
    If port is not supplied or not found, results are returned without
    port filtering (all-port pool).
    """

    print("Received itinerary suggestion request:", body)
    # Accept user-provided tags as-is and normalize at overlap checks so
    # singular/plural naming differences don't collapse suggestions.
    requested_tags = [t.strip().lower() for t in body.tags if t.strip()]
    if not requested_tags:
        raise HTTPException(
            status_code=422,
            detail="No tags provided",
        )

    resolved_port = _resolve_port(db, body.port_id, body.port)
    km_cap, configured_speed_kmph = _resolve_package_constraints(
        db,
        resolved_port.id if resolved_port else None,
        body.hours,
    )

    # ── 1. Query vendors for this port ───────────────────────────────────────
    query = db.query(Vendors).filter(
        Vendors.status == "Active",
    )
    if resolved_port:
        query = query.filter(Vendors.port_id == resolved_port.id)

    vendors = query.order_by(Vendors.rating.desc()).all()

    # ── 3. Convert to stops and split into tag-matched vs fallback ────────────
    all_stops: List[ItineraryStop] = _dedupe_stops([_vendor_to_stop(v) for v in vendors])
    tag_matched   = [s for s in all_stops if _tags_overlap(s.tags, requested_tags)]
    untagged_fill = [s for s in all_stops if s not in tag_matched]

    fallback_used = False

    # If fewer than 2 tag-matched stops, absorb untagged as fallback
    if len(tag_matched) < 2:
        fallback_used = True
        candidates = all_stops  # use everything
    else:
        candidates = tag_matched

    candidates = _dedupe_stops(candidates)

    # Prefer vendors that match more requested tags, then rating.
    candidates.sort(
        key=lambda s: (_tag_overlap_score(s.tags, requested_tags), s.rating),
        reverse=True,
    )

    positive_distances = [float(item.distance_from_port) for item in all_stops if (item.distance_from_port or 0) > 0]
    fallback_port_distance_km = sum(positive_distances) / len(positive_distances) if positive_distances else 3.0

    # ── 4. Build up to MAX_ITINERARIES by rotating the start index ────────────
    itineraries: List[ItineraryOption] = []
    used_combos: set[tuple] = set()

    for offset in range(len(candidates)):
        if len(itineraries) >= MAX_ITINERARIES:
            break
        built = _build_itinerary(
            candidates,
            body.hours,
            km_budget=km_cap,
            speed_kmph=configured_speed_kmph,
            fallback_port_distance_km=fallback_port_distance_km,
            requested_tags=requested_tags,
            seed_offset=offset,
        )
        if not built:
            continue
        stops, total_km, travel_minutes = built
        combo_key = tuple(sorted(s.vendor_id for s in stops))
        if combo_key in used_combos:
            continue
        used_combos.add(combo_key)

        total_h = round(sum(s.avg_time_hours for s in stops) + (travel_minutes / 60.0), 2)
        highlights = list({tag for s in stops for tag in s.tags if tag in requested_tags})
        if not highlights:
            highlights = requested_tags[:3]

        itineraries.append(
            ItineraryOption(
                itinerary_number=len(itineraries) + 1,
                label=_itinerary_label(stops, len(itineraries) + 1),
                total_hours=total_h,
                total_distance_km=round(total_km, 2),
                estimated_travel_minutes=round(travel_minutes, 1),
                total_stops=len(stops),
                highlights=highlights,
                stops=stops,
            )
        )
        print(f"Built itinerary option {len(itineraries)} with offset {offset} and combo {combo_key}")

    return ItinerarySuggestOut(
        port_id=resolved_port.id if resolved_port else None,
        port_name=resolved_port.name if resolved_port else None,
        requested_hours=body.hours,
        requested_tags=requested_tags,
        itineraries=itineraries,
        fallback_used=fallback_used,
    )


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
