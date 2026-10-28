from datetime import timedelta, date, datetime
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.agent_profile import AgentProfile
from app.db.models.aggregator_profile import AggregatorProfile
from app.db.models.email_verification import EmailVerification
from app.services.auth import get_password_hash, verify_password, create_access_token, create_refresh_token
from app.services.crew_service import generate_unique_hpid
from app.services.email import send_email_verification_code
from app.services.crew_reference import normalize_nationality, normalize_rank
from app.api.v1.routes_auth import AuthOut
from app.core.config import settings
import random
import string
import secrets

router = APIRouter()

# --- Email OTP (verify-at-registration / "block") ---
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5


def _consume_valid_otp(db: Session, email: str, code: str) -> bool:
    """Authoritative OTP check used at account creation. Verifies the latest
    unexpired code for `email`, and on success deletes all codes for that email
    (single-use). Returns False on missing/expired/wrong/over-attempts."""
    rec = (
        db.query(EmailVerification)
        .filter(EmailVerification.email == email)
        .order_by(EmailVerification.id.desc())
        .first()
    )
    if not rec or rec.expires_at < datetime.utcnow() or rec.attempts >= OTP_MAX_ATTEMPTS:
        return False
    if not verify_password(code, rec.code_hash):
        rec.attempts += 1
        db.commit()
        return False
    db.query(EmailVerification).filter(EmailVerification.email == email).delete()
    db.commit()
    return True

class CrewRegistrationIn(BaseModel):
    # User fields
    email: EmailStr
    password: str = Field(min_length=6)
    mobile_number: str
    otp: str = Field(min_length=6, max_length=6)  # emailed verification code

    # Profile fields
    full_name: str
    rank: str
    nationality: str
    passport_number: Optional[str] = None
    date_of_birth: Optional[date] = None


class SendOtpIn(BaseModel):
    email: EmailStr


class VerifyOtpIn(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)

class RegistrationCheckIn(BaseModel):
    email: EmailStr
    mobile_number: Optional[str] = None

class RegistrationCheckOut(BaseModel):
    email_exists: bool
    mobile_exists: bool

class AgentRegistrationIn(BaseModel):
    # User fields
    email: EmailStr
    password: str = Field(min_length=6)
    full_name: str
    mobile_number: str
    
    # Profile fields
    agency_name: str
    location: str
    assigned_port: Optional[str] = None
    agent_identifier: Optional[str] = None

class AggregatorRegistrationIn(BaseModel):
    # User fields
    email: EmailStr
    password: str = Field(min_length=6)
    full_name: str
    mobile_number: str
    
    # Profile fields
    company_name: str
    provider_type: str = Field(default="aggregator", pattern="^(partnered_driver|aggregator)$")
    operating_port_id: int
    aggregator_identifier: Optional[str] = None
    status: str = Field(default="Active", pattern="^(Active|Suspended|Inactive)$")

class FleetItem(BaseModel):
    type: str
    count: int
    cost_per_km: float

class AggregatorUpdate(BaseModel):
    email: Optional[EmailStr] = None
    mobile_number: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6)
    company_name: Optional[str]
    provider_type: Optional[str] = Field(default=None, pattern="^(partnered_driver|aggregator)$")
    contact_person: Optional[str]
    operating_port_id: Optional[int]
    gst_number: Optional[str]
    status: Optional[str] = Field(default=None, pattern="^(Active|Suspended|Inactive)$")

    fleet: Optional[List[FleetItem]]
    documents: Optional[List[str]]

@router.post("/send-otp", status_code=status.HTTP_200_OK)
def send_registration_otp(body: SendOtpIn, db: Session = Depends(get_db)):
    """Email a 6-digit verification code for a new-account signup. Rejects
    emails that are already registered (registration already reveals this via
    /registration/check, so this is not an enumeration regression)."""
    email = body.email.lower().strip()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    # Invalidate any previous codes for this email.
    db.query(EmailVerification).filter(EmailVerification.email == email).delete()
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(EmailVerification(
        email=email,
        code_hash=get_password_hash(code),
        expires_at=datetime.utcnow() + timedelta(minutes=OTP_TTL_MINUTES),
    ))
    db.commit()
    send_email_verification_code(to=email, code=code)
    return {"message": "Verification code sent."}


