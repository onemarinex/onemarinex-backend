"""Company publication contract: canonical cells, complementary suppression, no arbitrary prose.

Cells are UTC calendar month / recorded port / vessel. Every released response
signature has k distinct people; all signatures in a cell must pass. Consequently
subtotals cannot recover an excluded small cell. Never publish original identifiers,
amounts, contact preferences or arbitrary text. Originals use audited investigation.
"""
from collections import defaultdict
from datetime import datetime, timezone
import json
from app.core.config import settings

QUESTIONS = ("went_ashore", "difficulty", "barriers", "facility_access", "outcome",
             "nonparticipation_reason", "satisfaction", "extra_charge")
FIELD_MAP = {**{k: "aggregate and screened cohort explorer" for k in QUESTIONS},
    "amount_paid": "restricted evidence only: precise amount can identify a person",
    "currency": "restricted evidence only: payment context can identify a person",
    "message": "allowlisted generic phrases only; other originals restricted",
    "experience_at": "UTC calendar month; source for survey period attribution",
    "created_at": "submission provenance; not the survey experience date",
    "contact_opt_in": "restricted evidence only", "user_id": "audited identity reveal only"}
SAFE_PHRASES = {"good service", "very good service", "excellent service", "transport was on time",
    "the transport was on time", "helpful service", "service was delayed", "facilities were closed",
    "no comment", "thank you"}

def threshold():
    return settings.WELFARE_MIN_RESPONDENTS

def month_window(start, end):
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    left = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    right = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if end != right:
        right = datetime(right.year + (right.month == 12), right.month % 12 + 1, 1, tzinfo=timezone.utc)
    return left, right

def screened_text(value):
    normalized = " ".join((value or "").strip().rstrip(".! ").lower().split())
    return normalized.capitalize() + "." if normalized in SAFE_PHRASES else None

def safe_answers(row):
    answers = row.answers or {}
    return {k: answers.get(k) for k in QUESTIONS}

def signature(row):
    return json.dumps({"answers": safe_answers(row), "text": screened_text(row.message)}, sort_keys=True)

def eligible_rows(rows, person=lambda r:r.user_id, cell=None, variant=None):
    """Return only complete k-safe cells, never a total containing hidden leaves."""
    cells = defaultdict(lambda:defaultdict(set))
    if cell is None:
        cell = lambda r:(r.experience_at.strftime("%Y-%m"), r.port_id, r.vessel_id)
    variant = variant or signature
    for row in rows:
        cells[cell(row)][variant(row)].add(person(row))
    safe = {key for key, variants in cells.items() if all(len(ids) >= threshold() for ids in variants.values())}
    # Overlapping cohorts can disclose a singleton by subtraction even when each
    # cohort has k people. Require k indistinguishable membership patterns across
    # the entire authorized source universe, not only the current filter.
    memberships = defaultdict(set)
    for row in rows:
        memberships[person(row)].add((cell(row), variant(row)))
    patterns = defaultdict(set)
    for who, membership in memberships.items():
        patterns[frozenset(membership)].add(who)
    safe_people = {who for pattern, people in patterns.items() if len(people) >= threshold() for who in people}
    # Exclude the whole cell if membership composition would remove a leaf.
    safe = {key for key in safe if all(who in safe_people for people in cells[key].values() for who in people)}
    return [r for r in rows if cell(r) in safe]

def status(value=None, *, suppressed=False, empty=False):
    return "insufficient_sample" if suppressed else "no_responses" if empty else "unavailable" if value is None else "zero" if value == 0 else "available"


# Facility labels are crew-supplied free text too; never treat them as public master data.
PUBLIC_FACILITIES = {"seafarers centre", "seafarers center", "seamen's club", "welfare centre", "medical centre", "shopping mall", "restaurant", "hospital", "pharmacy"}

def screened_facility(value):
    value = " ".join((value or "").strip().lower().split())
    return value.title() if value in PUBLIC_FACILITIES else None
