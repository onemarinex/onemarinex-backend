"""Super Admin onboarding. Draft, review, verify, invite. No shared passwords."""

from datetime import datetime, timezone, timedelta
from urllib.parse import quote
import secrets
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, EmailStr, TypeAdapter
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.shipping_company import (
    ShippingCompany,
    CompanyUserMembership,
    CompanyAuditEvent,
)
from app.db.models.company_profile import CompanyProfile, CompanyOnboardingDraft
from app.db.models.company_invitation import CompanyInvitation
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_shipping_company import _normalized
from app.api.v1.routes_company_staff import digest
from app.services import company_verification as verification, email as mail
from app.core.config import settings

router = APIRouter()


def admin(actor: User = Depends(get_current_user)):
    if actor.role != "superadmin":
        raise HTTPException(403, "Super Admin access is required.")
    return actor


class DraftIn(BaseModel):
    legal_name: str = Field(default="", max_length=255)
    trading_name: str = Field(default="", max_length=255)
    country: str = Field(default="", max_length=120)
    registration_number: str = Field(default="", max_length=120)
    address: str = Field(default="", max_length=1000)
    website: str = Field(default="", max_length=255)
    representative_name: str = Field(default="", max_length=120)
    designation: str = Field(default="", max_length=120)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=80)
    evidence_type: str = Field(default="", max_length=120)
    evidence_reference: str = Field(default="", max_length=255)
    authority_evidence: str = Field(default="", max_length=1000)


def out(row):
    return {
        "id": row.id,
        "details": row.payload,
        "status": row.status,
        "company_id": row.company_id,
        "updated_at": row.updated_at,
    }


def load(db, draft_id, lock=False):
    q = db.query(CompanyOnboardingDraft).filter_by(id=draft_id)
    if lock:
        q = q.with_for_update()
    row = q.first()
    if row is None:
        raise HTTPException(404, "Onboarding draft not found.")
    return row


@router.get("")
def drafts(
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    actor=Depends(admin),
):
    q = db.query(CompanyOnboardingDraft)
    return {
        "items": [
            out(r)
            for r in q.order_by(
                CompanyOnboardingDraft.updated_at.desc(),
                CompanyOnboardingDraft.id.desc(),
            )
            .offset(offset)
            .limit(limit)
            .all()
        ],
        "total": q.count(),
        "offset": offset,
        "limit": limit,
    }


@router.post("", status_code=201)
def create(body: DraftIn, db: Session = Depends(get_db), actor=Depends(admin)):
    row = CompanyOnboardingDraft(
        payload=body.model_dump(), created_by_user_id=actor.id, status="DRAFT"
    )
    db.add(row)
    db.commit()
    return out(row)


@router.get("/{draft_id}")
def detail(draft_id: int, db: Session = Depends(get_db), actor=Depends(admin)):
    return out(load(db, draft_id))


@router.put("/{draft_id}")
def update(
    draft_id: int, body: DraftIn, db: Session = Depends(get_db), actor=Depends(admin)
):
    row = load(db, draft_id, True)
    if row.status != "DRAFT":
        raise HTTPException(409, "This onboarding is complete.")
    row.payload = body.model_dump()
    db.commit()
    return out(row)


@router.post("/{draft_id}/complete")
def complete(draft_id: int, db: Session = Depends(get_db), actor=Depends(admin)):
    row = load(db, draft_id, True)
    if row.status == "COMPLETED":
        return out(row)
    data = row.payload
    required = [
        "legal_name",
        "country",
        "registration_number",
        "address",
        "representative_name",
        "email",
        "evidence_type",
        "evidence_reference",
        "authority_evidence",
    ]
    missing = [
        k.replace("_", " ") for k in required if not str(data.get(k, "")).strip()
    ]
    if missing:
        raise HTTPException(422, "Complete these fields: " + ", ".join(missing))
    try:
        address = (
            str(TypeAdapter(EmailStr).validate_python(data["email"])).lower().strip()
        )
    except ValueError:
        raise HTTPException(422, "Enter a valid business email.") from None
    normalized = _normalized(data["legal_name"])
    if db.query(ShippingCompany.id).filter_by(normalized_name=normalized).first():
        raise HTTPException(409, "This company is already registered.")
    existing = (
        db.query(User)
        .filter(func.lower(User.email) == address)
        .with_for_update()
        .first()
    )
    if (
        existing
        and db.query(CompanyUserMembership.id)
        .filter_by(user_id=existing.id, enabled=True)
        .first()
    ):
        raise HTTPException(
            409, "The proposed administrator already belongs to a company."
        )
    if not mail.is_configured():
        raise HTTPException(
            503, "Configure email delivery before completing onboarding."
        )
    company = ShippingCompany(
        legal_name=data["legal_name"].strip(),
        normalized_name=normalized,
        status="PENDING_VERIFICATION",
    )
    try:
        db.add(company)
        db.flush()
        verification.verify(
            db,
            company=company,
            actor=actor,
            evidence_type=data["evidence_type"],
            evidence_reference=data["evidence_reference"],
            reason="Super Admin onboarding; representative authority evidence recorded.",
        )
        db.add(
            CompanyProfile(
                company_id=company.id,
                corporate_details=data,
                updated_by_user_id=actor.id,
            )
        )
        token = secrets.token_urlsafe(32)
        invitation = CompanyInvitation(
            company_id=company.id,
            email=address,
            role="ADMIN",
            token_hash=digest(token),
            invited_by_user_id=actor.id,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
        )
        db.add(invitation)
        db.flush()
        link = (
            settings.APP_PUBLIC_BASE_URL.rstrip("/")
            + "/company-invitation#"
            + quote(token)
        )
        if not mail.send_email(
            address,
            "Your HeyPorts company administrator invitation",
            f"Your company has been onboarded to HeyPorts. Set your credentials or sign in through {link}. The link expires in 48 hours.",
        ):
            db.rollback()
            raise HTTPException(
                503, "The invitation was not sent. Onboarding was not completed."
            )
        db.add(
            CompanyAuditEvent(
                company_id=company.id,
                actor_user_id=actor.id,
                target_type="company_membership",
                target_id=invitation.id,
                action="initial_administrator_invited",
                after_state={"email": address, "role": "ADMIN"},
            )
        )
        row.company_id = company.id
        row.status = "COMPLETED"
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            409, "The company or administrator changed. Review the draft and retry."
        ) from None
    return out(row)
