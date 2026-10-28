"""Crew bill upload — shore-leave receipts, kept per crew member.

Files go to DigitalOcean Spaces when configured (persistent, shared across
instances), else local disk in dev — see app.services.storage. `receipt_url`
stores the raw storage reference; responses resolve it to a fetchable URL
(direct/CDN for public, a presigned URL for private).
"""

from app.services.collection_pages import page_all
import io
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import Response
from app.services.result_limits import capped_response
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.db.models.cab_booking import CabBooking, BookingStatus
from app.db.models.crew_profile import CrewProfile
from app.db.models.expense_bill import ExpenseBill
from app.db.models.shore_pass import ShorePass
from app.db.models.user import User
from app.db.session import get_db
from app.services import storage
from app.services import bill_extraction

router = APIRouter()

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"}
# Types Claude vision can read for auto-extract (HEIC is not supported there).
EXTRACT_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

# A bill may be linked to a trip that is still active, or to one that ended
# within this grace window.
TRIP_LINK_GRACE = timedelta(hours=24)

# Cab statuses that mean the ride is underway or has a confirmed provider.
_CAB_ACTIVE_STATUSES = {
    BookingStatus.DRIVER_ASSIGNED, BookingStatus.DRIVER_ACCEPTED, BookingStatus.ON_TRIP,
    BookingStatus.ARRIVED, BookingStatus.IN_PROGRESS, BookingStatus.CONFIRMED,
    BookingStatus.PROVIDER_ACCEPTED,
}
_CAB_ACTIVE_STATUS_VALUES = {status.value for status in _CAB_ACTIVE_STATUSES}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize naive timestamps (legacy rows store naive UTC) for comparison."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _shore_pass_ended_at(sp: ShorePass) -> Optional[datetime]:
    """When the shore leave ended: crew checked back in, or the pass expired."""
    now = _utcnow()
    in_time = _as_aware(sp.in_time)
    expires = _as_aware(sp.expires_at)
    # Some legacy approvals pre-fill in_time with the future expiry time. That
    # is still an active shore leave until the timestamp is actually reached.
    if in_time and in_time <= now:
        return in_time
    if expires and expires <= now:
        return expires
    return None  # still out / not expired -> active


def _cab_ended_at(cb: CabBooking) -> Optional[datetime]:
    explicit_end = _as_aware(cb.trip_completed_at) or _as_aware(cb.completed_at)
    if explicit_end:
        return explicit_end
    # Older completed rows did not always populate a completion timestamp.
    # updated_at is the closest reliable boundary for their 24-hour window.
    if _cab_status(cb) == BookingStatus.COMPLETED.value:
        return _as_aware(cb.updated_at) or _as_aware(cb.created_at)
    return None


def _cab_status(cb: CabBooking) -> str:
    status = cb.status
    return str(getattr(status, "value", status)).strip().lower()


def _ended_within_grace(ended_at: Optional[datetime]) -> bool:
    if ended_at is None:
        return False
    age = _utcnow() - ended_at
    return timedelta(0) <= age <= TRIP_LINK_GRACE


def _shore_pass_link_state(sp: ShorePass) -> tuple[bool, Optional[datetime]]:
    """Return whether a shore pass is current/recent and its end timestamp."""
    if (sp.status or "").strip().lower() != "approved" or not sp.is_verified:
        return False, None
    ended_at = _shore_pass_ended_at(sp)
    return ended_at is None or _ended_within_grace(ended_at), ended_at


def _cab_link_state(cb: CabBooking) -> tuple[bool, Optional[datetime]]:
    """Return whether a cab ride is active/recent and its end timestamp."""
    status = _cab_status(cb)
    if status in _CAB_ACTIVE_STATUS_VALUES:
        return True, None
    if status != BookingStatus.COMPLETED.value:
        return False, None
    ended_at = _cab_ended_at(cb)
    return _ended_within_grace(ended_at), ended_at


