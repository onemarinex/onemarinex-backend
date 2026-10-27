"""Deciding a data-deletion request, and saying what could be erased.

Two things live here. The state machine, which is small; and the
classification the owner's answer demands — "backend must classify erasable
identity/profile data, legally retained operational/safety/financial records,
third-party data and backup retention". That classification is the honest part
of this feature, because it is what the requester is actually told.

It is a description, not an eraser. Nothing in this module writes to the
tables it names. Approving a request records that erasure was authorised; a
person carries it out and records what they did. The owner's answer is
explicit that deadlines and notices need privacy counsel before launch and
that nobody should "claim full compliance merely because the button exists",
and the standing instruction on production data rules out a deliverable that
rewrites existing rows on its own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.db.models.data_deletion import DataDeletionRequest

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "REQUESTED": frozenset({"UNDER_REVIEW", "APPROVED", "REFUSED", "CANCELLED"}),
    "UNDER_REVIEW": frozenset({"APPROVED", "REFUSED", "CANCELLED"}),
    "APPROVED": frozenset({"FULFILLED"}),
    "REFUSED": frozenset(),
    "CANCELLED": frozenset(),
    "FULFILLED": frozenset(),
}

OPEN_STATUSES = ("REQUESTED", "UNDER_REVIEW")
# While a request is in one of these, the requester may withdraw it.
CANCELLABLE = ("REQUESTED", "UNDER_REVIEW")


# What an approved request can and cannot reach. Shown to the requester, so it
# is written in their words rather than in column names.
ERASABLE = [
    ("Account and sign-in", "Email address, password, phone number, sessions."),
    ("Profile", "Name, date of birth, nationality, photograph, contact details."),
    ("Preferences", "Notification settings and saved places."),
    ("Messages you sent", "Chat messages, feedback and contact-form messages."),
]

RETAINED = [
    ("Shore-pass and immigration records",
     "A port call's record of who went ashore is kept by the port and the "
     "agent. Removing it would alter an immigration record."),
    ("Safety records",
     "Incidents and SOS records are safety history for a vessel and the people "
     "on it, not personal preference data."),
    ("Trips already taken",
     "A completed cab trip carries the driver's and the provider's record of "
     "the journey as well as yours."),
    ("Financial records",
     "Payments and invoices are kept for the period the law requires."),
    ("Audit trail",
     "Who approved what, and when. An audit that can be edited is not one."),
]

THIRD_PARTY = [
    ("Your agent and your employer",
     "Manifest and assignment data they supplied is theirs to correct."),
    ("Cab providers and drivers",
     "A trip's record sits with the provider as well as with HeyPorts."),
]

BACKUPS = (
    "Backups are kept on a fixed cycle and are not edited in place. Data "
    "erased from the live system remains in a backup until that backup expires."
)


class DeletionError(Exception):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_superadmin(actor) -> None:
    if getattr(actor, "role", None) != "superadmin":
        raise DeletionError("Only authorised privacy staff may decide this request")


def _transition(request: DataDeletionRequest, to_status: str) -> None:
    allowed = ALLOWED_TRANSITIONS.get(request.status, frozenset())
    if to_status not in allowed:
        raise DeletionError(
            f"A request cannot go from {request.status} to {to_status}. "
            f"Allowed from {request.status}: "
            + (", ".join(sorted(allowed)) if allowed else "nothing, it is final")
        )
    request.status = to_status


def open_request_for(db: Session, *, user_id: int) -> Optional[DataDeletionRequest]:
    """The one request a person may have outstanding, if any."""
    return (
        db.query(DataDeletionRequest)
        .filter(
            DataDeletionRequest.user_id == user_id,
            DataDeletionRequest.status.in_(OPEN_STATUSES + ("APPROVED",)),
        )
        .order_by(DataDeletionRequest.created_at.desc())
        .first()
    )


def latest_for(db: Session, *, user_id: int) -> Optional[DataDeletionRequest]:
    return (
        db.query(DataDeletionRequest)
        .filter(DataDeletionRequest.user_id == user_id)
        .order_by(DataDeletionRequest.created_at.desc())
        .first()
    )


def submit(db: Session, *, user_id: int, reason: Optional[str] = None):
    """One outstanding request per person. A second is refused, not queued."""
    if open_request_for(db, user_id=user_id) is not None:
        raise DeletionError("You already have a request open")
    request = DataDeletionRequest(
        user_id=user_id, reason=(reason or None), status="REQUESTED",
    )
    db.add(request)
    db.flush()
    return request


def cancel(db: Session, *, request: DataDeletionRequest, actor_user_id: int):
    """Only the requester withdraws their own request."""
    if request.user_id != actor_user_id:
        raise DeletionError("Only the person who asked may withdraw it")
    if request.status not in CANCELLABLE:
        raise DeletionError(
            "This request has already been decided and cannot be withdrawn"
        )
    _transition(request, "CANCELLED")
    request.cancelled_at = _utc_now()
    return request


def begin_review(db: Session, *, request: DataDeletionRequest, actor):
    _require_superadmin(actor)
    _transition(request, "UNDER_REVIEW")
    request.reviewed_by_user_id = getattr(actor, "id", None)
    return request


def approve(db: Session, *, request: DataDeletionRequest, actor,
            reason: Optional[str] = None, retention_note: Optional[str] = None):
    """Authorise erasure. It does not perform it — see the module docstring."""
    _require_superadmin(actor)
    _transition(request, "APPROVED")
    request.reviewed_by_user_id = getattr(actor, "id", None)
    request.decided_at = _utc_now()
    request.decision_reason = reason
    request.retention_note = retention_note
    return request


def refuse(db: Session, *, request: DataDeletionRequest, actor,
           reason: str, retention_note: str):
    """A refusal has to say which lawful exception it rests on."""
    _require_superadmin(actor)
    if not (reason or "").strip():
        raise DeletionError("A refusal must record why")
    if not (retention_note or "").strip():
        raise DeletionError(
            "A refusal must name the lawful retention exception the requester "
            "is told about"
        )
    _transition(request, "REFUSED")
    request.reviewed_by_user_id = getattr(actor, "id", None)
    request.decided_at = _utc_now()
    request.decision_reason = reason
    request.retention_note = retention_note
    return request


def mark_fulfilled(db: Session, *, request: DataDeletionRequest, actor, note: str):
    """Record an erasure a person has already carried out.

    Deliberately a record rather than an action. Nothing here touches the data
    it describes.
    """
    _require_superadmin(actor)
    if not (note or "").strip():
        raise DeletionError("Record what was erased or anonymised")
    _transition(request, "FULFILLED")
    request.fulfilled_at = _utc_now()
    request.fulfilled_by_user_id = getattr(actor, "id", None)
    request.fulfilment_note = note
    return request
