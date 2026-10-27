"""A ceiling on how many rows one response can carry.

The read-only audit: "no shared pagination/rate-limit policy for all reviewed
identity/report endpoints was established. Several list paths end in unbounded
`.all()`."

This is the ceiling half. It is **not** pagination, and saying so matters:
pagination is a product change — the screen needs pages, the caller needs a
cursor, and 117 list endpoints would each need a decision. A ceiling is a
safety limit that needs none of that. It stops one query from trying to
materialise an entire table into memory and into a JSON response, which is the
failure the audit is pointing at.

Two rules make it safe to apply without auditing every screen:

  - the ceiling is far above anything the system holds today, so no caller's
    behaviour changes now. It is there for the table that grows by a factor of
    a thousand, not for next week;
  - when it *is* hit, the response says so in its headers rather than quietly
    returning a prefix. A truncated list that looks complete is worse than a
    slow one, because the reader draws conclusions from it.

The shape of the response is unchanged — still a bare array — so no caller
needs updating. A caller that wants more passes `?limit=`.

Deliberately not applied to queries that feed internal logic. A cleanup loop
or an aggregate that silently processed the first N rows would be a
correctness bug wearing a safety limit's clothes.

Three places this must not be used, each found by classifying the rest of the
application's list queries rather than by reasoning about it here:

  **An oldest-first list.** A ceiling keeps the rows it reaches first, so on
  `ORDER BY id` it keeps the oldest and drops the newest -- the opposite of
  what anyone wants from a truncated audit trail. `company_review`'s audit
  events are ordered that way. Order newest-first before capping, or do not
  cap.

  **A list with no ORDER BY at all.** The ceiling then returns whichever rows
  the database happened to produce, which is not an answer. `/incidents/` was
  in this state and was ordered as part of capping it.

  **A list nested inside another object.** These headers describe one
  response. A claim that carries its conflicting claim ids inside it cannot
  say *which* nested list was cut, so truncating one tells the reader nothing
  about what they are missing. Paginate the parent or expand the child on
  demand instead.

And one that is not about correctness: a count beside a list must be computed
over the whole query, not over the capped rows, or the page reports that it is
showing 2,000 of 2,000.
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
    rows, truncated, ceiling = capped(query, limit=limit)
    apply_headers(response, rows, truncated, ceiling)
    return rows
