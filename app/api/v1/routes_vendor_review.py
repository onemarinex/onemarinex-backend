"""The Super Admin queue for vendor verification, which did not exist.

Four documents per vendor were collected and shown to nobody. These routes are
the other half: list who is waiting, open one and actually see the evidence,
and decide.

Documents are served as short-lived signed URLs minted at the moment a
reviewer asks for them -- `storage.resolve` presigns a Spaces object or signs
a local one. Nothing stores a readable URL, and nothing here returns a
document's bytes.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from app.db.models.vendor_profile import VendorProfile
from app.db.session import get_db
from app.services import storage, vendor_verification
from app.services.result_limits import capped_response
from app.services.vendor_verification import VendorDecisionError

router = APIRouter()


def _require_superadmin(current_user: User) -> None:
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Super Admin only")


class VendorDocumentOut(BaseModel):
    field: str
    label: str
    url: str


class VendorReviewOut(BaseModel):
    id: int
    company_name: str
    contact_person: Optional[str] = None
    phone_number: Optional[str] = None
    verification_status: str
    verification_notes: Optional[str] = None
    documents: List[VendorDocumentOut] = Field(default_factory=list)
    missing_documents: List[str] = Field(default_factory=list)


class VerifyIn(BaseModel):
    evidence_type: str = Field(min_length=2, max_length=64)
    evidence_reference: Optional[str] = Field(default=None, max_length=255)
    reason: str = Field(default="", max_length=2000)


class ReasonIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


def _to_out(profile: VendorProfile, *, include_documents: bool) -> VendorReviewOut:
    present = vendor_verification.documents_present(profile)
    present_fields = {item["field"] for item in present}
    return VendorReviewOut(
        id=profile.id,
        company_name=profile.company_name,
        contact_person=profile.contact_person,
        phone_number=profile.phone_number,
        verification_status=profile.verification_status or vendor_verification.PENDING,
        verification_notes=profile.verification_notes,
        # Only when one vendor is opened. Minting four signed URLs for every
        # row of a list would hand out far more than the reviewer asked to see.
        documents=[
            VendorDocumentOut(
                field=item["field"], label=item["label"],
                url=storage.resolve(item["stored_ref"]),
            )
            for item in present
        ] if include_documents else [],
        missing_documents=[
            label for field, label in vendor_verification.DOCUMENT_FIELDS
            if field not in present_fields
        ],
    )


@router.get("/vendor-review", response_model=List[VendorReviewOut])
def list_vendor_reviews(
    response: Response,
    status: Optional[str] = None,
    limit: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_superadmin(current_user)
    query = db.query(VendorProfile)
    if status:
        query = query.filter(VendorProfile.verification_status == status.strip().lower())
    rows = capped_response(response, query.order_by(VendorProfile.id.desc()), limit=limit)
    return [_to_out(row, include_documents=False) for row in rows]


@router.get("/vendor-review/{vendor_id}", response_model=VendorReviewOut)
def get_vendor_review(
    vendor_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """One vendor, with its documents as URLs that expire."""
    _require_superadmin(current_user)
    profile = db.query(VendorProfile).filter(VendorProfile.id == vendor_id).first()
    if profile is None:
        raise HTTPException(status_code=404, detail="Vendor profile not found")
    return _to_out(profile, include_documents=True)


def _decide(db: Session, vendor_id: int, current_user: User, action, **kwargs):
    _require_superadmin(current_user)
    profile = (
        db.query(VendorProfile)
        .filter(VendorProfile.id == vendor_id)
        .with_for_update()
        .first()
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="Vendor profile not found")
    try:
        action(profile, actor_id=current_user.id, **kwargs)
    except VendorDecisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    db.commit()
    db.refresh(profile)
    return _to_out(profile, include_documents=True)


@router.post("/vendor-review/{vendor_id}/verify", response_model=VendorReviewOut)
def verify_vendor(vendor_id: int, body: VerifyIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """The only route in the app that can set a vendor to verified."""
    return _decide(db, vendor_id, current_user, vendor_verification.verify,
                   evidence_type=body.evidence_type,
                   evidence_reference=body.evidence_reference, reason=body.reason)


@router.post("/vendor-review/{vendor_id}/reject", response_model=VendorReviewOut)
def reject_vendor(vendor_id: int, body: ReasonIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    return _decide(db, vendor_id, current_user, vendor_verification.reject,
                   reason=body.reason)


@router.post("/vendor-review/{vendor_id}/reopen", response_model=VendorReviewOut)
def reopen_vendor(vendor_id: int, body: ReasonIn, db: Session = Depends(get_db),
                  current_user: User = Depends(get_current_user)):
    """Send a decided vendor back for another look at the evidence."""
    return _decide(db, vendor_id, current_user, vendor_verification.reopen,
                   reason=body.reason)
