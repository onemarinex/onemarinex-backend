"""Data-deletion requests — the owner's "in this release" answer, both sides.

    "Add a visible 'Request deletion of my data' action in Crew Profile >
    Privacy/Data Controls. It should initiate an authenticated request and
    confirmation, not immediate destructive deletion. Show request status and
    acknowledgement; provide a way to cancel a pending request if appropriate.
    Restrict the review queue to authorized privacy/Super Admin staff; record
    receipt, review, outcome and completion."

What is deliberately absent is any route that erases anything. The same answer
requires response deadlines and notices to be defined with privacy counsel
before launch, and warns against claiming compliance because a button exists.
Approving a request authorises an erasure and records it; a person performs it
and records what they did.
"""

from app.services.collection_pages import page_all

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.db.models.data_deletion import DataDeletionRequest
from app.db.models.user import User
from app.db.session import get_db
from app.services import data_deletion as deletion

router = APIRouter()
admin_router = APIRouter()


class DeletionRequestIn(BaseModel):
    # Optional on purpose: a person does not have to justify asking.
    reason: Optional[str] = None


class DeletionRequestOut(BaseModel):
    id: int
    status: str
    reason: Optional[str] = None
    decision_reason: Optional[str] = None
    retention_note: Optional[str] = None
    fulfilment_note: Optional[str] = None
    created_at: datetime
    decided_at: Optional[datetime] = None
    fulfilled_at: Optional[datetime] = None
    can_cancel: bool = False


class DataCategoryOut(BaseModel):
    title: str
    detail: str


class DataControlsOut(BaseModel):
    """What a request would and would not reach, in the requester's words."""

    erasable: List[DataCategoryOut]
    retained: List[DataCategoryOut]
    third_party: List[DataCategoryOut]
    backups: str
    request: Optional[DeletionRequestOut] = None


class AdminDecisionIn(BaseModel):
    reason: Optional[str] = None
    retention_note: Optional[str] = None


class RefusalIn(BaseModel):
    reason: str = Field(min_length=1)
    retention_note: str = Field(min_length=1)


class FulfilmentIn(BaseModel):
    note: str = Field(min_length=1)


class AdminRequestOut(DeletionRequestOut):
    user_id: int
    user_email: Optional[str] = None


def _out(request: Optional[DataDeletionRequest]) -> Optional[DeletionRequestOut]:
    if request is None:
        return None
    return DeletionRequestOut(
        id=request.id, status=request.status, reason=request.reason,
        decision_reason=request.decision_reason,
        retention_note=request.retention_note,
        fulfilment_note=request.fulfilment_note,
        created_at=request.created_at, decided_at=request.decided_at,
        fulfilled_at=request.fulfilled_at,
        can_cancel=request.status in deletion.CANCELLABLE,
    )


def _categories(pairs) -> List[DataCategoryOut]:
    return [DataCategoryOut(title=title, detail=detail) for title, detail in pairs]


@router.get("/privacy/data-controls", response_model=DataControlsOut)
def data_controls(db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """The classification, plus this person's most recent request if any.

    The classification is shown *before* asking, not after refusing. Telling
    someone what cannot be erased only once they have asked is how a request
    process becomes a complaints process.
    """
    return DataControlsOut(
        erasable=_categories(deletion.ERASABLE),
        retained=_categories(deletion.RETAINED),
        third_party=_categories(deletion.THIRD_PARTY),
        backups=deletion.BACKUPS,
        request=_out(deletion.latest_for(db, user_id=current_user.id)),
    )


@router.post("/privacy/deletion-requests", response_model=DeletionRequestOut,
             status_code=201)
def request_deletion(body: DeletionRequestIn, db: Session = Depends(get_db),
                     current_user: User = Depends(get_current_user)):
    try:
        request = deletion.submit(db, user_id=current_user.id, reason=body.reason)
    except deletion.DeletionError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    db.commit()
    db.refresh(request)
    return _out(request)


@router.post("/privacy/deletion-requests/{request_id}/cancel",
             response_model=DeletionRequestOut)
def cancel_deletion(request_id: int, db: Session = Depends(get_db),
                    current_user: User = Depends(get_current_user)):
    request = (
        db.query(DataDeletionRequest)
        .filter(DataDeletionRequest.id == request_id)
        .first()
    )
    # Same 404 for someone else's request as for one that does not exist:
    # whether a given id belongs to somebody is not a fact to hand out.
    if request is None or request.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Request not found")
    try:
        deletion.cancel(db, request=request, actor_user_id=current_user.id)
    except deletion.DeletionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(request)
    return _out(request)


# --- the review queue --------------------------------------------------------

def _require_superadmin(current_user: User) -> User:
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Authorised privacy staff only")
    return current_user


def _admin_out(db: Session, request: DataDeletionRequest) -> AdminRequestOut:
    user = db.query(User).filter(User.id == request.user_id).first()
    base = _out(request)
    return AdminRequestOut(
        **base.model_dump(), user_id=request.user_id,
        user_email=user.email if user else None,
    )


@admin_router.get("/deletion-requests", response_model=List[AdminRequestOut])
def deletion_queue(status: Optional[str] = None, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    _require_superadmin(current_user)
    query = db.query(DataDeletionRequest)
    if status:
        query = query.filter(DataDeletionRequest.status == status.upper())
    else:
        query = query.filter(
            DataDeletionRequest.status.in_(deletion.OPEN_STATUSES + ("APPROVED",))
        )
    rows = page_all(
        query.order_by(DataDeletionRequest.created_at),
        collection="routes_privacy.deletion_queue.0",
        label="Deletion queue",
    )
    return [_admin_out(db, row) for row in rows]


def _decide(db, request_id, current_user, action, **kwargs):
    _require_superadmin(current_user)
    request = (
        db.query(DataDeletionRequest)
        .filter(DataDeletionRequest.id == request_id)
        .first()
    )
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found")
    try:
        action(db, request=request, actor=current_user, **kwargs)
    except deletion.DeletionError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from None
    db.commit()
    db.refresh(request)
    return _admin_out(db, request)


@admin_router.post("/deletion-requests/{request_id}/review",
                   response_model=AdminRequestOut)
def start_review(request_id: int, db: Session = Depends(get_db),
                 current_user: User = Depends(get_current_user)):
    return _decide(db, request_id, current_user, deletion.begin_review)


@admin_router.post("/deletion-requests/{request_id}/approve",
                   response_model=AdminRequestOut)
def approve(request_id: int, body: AdminDecisionIn, db: Session = Depends(get_db),
            current_user: User = Depends(get_current_user)):
    """Authorises the erasure. Does not carry it out."""
    return _decide(db, request_id, current_user, deletion.approve,
                   reason=body.reason, retention_note=body.retention_note)


@admin_router.post("/deletion-requests/{request_id}/refuse",
                   response_model=AdminRequestOut)
def refuse(request_id: int, body: RefusalIn, db: Session = Depends(get_db),
           current_user: User = Depends(get_current_user)):
    return _decide(db, request_id, current_user, deletion.refuse,
                   reason=body.reason, retention_note=body.retention_note)


@admin_router.post("/deletion-requests/{request_id}/fulfilled",
                   response_model=AdminRequestOut)
def mark_fulfilled(request_id: int, body: FulfilmentIn, db: Session = Depends(get_db),
                   current_user: User = Depends(get_current_user)):
    """Record an erasure already performed, and what it covered."""
    return _decide(db, request_id, current_user, deletion.mark_fulfilled,
                   note=body.note)