@router.post("/verify-otp", status_code=status.HTTP_200_OK)
def verify_registration_otp(body: VerifyOtpIn, db: Session = Depends(get_db)):
    """Non-consuming pre-check so the UI can confirm the code before the final
    submit. The authoritative single-use check runs in /registration/crew."""
    email = body.email.lower().strip()
    rec = (
        db.query(EmailVerification)
        .filter(EmailVerification.email == email)
        .order_by(EmailVerification.id.desc())
        .first()
    )
    if not rec or rec.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Code expired — request a new one.")
    if rec.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Too many attempts — request a new code.")
    if not verify_password(body.code, rec.code_hash):
        rec.attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid code.")
    rec.verified = True
    db.commit()
    return {"verified": True}


@router.post("/crew", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register_crew(body: CrewRegistrationIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # Block account creation until the emailed OTP is verified (single-use).
    if not _consume_valid_otp(db, email, body.otp):
        raise HTTPException(status_code=400, detail="Invalid or expired verification code")

    # Check if user already exists
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    if body.mobile_number and db.query(User).filter(User.mobile_number == body.mobile_number).first():
        raise HTTPException(status_code=409, detail="Mobile number already registered")

    try:
        nationality = normalize_nationality(body.nationality, strict=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # A passport is the one identifier that is genuinely one per person, and it
    # becomes part of the HPID, so a placeholder typed here follows someone
    # around permanently. Sign-up checked the email and the mobile number and
    # nothing else, which is how accounts under "U" and "NOT_PROVIDED" exist —
    # three different people share the first of those.
    passport = None
    if body.passport_number is not None and str(body.passport_number).strip():
        from app.services.crew_identity import (
            CrewIdentityConflict,
            passport_already_registered,
            validate_passport_number,
        )

        try:
            passport = validate_passport_number(body.passport_number)
        except CrewIdentityConflict as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if passport_already_registered(db, passport) is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "An account already exists for this passport number. "
                    "Sign in instead, or reset the password on that account."
                ),
            )

    # 1. Create User
    user = User(
        name=body.full_name,
        email=email,
        mobile_number=body.mobile_number,
        hashed_password=get_password_hash(body.password),
        role="crew"
    )
    db.add(user)
    db.flush()  # Get user.id without committing

    # 2. Create Crew Profile
    crew_profile = CrewProfile(
        user_id=user.id,
        full_name=body.full_name,
        rank=normalize_rank(body.rank) or "other",
        nationality=nationality,
        # Canonical form, so a later comparison is not defeated by spacing.
        passport_number=passport,
        date_of_birth=body.date_of_birth
    )
    db.add(crew_profile)
    db.flush() # Get crew_profile.id

    # 3. Generate HPID using Passport Number
    crew_profile.hpid = generate_unique_hpid(
        db, passport, nationality, "port_general",
        unique_fallback=user.id, exclude_profile_id=crew_profile.id,
    )

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    db.refresh(crew_profile)
    try:
        from app.api.v1.routes_crew import sync_crew_manifest_helper
        sync_crew_manifest_helper(crew_profile, db)
    except Exception as e:
        # The account is already committed above; a manifest-sync failure must
        # not fail the signup. Roll back the (now-aborted) transaction so the
        # session is usable for the refresh/token issuance below.
        db.rollback()
        print(f"Error during registration crew manifest sync: {e}")

    try:
        # The sync above walks VesselCrew, which is a *current* roster -- every
        # row is deleted when the vessel departs. So someone registering after
        # signing off matches nothing, and the assignments that are theirs keep
        # crew_profile_id NULL for ever. This attaches those, and only the ones
        # that are unambiguously theirs.
        from app.services.historical_linkage import link_historical_assignments

        # Historical registration is a proposal for evidence-backed review.
        # Active-call matching already happened above; never auto-attach history.
        link_historical_assignments(db, crew_profile, require_review=True)
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error during registration historical linkage: {e}")

    db.refresh(user)

    # 3. Issue Token
    token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )

    return AuthOut(access_token=token, refresh_token=refresh_token, role=user.role)

