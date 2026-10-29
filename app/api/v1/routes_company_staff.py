"""Company staff: possession-verified invitations and serialized membership changes."""

from typing import Optional

from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.user import User
from app.db.models.shipping_company import (
    ShippingCompany,
    CompanyUserMembership,
    CompanyAuditEvent,
)
from app.db.models.company_invitation import CompanyInvitation
from app.api.v1.routes_auth import get_current_user, get_current_user_optional
from app.api.v1.routes_shipping_company import (
    current_company_member,
    _require_company_admin,
    MemberOut,
)
from app.services.auth import get_password_hash
from app.services import email as mail
from app.core.config import settings, database_is_local
from jose import jwt, JWTError

router = APIRouter()
ROLES = ("ADMIN", "ANALYST", "VIEWER")


def now():
    return datetime.now(timezone.utc)


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def audit(db, actor, company_id, target_id, action, before, after):
    db.add(
        CompanyAuditEvent(
            actor_user_id=actor.id,
            company_id=company_id,
            target_type="company_membership",
            target_id=target_id,
            action=action,
            before_state=before,
            after_state=after,
        )
    )


def lock_admin(db, company, membership):
    db.query(ShippingCompany).filter_by(id=company.id).with_for_update().one()
    db.refresh(company)
    db.refresh(membership)
    if company.status != "VERIFIED" or not membership.enabled:
        raise HTTPException(403, "Company access is no longer active")
    _require_company_admin(membership)


def change_member(db, company, actor, member, role, enabled):
    if role not in ROLES:
        raise HTTPException(422, "Unknown company membership role")
    # Serialize *all* membership changes on the parent, including invite acceptance.
    db.query(ShippingCompany).filter_by(id=company.id).with_for_update().one()
    db.refresh(member)
    if member.enabled and member.role == "ADMIN" and (not enabled or role != "ADMIN"):
        admins = (
            db.query(CompanyUserMembership)
            .filter_by(company_id=company.id, role="ADMIN", enabled=True)
            .count()
        )
        if admins <= 1:
            raise HTTPException(409, "Keep at least one active company administrator")
    before = {"role": member.role, "enabled": member.enabled}
    member.role, member.enabled = role, enabled
    audit(
        db,
        actor,
        company.id,
        member.id,
        "membership_changed",
        before,
        {"role": role, "enabled": enabled},
    )
    db.flush()
    return member


class ChangeIn(BaseModel):
    role: str
    enabled: bool


@router.patch("/members/{user_id}", response_model=MemberOut)
def update_member(
    user_id: int,
    body: ChangeIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor: User = Depends(get_current_user),
):
    company, membership = context
    lock_admin(db, company, membership)
    member = (
        db.query(CompanyUserMembership)
        .filter_by(company_id=company.id, user_id=user_id)
        .first()
    )
    if member is None:
        raise HTTPException(404, "Member not found")
    change_member(db, company, actor, member, body.role, body.enabled)
    db.commit()
    return MemberOut(
        user_id=user_id,
        email=db.get(User, user_id).email,
        role=member.role,
        enabled=member.enabled,
    )


class InviteIn(BaseModel):
    email: EmailStr
    role: str = "VIEWER"


class InviteOut(BaseModel):
    id: int
    email: str
    role: str
    expires_at: datetime
    accepted_at: Optional[datetime]
    revoked_at: Optional[datetime]


def output(row):
    return InviteOut.model_validate(row, from_attributes=True)


@router.get("/invitations", response_model=list[InviteOut])
def invitations(
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
):
    company, member = context
    _require_company_admin(member)
    from app.services.collection_pages import page_all

    rows = page_all(
        db.query(CompanyInvitation)
        .filter_by(company_id=company.id)
        .order_by(CompanyInvitation.id.desc()),
        collection="company_invitations",
        label="Invitations",
    )
    return [output(r) for r in rows]


