"""Bound public result pages without changing array response contracts.

HTTP requests default to 100 rows. collection/collection_offset/collection_limit
select subsequent SQL pages; X-Collection-Pages exposes navigation metadata.
Legacy X-Result-* headers remain available. Direct internal callers retain the
2,000-row default. Never use display pagination for authorization, validation,
updates or totals; those queries must remain complete.
"""

from __future__ import annotations

from typing import Optional, Tuple

from fastapi import Response

# Roughly three orders of magnitude above the largest table in development,
# so this is a runaway stop rather than a limit anyone meets in normal use.
DEFAULT_MAX_ROWS = 2000

# What `?limit=` may be raised to. A deliberate export still has a ceiling.
HARD_MAX_ROWS = 10000


def resolve_limit(limit: Optional[int]) -> int:
    if limit is None or limit <= 0:
        return DEFAULT_MAX_ROWS
    return min(limit, HARD_MAX_ROWS)


def capped(query, *, limit: Optional[int] = None) -> Tuple[list, bool, int]:
    """Rows, whether the ceiling cut them off, and the ceiling applied.

    Fetches one more row than asked for, so "there is more" is known without
    a second COUNT over the same predicate.
    """
    ceiling = resolve_limit(limit)
    rows = query.limit(ceiling + 1).all()
    truncated = len(rows) > ceiling
    return rows[:ceiling], truncated, ceiling


def apply_headers(response: Response, rows: list, truncated: bool, ceiling: int) -> None:
    """Say plainly what was returned and whether anything was left behind."""
    response.headers["X-Result-Count"] = str(len(rows))
    response.headers["X-Result-Limit"] = str(ceiling)
    response.headers["X-Result-Truncated"] = "true" if truncated else "false"


def capped_response(response: Response, query, *, limit: Optional[int] = None) -> list:
    """The common case: cap, label the response, return the rows."""
    from app.services.collection_pages import (
        options,
        note,
        result_collection,
        existing_offset,
        is_selected,
    )

    collection, label = result_collection()
    offset, page_size = options(collection)
    if not is_selected(collection):
        offset = existing_offset(query)
    ceiling = min(resolve_limit(limit), page_size)
    rows = query.offset(offset).limit(ceiling + 1).all()
    truncated = len(rows) > ceiling
    rows = note(collection, rows, offset, ceiling, label=label)
    apply_headers(response, rows, truncated, ceiling)
    return rows