@router.post("/agent", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register_agent(body: AgentRegistrationIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # Check if user already exists
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    if body.mobile_number and db.query(User).filter(User.mobile_number == body.mobile_number).first():
        raise HTTPException(status_code=409, detail="Mobile number already registered")

    # 1. Create User
    user = User(
        name=body.full_name,
        email=email,
        mobile_number=body.mobile_number,
        hashed_password=get_password_hash(body.password),
        role="agent"
    )
    db.add(user)
    db.flush()

    # 2. Create Agent Profile
    agent_id = body.agent_identifier
    if not agent_id:
        rand_part = ''.join(random.choices(string.digits, k=4))
        agent_id = f"AGT-{random.randint(10000, 99999)}-{rand_part}"

    agent_profile = AgentProfile(
        user_id=user.id,
        agency_name=body.agency_name,
        location=body.location,
        agent_identifier=agent_id,
        assigned_port=body.assigned_port
    )
    db.add(agent_profile)
    
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    db.refresh(user)

    # 3. Issue Token
    token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
    
    return AuthOut(access_token=token, refresh_token=refresh_token, role=user.role)

@router.post("/aggregator", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register_aggregator(body: AggregatorRegistrationIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # Check if user already exists
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    if body.mobile_number and db.query(User).filter(User.mobile_number == body.mobile_number).first():
        raise HTTPException(status_code=409, detail="Mobile number already registered")

    # 1. Create User
    user = User(
        name=body.full_name,
        email=email,
        mobile_number=body.mobile_number,
        hashed_password=get_password_hash(body.password),
        role="aggregator"
    )
    db.add(user)
    db.flush()

    # 2. Create Aggregator Profile
    agg_id = body.aggregator_identifier
    if not agg_id:
        rand_part = ''.join(random.choices(string.digits, k=4))
        agg_id = f"AGG-{random.randint(10000, 99999)}-{rand_part}"

    aggregator_profile = AggregatorProfile(
        user_id=user.id,
        company_name=body.company_name,
        provider_type=body.provider_type,
        operating_port_id=body.operating_port_id,
        aggregator_identifier=agg_id,
        contact_person=body.full_name,
        status=body.status
    )
    db.add(aggregator_profile)

    try:
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

    db.refresh(user)

    # 3. Issue Token
    token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )

    return AuthOut(access_token=token, refresh_token=refresh_token, role=user.role)

@router.post("/check", response_model=RegistrationCheckOut)
def registration_check(body: RegistrationCheckIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    email_exists = db.query(User).filter(User.email == email).first() is not None
    mobile_exists = False
    if body.mobile_number:
        mobile_exists = (
            db.query(User)
            .filter(User.mobile_number == body.mobile_number)
            .first()
            is not None
        )

    return RegistrationCheckOut(
        email_exists=email_exists,
        mobile_exists=mobile_exists,
    )

@router.put("/aggregator/{agg_id}")
def update_aggregator(
    agg_id: int,
    payload: AggregatorUpdate,
    db: Session = Depends(get_db)
):
    aggregator = db.query(AggregatorProfile).filter(AggregatorProfile.id == agg_id).first()

    if not aggregator:
        raise HTTPException(status_code=404, detail="Aggregator not found")

    update_data = payload.dict(exclude_unset=True)

    # email/mobile_number/password live on the linked User row, not AggregatorProfile
    user_fields = {"email", "mobile_number", "password"}
    user_updates = {k: update_data.pop(k) for k in list(update_data.keys()) if k in user_fields}
    if user_updates:
        user = db.query(User).filter(User.id == aggregator.user_id).first()
        if user:
            if user_updates.get("email"):
                user.email = user_updates["email"].lower().strip()
            if user_updates.get("mobile_number"):
                user.mobile_number = user_updates["mobile_number"]
            if user_updates.get("password"):
                user.hashed_password = get_password_hash(user_updates["password"])

    for key, value in update_data.items():
        setattr(aggregator, key, value)

    db.commit()
    db.refresh(aggregator)

    return {
        "message": "Aggregator updated successfully",
        "data": aggregator
    }