@router.post("/invitations", response_model=InviteOut, status_code=201)
def invite(
    body: InviteIn,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor: User = Depends(get_current_user),
):
    company, member = context
    lock_admin(db, company, member)
    if body.role not in ROLES:
        raise HTTPException(422, "Unknown company membership role")
    # Never use the mailer's development body-logging path for bearer links.
    if not mail.is_configured():
        raise HTTPException(
            503, "Email delivery must be configured before inviting staff"
        )
    db.query(ShippingCompany).filter_by(id=company.id).with_for_update().one()
    address = str(body.email).strip().lower()
    for old in (
        db.query(CompanyInvitation)
        .filter_by(
            company_id=company.id, email=address, accepted_at=None, revoked_at=None
        )
        .all()
    ):
        old.revoked_at = now()
    token = secrets.token_urlsafe(32)
    row = CompanyInvitation(
        company_id=company.id,
        email=address,
        role=body.role,
        invited_by_user_id=actor.id,
        token_hash=digest(token),
        expires_at=now() + timedelta(days=2),
    )
    db.add(row)
    db.flush()
    link = (
        settings.APP_PUBLIC_BASE_URL.rstrip("/") + "/company-invitation#" + quote(token)
    )
    if not mail.send_email(
        address,
        "Your HeyPorts company invitation",
        f"You have been invited to {company.legal_name} as {body.role}. Open {link} within 48 hours. If unexpected, ignore this invitation.",
    ):
        db.rollback()
        raise HTTPException(503, "Invitation was not sent; try again")
    audit(
        db,
        actor,
        company.id,
        row.id,
        "invitation_sent",
        None,
        {"email": address, "role": body.role},
    )
    db.commit()
    return output(row)


