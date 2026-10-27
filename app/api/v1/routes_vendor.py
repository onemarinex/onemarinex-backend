from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_user
from app.db.models.vendor_profile import VendorProfile
from app.db.session import get_db
from app.services.storage import save_upload

router = APIRouter()

@router.post("/vendor/profile", status_code=status.HTTP_201_CREATED)
async def create_vendor_profile(
    company_name: str = Form(...),
    contact_person: str = Form(""),
    phone_number: str = Form(""),
    ports_served: List[str] = Form([]),
    categories_supplied: List[str] = Form([]),
    logo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    if user.role != "vendor":
        raise HTTPException(status_code=403, detail="Only vendors can create profiles")

    existing = db.query(VendorProfile).filter(VendorProfile.user_id == user.id).first()
    if existing:
        raise HTTPException(status_code=409, detail="Profile already exists")

    logo_url = await save_upload(logo, public=True) if logo else None

    profile = VendorProfile(
        user_id=user.id,
        company_name=company_name.strip(),
        contact_person=contact_person.strip() or None,
        phone_number=phone_number.strip() or None,
        ports_served=ports_served,
        categories_supplied=categories_supplied,
        logo_url=logo_url,
        verification_status="pending",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return {"id": profile.id, "status": "created"}

@router.post("/vendor/documents")
async def upload_vendor_documents(
    trade_license: Optional[UploadFile] = File(None),
    gst_vat: Optional[UploadFile] = File(None),
    bank_details: Optional[UploadFile] = File(None),
    iso_certifications: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Vendor profile not found")

    # Verification documents, not brand assets: a trade licence, a GST
    # certificate and bank details identify a business and its money. These
    # were public-read by default and under a guessable name on local disk.
    if trade_license:       profile.trade_license_url = await save_upload(trade_license, public=False)
    if gst_vat:             profile.gst_vat_url = await save_upload(gst_vat, public=False)
    if bank_details:        profile.bank_details_url = await save_upload(bank_details, public=False)
    if iso_certifications:  profile.iso_certifications_url = await save_upload(iso_certifications, public=False)

    if any([profile.trade_license_url, profile.gst_vat_url, profile.bank_details_url, profile.iso_certifications_url]):
        profile.verification_status = "under_review"

    db.commit()
    return {"status": "documents_uploaded"}

@router.get("/vendor/verification")
def get_vendor_verification_status(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == user.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="Vendor profile not found")
    return {"status": profile.verification_status, "notes": profile.verification_notes}
