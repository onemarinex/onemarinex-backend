"""Company administration. Tenant-bound assets and immutable access activity."""

import base64
import binascii
import io
import warnings
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from PIL import Image, UnidentifiedImageError
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.company_profile import CompanyProfile
from app.db.models.company_invitation import CompanyInvitation
from app.db.models.shipping_company import CompanyAuditEvent
from app.db.models.user import User
from app.api.v1.routes_auth import get_current_user
from app.api.v1.routes_shipping_company import (
    current_company_member,
    _require_company_admin,
)
from app.api.v1.routes_company_staff import lock_admin, invite, InviteIn

router = APIRouter()


def profile_out(company, profile):
    return {
        "company_id": company.id,
        "legal_name": company.legal_name,
        "status": company.status,
        "details": {
            k: v
            for k, v in (profile.corporate_details if profile else {}).items()
            if k
            in ("trading_name", "country", "registration_number", "address", "website")
        },
        "logo": (
            "data:image/png;base64," + base64.b64encode(profile.logo_png).decode()
            if profile and profile.logo_png
            else None
        ),
        "updated_at": profile.updated_at if profile else None,
    }


@router.get("/profile")
def profile(db: Session = Depends(get_db), context=Depends(current_company_member)):
    company, _ = context
    return profile_out(company, db.get(CompanyProfile, company.id))


class LogoIn(BaseModel):
    content: str = Field(max_length=2800000)


def clean_logo(encoded):
    try:
        data = base64.b64decode(encoded, validate=True)
        if not data or len(data) > 2000000:
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if (
                    image.format not in ("PNG", "JPEG")
                    or image.width * image.height > 16000000
                ):
                    raise ValueError()
                image.load()
                normalized = image.convert("RGBA")
                normalized.thumbnail((1024, 1024))
                output = io.BytesIO()
                normalized.save(output, format="PNG")
                return output.getvalue()
    except (
        ValueError,
        binascii.Error,
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise HTTPException(
            422, "Use a valid PNG or JPEG, at most 2 MB and 16 million pixels."
        ) from None


@router.put("/profile/logo")
def put_logo(
    body: LogoIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor: User = Depends(get_current_user),
):
    company, member = context
    lock_admin(db, company, member)
    content = clean_logo(body.content)
    record = db.get(CompanyProfile, company.id)
    if record is None:
        record = CompanyProfile(company_id=company.id, corporate_details={})
        db.add(record)
    before = {"has_logo": bool(record.logo_png)}
    record.logo_png = content
    record.updated_by_user_id = actor.id
    db.add(
        CompanyAuditEvent(
            company_id=company.id,
            actor_user_id=actor.id,
            target_type="company_branding",
            target_id=company.id,
            action="logo_updated",
            before_state=before,
            after_state={"has_logo": True},
        )
    )
    db.commit()
    return profile_out(company, record)


@router.delete("/profile/logo", status_code=204)
def remove_logo(
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor: User = Depends(get_current_user),
):
    company, member = context
    lock_admin(db, company, member)
    record = db.get(CompanyProfile, company.id)
    if record and record.logo_png:
        record.logo_png = None
        record.updated_by_user_id = actor.id
        db.add(
            CompanyAuditEvent(
                company_id=company.id,
                actor_user_id=actor.id,
                target_type="company_branding",
                target_id=company.id,
                action="logo_removed",
                before_state={"has_logo": True},
                after_state={"has_logo": False},
            )
        )
    db.commit()


@router.get("/activity")
def activity(
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, member = context
    _require_company_admin(member)
    query = (
        db.query(CompanyAuditEvent, User.name)
        .outerjoin(User, User.id == CompanyAuditEvent.actor_user_id)
        .filter(
            CompanyAuditEvent.company_id == company.id,
            CompanyAuditEvent.target_type.in_(
                ["company_membership", "company_branding"]
            ),
        )
    )
    total = query.count()
    rows = (
        query.order_by(CompanyAuditEvent.created_at.desc(), CompanyAuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "items": [
            {
                "id": e.id,
                "actor": name or "Former user",
                "action": e.action,
                "created_at": e.created_at,
                "changes": {
                    k: v
                    for k, v in (e.after_state or {}).items()
                    if k in ("email", "role", "enabled", "has_logo")
                },
            }
            for e, name in rows
        ],
        "total": total,
        "offset": offset,
        "limit": limit,
    }


@router.post("/invitations/{invitation_id}/resend", status_code=201)
def resend(
    invitation_id: int,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor: User = Depends(get_current_user),
):
    company, member = context
    lock_admin(db, company, member)
    row = (
        db.query(CompanyInvitation)
        .filter_by(id=invitation_id, company_id=company.id)
        .first()
    )
    if row is None:
        raise HTTPException(404, "Invitation not found")
    if row.accepted_at:
        raise HTTPException(
            409, "This invitation was accepted. Manage the member instead."
        )
    # The existing send path rotates all pending tokens and rolls back on failure.
    return invite(
        InviteIn(email=row.email, role=row.role), db=db, context=context, actor=actor
    )