def _validate_trip_link(
    db: Session,
    profile: CrewProfile,
    shore_pass_id: Optional[int],
    cab_booking_id: Optional[int],
) -> None:
    """Require exactly one owned trip that is active or ended within 24 hours."""
    if shore_pass_id is None and cab_booking_id is None:
        raise HTTPException(
            status_code=400,
            detail="Select the current trip or a trip that ended within the last 24 hours",
        )
    if shore_pass_id is not None and cab_booking_id is not None:
        raise HTTPException(status_code=400, detail="Link the bill to either a shore pass or a cab booking, not both")

    if shore_pass_id is not None:
        sp = db.query(ShorePass).filter(
            ShorePass.id == shore_pass_id,
            ShorePass.crew_profile_id == profile.id,
        ).first()
        if not sp:
            raise HTTPException(status_code=404, detail="Shore pass not found")
        linkable, _ = _shore_pass_link_state(sp)
        if not linkable:
            raise HTTPException(
                status_code=400,
                detail="This shore leave is not active and did not end within the last 24 hours",
            )

    if cab_booking_id is not None:
        cb = db.query(CabBooking).filter(
            CabBooking.id == cab_booking_id,
            CabBooking.crew_id == profile.id,
        ).first()
        if not cb:
            raise HTTPException(status_code=404, detail="Cab booking not found")
        linkable, _ = _cab_link_state(cb)
        if not linkable:
            raise HTTPException(
                status_code=400,
                detail="This cab trip is not active and did not end within the last 24 hours",
            )


class BillOut(BaseModel):
    id: int
    merchant: str
    amount: Optional[float] = None            # what the crew paid (post-tax)
    bill_number: Optional[str] = None
    notes: Optional[str] = None
    bill_date: Optional[datetime] = None
    shore_pass_id: Optional[int] = None
    cab_booking_id: Optional[int] = None
    receipt_url: str
    receipt_filename: str
    created_at: datetime


def _to_out(bill: ExpenseBill) -> BillOut:
    # Crew-facing view: the amount shown is what was actually paid (post-tax);
    # fall back to the legacy single amount for older rows.
    paid = bill.amount_post_tax if bill.amount_post_tax is not None else bill.amount
    return BillOut(
        id=bill.id,
        merchant=bill.merchant,
        amount=float(paid) if paid is not None else None,
        bill_number=bill.bill_number,
        notes=bill.notes,
        bill_date=bill.bill_date,
        shore_pass_id=bill.shore_pass_id,
        cab_booking_id=bill.cab_booking_id,
        receipt_url=storage.resolve(bill.receipt_url),  # presigns private refs
        receipt_filename=bill.receipt_filename,
        created_at=bill.created_at,
    )


