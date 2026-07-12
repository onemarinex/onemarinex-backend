from datetime import timedelta
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


@router.post("/login", response_model=AuthOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = create_refresh_token(
        subject=user.email,
        expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
    )
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
    if user and verify_password(body.password, user.hashed_password):
        token = create_access_token(
            subject=user.email,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token(
            subject=user.email,
            expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        )
        return {
            "access_token": token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "role": user.role,
            "must_change_password": user.must_change_password,
        }

    # If not in users table, check the drivers table
    driver = db.query(Driver).filter(Driver.email == email).first()
    if driver and verify_password(body.password, driver.hashed_password):
        token = create_access_token(
            subject=driver.email,
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        )
        refresh_token = create_refresh_token(
            subject=driver.email,
            expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        )
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
