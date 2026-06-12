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

import random
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db.models.vendors import PlaceCategory, Vendors
from app.db.models.port import Port
from app.db.session import get_db

router = APIRouter()

# ── constants ────────────────────────────────────────────────────────────────

# Map the user-facing tag names to the PlaceCategory values they cover.
TAG_TO_CATEGORIES: Dict[str, List[PlaceCategory]] = {
    "food":        [PlaceCategory.restaurant],
    "pubs":        [PlaceCategory.pub],
    "sightseeing": [PlaceCategory.sightseeing],
    "nightlife":   [PlaceCategory.pub],
    "relax":       [PlaceCategory.hotel, PlaceCategory.sightseeing],
    "shopping":    [PlaceCategory.sightseeing],   # sightseeing covers market/mall venues
    "sim_card":    [PlaceCategory.sightseeing],   # vendors tagged "sim_card" stored as sightseeing
    "currency":    [PlaceCategory.sightseeing],   # vendors tagged "currency" stored as sightseeing
}

ALL_VALID_TAGS = list(TAG_TO_CATEGORIES.keys())

# Fallback time (hours) to budget for a stop when no avg_time_spent_hours is set.
DEFAULT_TIME_BY_CATEGORY: Dict[PlaceCategory, float] = {
    PlaceCategory.restaurant: 1.0,
    PlaceCategory.pub:        1.5,
    PlaceCategory.hotel:      2.0,
    PlaceCategory.sightseeing: 1.0,
}

MAX_ITINERARIES = 6
MAX_STOPS_PER_ITINERARY = 8


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


class ItineraryOption(BaseModel):
    itinerary_number: int
    label: str
    total_hours: float
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


def _vendor_to_stop(v: Vendors) -> ItineraryStop:
    other = v.other_information or {}
    raw_tags = other.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]

    avg_time = other.get("avg_time_spent_hours")
    if avg_time is None:
        avg_time = DEFAULT_TIME_BY_CATEGORY.get(v.category, 1.0)
    else:
        try:
            avg_time = float(avg_time)
        except (TypeError, ValueError):
            avg_time = DEFAULT_TIME_BY_CATEGORY.get(v.category, 1.0)

    price = other.get("price_per_person")
    if price is not None:
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None

    image_url = None
    if v.images and len(v.images) > 0:
        image_url = v.images[0]

    return ItineraryStop(
        vendor_id=v.id,
        name=v.name,
        category=v.category.value,
        tags=raw_tags,
        avg_time_hours=avg_time,
        distance_from_port=v.distance_from_port,
        rating=v.rating or 0.0,
        price_per_person=price,
        address=v.location_name,
        phone=v.phone,
        image_url=image_url,
        timings=other.get("timings"),
        lat=v.lat,
        lng=v.lng,
        description=other.get("about") or other.get("description"),
    )


def _tags_overlap(vendor_tags: List[str], requested_tags: List[str]) -> bool:
    vt = {t.strip().lower() for t in vendor_tags}
    rt = {t.strip().lower() for t in requested_tags}
    return bool(vt & rt)


def _build_itinerary(
    candidates: List[ItineraryStop],
    hours_budget: float,
    seed_offset: int = 0,
) -> Optional[List[ItineraryStop]]:
    """
    Greedy pack: start from seed_offset in the (already-sorted) candidate list,
    wrap around, pick stops until the budget is consumed or list exhausted.
    Returns None if nothing fits.
    """
    stops: List[ItineraryStop] = []
    total = 0.0
    n = len(candidates)
    seen_ids: set[int] = set()

    for i in range(n):
        idx = (i + seed_offset) % n
        stop = candidates[idx]
        if stop.vendor_id in seen_ids:
            continue
        if total + stop.avg_time_hours <= hours_budget + 0.25:  # 15-min tolerance
            stops.append(stop)
            total += stop.avg_time_hours
            seen_ids.add(stop.vendor_id)
        if len(stops) >= MAX_STOPS_PER_ITINERARY:
            break

    return stops if stops else None


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
    requested_tags = [t.strip().lower() for t in body.tags if t.strip().lower() in ALL_VALID_TAGS]
    if not requested_tags:
        raise HTTPException(
            status_code=422,
            detail=f"No valid tags provided. Valid tags: {ALL_VALID_TAGS}",
        )

    resolved_port = _resolve_port(db, body.port_id, body.port)

    # ── 1. Collect relevant categories ───────────────────────────────────────
    relevant_categories: set[PlaceCategory] = set()
    for tag in requested_tags:
        for cat in TAG_TO_CATEGORIES.get(tag, []):
            relevant_categories.add(cat)

    # ── 2. Query vendors for this port + relevant categories ─────────────────
    query = db.query(Vendors).filter(
        Vendors.status == "Active",
        Vendors.category.in_(list(relevant_categories)),
    )
    if resolved_port:
        query = query.filter(Vendors.port_id == resolved_port.id)

    vendors = query.order_by(Vendors.rating.desc()).all()

    # ── 3. Convert to stops and split into tag-matched vs fallback ────────────
    all_stops: List[ItineraryStop] = [_vendor_to_stop(v) for v in vendors]
    tag_matched   = [s for s in all_stops if _tags_overlap(s.tags, requested_tags)]
    untagged_fill = [s for s in all_stops if s not in tag_matched]

    fallback_used = False

    # If fewer than 2 tag-matched stops, absorb untagged as fallback
    if len(tag_matched) < 2:
        fallback_used = True
        candidates = all_stops  # use everything
    else:
        candidates = tag_matched

    # Sort by rating desc
    candidates.sort(key=lambda s: s.rating, reverse=True)

    # ── 4. Build up to MAX_ITINERARIES by rotating the start index ────────────
    itineraries: List[ItineraryOption] = []
    used_combos: set[tuple] = set()

    for offset in range(len(candidates)):
        if len(itineraries) >= MAX_ITINERARIES:
            break
        stops = _build_itinerary(candidates, body.hours, seed_offset=offset)
        if not stops:
            continue
        combo_key = tuple(sorted(s.vendor_id for s in stops))
        if combo_key in used_combos:
            continue
        used_combos.add(combo_key)

        total_h = round(sum(s.avg_time_hours for s in stops), 2)
        highlights = list({tag for s in stops for tag in s.tags if tag in requested_tags})
        if not highlights:
            highlights = list(relevant_categories)[:3]

        itineraries.append(
            ItineraryOption(
                itinerary_number=len(itineraries) + 1,
                label=_itinerary_label(stops, len(itineraries) + 1),
                total_hours=total_h,
                total_stops=len(stops),
                highlights=highlights,
                stops=stops,
            )
        )

    return ItinerarySuggestOut(
        port_id=resolved_port.id if resolved_port else None,
        port_name=resolved_port.name if resolved_port else None,
        requested_hours=body.hours,
        requested_tags=requested_tags,
        itineraries=itineraries,
        fallback_used=fallback_used,
    )
