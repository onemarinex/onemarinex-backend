import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
    Header,
)
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
    # ensure these models are imported so relationships are configured
from app.db.models.user import User
from app.services.auth import (
    get_password_hash,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_subject,
    verify_refresh_token,
)
from app.core.config import settings

router = APIRouter()


# ----------------------------
# Auth helpers / dependencies
# ----------------------------
def get_current_user(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
) -> User:
    """
    Extract current user from Bearer token in the Authorization header.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )
    token = authorization.split(" ", 1)[1].strip()
    email = decode_subject(token)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


def get_current_user_optional(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
) -> Optional[User]:
    """The signed-in user, or None — never raises.

    For endpoints that must keep serving anonymous callers but can tailor the
    response when they do know who is asking. Port rules are the case: the
    screen is readable without signing in, yet an agent's own rules must reach
    only their crew.
    """
    try:
        return get_current_user(db=db, authorization=authorization)
    except HTTPException:
        return None


# ----------------------------
# Schemas
# ----------------------------
class SignupIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    email: EmailStr
    password: str = Field(min_length=6)
    role: str = Field(pattern="^(shipping_company|vendor|agent)$")


class LoginIn(BaseModel):
    email: EmailStr
    password: str


# Unified auth response for both signup & login
class AuthOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    must_change_password: Optional[bool] = False

class RefreshIn(BaseModel):
    refresh_token: str


# ----------------------------
# Auth routes
# ----------------------------
@router.post("/signup", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def signup(body: SignupIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # enforce uniqueness
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

    user = User(
        name=(body.name or "").strip() or None,
        email=email,
        hashed_password=get_password_hash(body.password),
        role=body.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # ⬇️ issue token immediately so user can continue onboarding
    token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
    return AuthOut(access_token=token, refresh_token=refresh_token, role=user.role)



def record_login(db: Session, *, role: str, email: str,
                 user_id=None, driver_id=None) -> None:
    """Log one successful login.

    Deliberately swallows its own errors: an analytics row must never be the
    reason someone cannot sign in. A failure here is logged and the login
    proceeds.
    """
    from app.db.models.login_event import LoginEvent

    try:
        db.add(LoginEvent(user_id=user_id, driver_id=driver_id,
                          role=role, email=email))
        db.commit()
    except Exception:
        db.rollback()
        logging.getLogger("app.auth").exception("could not record login event")


def _check_credential(db: Session, row, password: str, hashed: str) -> bool:
    """Verify a password under the shared attempt policy.

    Returns True on success. Raises 429 when the account is locked or the
    attempt is too soon after the last, and returns False for a wrong
    password — which the caller turns into the same 401 an unknown email
    gets, so a failure still says nothing about whether the account exists.

    `row` is None for an unknown email. There is nothing to count against, and
    inventing a record for every string anyone types is its own problem, so
    those are simply not counted. The rate an attacker can enumerate addresses
    is an infrastructure limit, not this one.
    """
    from app.services import attempt_limits

    if row is None:
        return False

    now = datetime.now(timezone.utc)
    attempt_limits.assert_attempt_allowed(row, now=now, fields=attempt_limits.AUTH)

    if verify_password(password, hashed):
        attempt_limits.record_success(row, fields=attempt_limits.AUTH)
        db.commit()
        return True

    locked = attempt_limits.record_failure(row, now=now, fields=attempt_limits.AUTH)
    db.commit()
    if locked:
        raise attempt_limits.locked_out_error()
    return False


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if not _check_credential(db, user, body.password, user.hashed_password if user else ""):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
    record_login(db, role=user.role, email=user.email, user_id=user.id)
    return AuthOut(
        access_token=token,
        refresh_token=refresh_token,
        role=user.role,
        must_change_password=user.must_change_password
    )

@router.post("/login-unified")
def login_unified(body: LoginIn, db: Session = Depends(get_db)):
    """Unified login that checks both users table and drivers table."""
    from app.db.models.driver import Driver

    email = body.email.lower().strip()

    # First, check the users table (crew, agent, aggregator, superadmin)
    user = db.query(User).filter(User.email == email).first()
    if _check_credential(db, user, body.password, user.hashed_password if user else ""):
        token = create_access_token(
            subject=user.email,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token(
            subject=user.email,
            expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        )
        record_login(db, role=user.role, email=user.email, user_id=user.id)
        return {
            "access_token": token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "role": user.role,
            "must_change_password": user.must_change_password,
        }

    # If not in users table, check the drivers table
    driver = db.query(Driver).filter(Driver.email == email).first()
    if _check_credential(db, driver, body.password, driver.hashed_password if driver else ""):
        token = create_access_token(
            subject=driver.email,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token(
            subject=driver.email,
            expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        )
        record_login(db, role="driver", email=driver.email, driver_id=driver.id)
        return {
            "access_token": token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "role": "driver",
            "must_change_password": driver.is_temp_password == 1,
            "name": driver.name,
            "aggregator_name": driver.aggregator.company_name if driver.aggregator else None,
        }

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.post("/refresh", response_model=AuthOut)
def refresh_token(body: RefreshIn, db: Session = Depends(get_db)):
    email = verify_refresh_token(body.refresh_token)
    if not email:
        raise HTTPException(status_code=401, detail="Invalid refresh token")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    access_token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
    return AuthOut(access_token=access_token, refresh_token=refresh_token, role=user.role)

class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str

@router.post("/change-password")
def change_password(
    body: ChangePasswordIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not verify_password(body.old_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect old password")
        
    current_user.hashed_password = get_password_hash(body.new_password)
    current_user.must_change_password = False
    db.commit()
    return {"message": "Password updated successfully"}


# ---------- forgot password (email a 6-digit reset code) ----------

from datetime import datetime, timedelta
import secrets

from app.db.models.password_reset import PasswordReset
from app.services.email import send_password_reset_code

RESET_CODE_TTL_MINUTES = 15


class ForgotPasswordIn(BaseModel):
    email: EmailStr


class ResetPasswordIn(BaseModel):
    email: EmailStr
    code: str
    new_password: str = Field(..., min_length=8)


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordIn, db: Session = Depends(get_db)):
    """Email a short-lived reset code. Always returns 200 so the endpoint
    can't be used to probe which emails are registered."""
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if user:
        # Invalidate any previous codes for this user.
        db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
        code = f"{secrets.randbelow(1_000_000):06d}"
        db.add(PasswordReset(
            user_id=user.id,
            token=get_password_hash(code),
            expires_at=datetime.utcnow() + timedelta(minutes=RESET_CODE_TTL_MINUTES),
        ))
        db.commit()
        send_password_reset_code(to=user.email, name=user.name, code=code)
    return {"message": "If that email is registered, a reset code has been sent."}


@router.post("/reset-password")
def reset_password(body: ResetPasswordIn, db: Session = Depends(get_db)):
    generic_error = HTTPException(status_code=400, detail="Invalid or expired reset code")
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user:
        raise generic_error
    reset = (
        db.query(PasswordReset)
        .filter(PasswordReset.user_id == user.id)
        .order_by(PasswordReset.id.desc())
        .first()
    )
    if not reset or reset.expires_at < datetime.utcnow():
        raise generic_error
    if not verify_password(body.code, reset.token):
        raise generic_error

    user.hashed_password = get_password_hash(body.new_password)
    user.must_change_password = False
    db.query(PasswordReset).filter(PasswordReset.user_id == user.id).delete()
    db.commit()
    return {"message": "Password reset successfully. You can now log in."}