def _crew_profile(db: Session, current_user: User) -> CrewProfile:
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Only crew can manage expense bills")
    profile = db.query(CrewProfile).filter(CrewProfile.user_id == current_user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Crew profile not found")
    return profile


@router.post("", response_model=BillOut, status_code=201)
def upload_bill(
    file: UploadFile = File(...),
    merchant: str = Form(...),
    amount: Optional[float] = Form(None),
    amount_pre_tax: Optional[float] = Form(None),
    amount_post_tax: Optional[float] = Form(None),
    bill_number: Optional[str] = Form(None),
    shore_pass_id: Optional[int] = Form(None),
    cab_booking_id: Optional[int] = Form(None),
    notes: Optional[str] = Form(None),
    bill_date: Optional[datetime] = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = _crew_profile(db, current_user)

    if not merchant.strip():
        raise HTTPException(status_code=400, detail="Merchant name is required")
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Upload an image (JPG/PNG/WebP/HEIC) or PDF")

    _validate_trip_link(db, profile, shore_pass_id, cab_booking_id)

    # Buffer with a size guard before persisting (bills are small).
    buf = io.BytesIO()
    size = 0
    while chunk := file.file.read(1024 * 1024):
        size += len(chunk)
        if size > MAX_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
        buf.write(chunk)
    buf.seek(0)

    ext = "." + (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ".jpg"
    key = f"expense_bills/bill_{profile.id}_{uuid.uuid4().hex}{ext}"
    # A receipt carries a crew member's spending; resolve() already presigns
    # it on read, so this only states what was previously left to the bucket.
    stored_ref = storage.save_fileobj(buf, key, content_type=file.content_type, public=False)

    bill = ExpenseBill(
        crew_id=profile.id,
        merchant=merchant.strip(),
        # `amount` stays the paid total for back-compat; prefer the explicit
        # post-tax figure when provided.
        amount=amount_post_tax if amount_post_tax is not None else amount,
        amount_pre_tax=amount_pre_tax,
        amount_post_tax=amount_post_tax if amount_post_tax is not None else amount,
        bill_number=(bill_number or "").strip() or None,
        shore_pass_id=shore_pass_id,
        cab_booking_id=cab_booking_id,
        notes=(notes or "").strip() or None,
        bill_date=bill_date,
        receipt_url=stored_ref,
        receipt_filename=file.filename or key.rsplit("/", 1)[-1],
    )
    db.add(bill)
    try:
        db.commit()
        db.refresh(bill)
    except Exception as e:
        db.rollback()
        storage.delete(stored_ref)  # don't orphan the uploaded object
        raise HTTPException(status_code=500, detail=str(e))
    return _to_out(bill)


class ExtractOut(BaseModel):
    merchant: Optional[str] = None
    bill_number: Optional[str] = None
    amount: Optional[float] = None            # total paid (= post-tax)
    amount_pre_tax: Optional[float] = None
    amount_post_tax: Optional[float] = None
    currency: Optional[str] = None
    bill_date: Optional[str] = None  # YYYY-MM-DD
    confidence: float = 0.0
    enabled: bool = True  # False when ANTHROPIC_API_KEY unset (UI can hint)


@router.post("/extract", response_model=ExtractOut)
def extract_bill_fields(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Read a receipt image/PDF and return suggested fields to pre-fill the
    upload form. Does not persist anything; the crew confirms then POSTs to ''."""
    _crew_profile(db, current_user)  # crew-only

    if file.content_type not in EXTRACT_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Use JPG/PNG/WebP/GIF or PDF for auto-extract")

    data = bytearray()
    while chunk := file.file.read(1024 * 1024):
        data.extend(chunk)
        if len(data) > MAX_SIZE_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 10 MB)")

    result = bill_extraction.extract_bill(bytes(data), file.content_type)
    return ExtractOut(
        merchant=result.merchant,
        bill_number=result.bill_number,
        amount=result.amount,
        amount_pre_tax=result.amount_pre_tax,
        amount_post_tax=result.amount_post_tax,
        currency=result.currency,
        bill_date=result.bill_date,
        confidence=result.confidence,
        enabled=bill_extraction.extraction_enabled(),
    )


class LinkableTripOut(BaseModel):
    kind: str                       # "shore_pass" | "cab_booking"
    id: int
    label: str                      # human-readable option for the picker
    ended_at: Optional[datetime] = None  # None -> still active


@router.get("/linkable-trips", response_model=List[LinkableTripOut])
def linkable_trips(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Trips this crew member can link a bill to right now: active shore
    passes / cab bookings, plus ones that ended within the last 24 hours."""
    profile = _crew_profile(db, current_user)
    active: List[LinkableTripOut] = []
    recent: List[LinkableTripOut] = []

    passes = page_all(
        db.query(ShorePass)
        .filter(
            ShorePass.crew_profile_id == profile.id,
            ShorePass.status == "approved",
            ShorePass.is_verified.is_(True),
        )
        .order_by(ShorePass.id.desc()),
        collection="routes_expenses.linkable_trips.0",
        label="Linkable trips",
    )
    for sp in passes:
        is_linkable, ended = _shore_pass_link_state(sp)
        if not is_linkable:
            continue
        bits = [b for b in (sp.port_name, sp.vessel_name) if b]
        label = f"Shore leave {sp.shore_pass_id}" + (f" — {' / '.join(bits)}" if bits else "")
        target = active if ended is None else recent
        target.append(LinkableTripOut(kind="shore_pass", id=sp.id, label=label, ended_at=ended))

    bookings = page_all(
        db.query(CabBooking)
        .filter(
            CabBooking.crew_id == profile.id,
            CabBooking.status.in_(_CAB_ACTIVE_STATUSES | {BookingStatus.COMPLETED}),
        )
        .order_by(CabBooking.id.desc()),
        collection="routes_expenses.linkable_trips.1",
        label="Linkable trips",
    )
    for cb in bookings:
        is_linkable, ended = _cab_link_state(cb)
        if not is_linkable:
            continue
        label = f"Cab {cb.booking_id} — {cb.drop_address[:40]}"
        target = active if ended is None else recent
        target.append(LinkableTripOut(kind="cab_booking", id=cb.id, label=label, ended_at=ended))

    recent.sort(
        key=lambda trip: _as_aware(trip.ended_at)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return active + recent


@router.get("", response_model=List[BillOut])
def list_bills(
    response: Response,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = _crew_profile(db, current_user)
    bills = capped_response(
        response,
        db.query(ExpenseBill)
        .filter(ExpenseBill.crew_id == profile.id)
        .order_by(ExpenseBill.created_at.desc()),
        limit=limit,
    )
    return [_to_out(b) for b in bills]


@router.delete("/{bill_id}", status_code=204)
def delete_bill(
    bill_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    profile = _crew_profile(db, current_user)
    bill = (
        db.query(ExpenseBill)
        .filter(ExpenseBill.id == bill_id, ExpenseBill.crew_id == profile.id)
        .first()
    )
    if not bill:
        raise HTTPException(status_code=404, detail="Bill not found")

    stored_ref = bill.receipt_url
    db.delete(bill)
    db.commit()
    storage.delete(stored_ref)  # best-effort; DB row is source of truth
    return None