@router.delete("/invitations/{invitation_id}", status_code=204)
def revoke(
    invitation_id: int,
    db: Session = Depends(get_db),
    context=Depends(current_company_member),
    actor: User = Depends(get_current_user),
):
    company, member = context
    lock_admin(db, company, member)
    db.query(ShippingCompany).filter_by(id=company.id).with_for_update().one()
    row = (
        db.query(CompanyInvitation)
        .filter_by(id=invitation_id, company_id=company.id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(404, "Invitation not found")
    row.revoked_at = now()
    audit(db, actor, company.id, row.id, "invitation_revoked", None, None)
    db.commit()


INVITATION_COOKIE = "company_invitation_context"
INVITATION_PATH = "/api/v1/shipping-company/invitations"
CONTEXT_MINUTES = 15


def available_invitation(db, row):
    if row is None:
        raise HTTPException(404, "Invitation unavailable. Ask your company administrator for a new invitation.")
    company = db.get(ShippingCompany, row.company_id)
    if (row.accepted_at or row.revoked_at or aware(row.expires_at) <= now()
            or company is None or company.status != "VERIFIED"):
        raise HTTPException(410, "Invitation expired, revoked or already accepted. Ask your company administrator for a new invitation.")
    return company


def invitation_from_context(db, request):
    try:
        payload = jwt.decode(request.cookies.get(INVITATION_COOKIE, ""), settings.SECRET_KEY,
                             algorithms=[settings.ALGORITHM])
        if payload.get("type") != "company_invitation_context":
            raise ValueError()
        row = db.get(CompanyInvitation, int(payload["invitation_id"]))
        if row is None or not secrets.compare_digest(row.token_hash, payload.get("invitation_digest", "")):
            raise ValueError()
    except (JWTError, ValueError, KeyError, TypeError):
        raise HTTPException(410, "Invitation session ended. Open the invitation email again or ask your company administrator for a new invitation.") from None
    available_invitation(db, row)
    return row


def invitation_summary(db, row, actor):
    company = available_invitation(db, row)
    user = db.query(User).filter(func.lower(User.email) == row.email).first()
    return {"company_name": company.legal_name, "email": row.email, "role": row.role,
            "expires_at": row.expires_at, "account_exists": user is not None,
            "signed_in": actor is not None,
            "identity_matches": bool(user and actor and actor.id == user.id)}


class ContextIn(BaseModel):
    token: str = Field(min_length=32, max_length=200)


@router.post("/invitations/session")
def start_invitation_session(body: ContextIn, request: Request, response: Response,
                             db: Session = Depends(get_db), actor: Optional[User] = Depends(get_current_user_optional)):
    row = db.query(CompanyInvitation).filter_by(token_hash=digest(body.token)).first()
    summary = invitation_summary(db, row, actor)
    expires = min(aware(row.expires_at), now() + timedelta(minutes=CONTEXT_MINUTES))
    context = jwt.encode({"type": "company_invitation_context", "invitation_id": row.id,
                          "invitation_digest": row.token_hash, "exp": expires},
                         settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    response.set_cookie(INVITATION_COOKIE, context, httponly=True,
                        secure=request.url.scheme == "https" or not database_is_local(settings.DATABASE_URL),
                        samesite="lax", max_age=max(1, int((expires-now()).total_seconds())), path=INVITATION_PATH)
    response.headers["Cache-Control"] = "no-store"
    return summary


@router.get("/invitations/session")
def resume_invitation_session(request: Request, response: Response, db: Session = Depends(get_db),
                              actor: Optional[User] = Depends(get_current_user_optional)):
    response.headers["Cache-Control"] = "no-store"
    return invitation_summary(db, invitation_from_context(db, request), actor)


@router.delete("/invitations/session", status_code=204)
def clear_invitation_session(response: Response):
    response.delete_cookie(INVITATION_COOKIE, path=INVITATION_PATH)


class AcceptIn(BaseModel):
    token: Optional[str] = Field(default=None, min_length=32, max_length=200)
    name: Optional[str] = Field(default=None, max_length=120)
    password: Optional[str] = Field(default=None, min_length=6, max_length=72)


@router.post("/invitations/accept")
def accept(
    body: AcceptIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    actor: Optional[User] = Depends(get_current_user_optional),
):
    row = (db.query(CompanyInvitation).filter_by(token_hash=digest(body.token)).first()
           if body.token else invitation_from_context(db, request))
    available_invitation(db, row)
    company = (
        db.query(ShippingCompany).filter_by(id=row.company_id).with_for_update().one()
    )
    db.refresh(row)
    if (
        row.accepted_at
        or row.revoked_at
        or aware(row.expires_at) <= now()
        or company.status != "VERIFIED"
    ):
        raise HTTPException(410, "Invitation expired, revoked or already accepted")
    existing = (
        db.query(User)
        .filter(func.lower(User.email) == row.email)
        .with_for_update()
        .first()
    )
    if existing:
        if actor is None or actor.id != existing.id:
            raise HTTPException(403, "Sign in with the invited email before accepting")
        user = existing
    else:
        if actor is not None:
            raise HTTPException(403, "Sign out before creating the invited account")
        if not body.password or not (body.name or "").strip():
            raise HTTPException(422, "Name and password are required for a new account")
        if len(body.password.encode("utf-8")) > 72:
            raise HTTPException(422, "Use a shorter password (at most 72 UTF-8 bytes)")
        user = User(
            email=row.email,
            name=body.name.strip(),
            hashed_password=get_password_hash(body.password),
            role="shipping_company",
        )
        from sqlalchemy.exc import IntegrityError

        try:
            db.add(user)
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                409, "Account changed; sign in with the invited email and accept again"
            ) from None
    other = (
        db.query(CompanyUserMembership)
        .filter_by(user_id=user.id, enabled=True)
        .filter(CompanyUserMembership.company_id != company.id)
        .first()
    )
    if other:
        raise HTTPException(409, "Account already belongs to another company")
    member = (
        db.query(CompanyUserMembership)
        .filter_by(company_id=company.id, user_id=user.id)
        .first()
    )
    if member:
        change_member(db, company, user, member, row.role, True)
    else:
        member = CompanyUserMembership(
            company_id=company.id,
            user_id=user.id,
            role=row.role,
            enabled=True,
            invited_by_user_id=row.invited_by_user_id,
        )
        db.add(member)
        db.flush()
    row.accepted_at = now()
    audit(
        db,
        user,
        company.id,
        member.id,
        "invitation_accepted",
        None,
        {"role": member.role},
    )
    db.commit()
    response.delete_cookie(INVITATION_COOKIE, path=INVITATION_PATH)
    response.headers["Cache-Control"] = "no-store"
    return {
        "accepted": True,
        "email": row.email,
        "next_step": "Sign in to open your company dashboard",
    }
