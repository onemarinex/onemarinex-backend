# app/api/v1/routes_users.py
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.user import User
from app.services.auth import verify_token  # your helper that decodes JWT

router = APIRouter()

# Used only to read the token from "Authorization: Bearer <token>".
# It also makes the Swagger docs show a lock icon.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

class AgentProfileOut(BaseModel):
    id: int
    agency_name: str
    location: str
    agent_identifier: Optional[str] = None

    class Config:
        from_attributes = True

class UserOut(BaseModel):
    id: int
    name: Optional[str] = None
    email: EmailStr
    role: str
    mobile_number: Optional[str] = None
    agent_profile: Optional[AgentProfileOut] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

def get_current_user(
    db: Session = Depends(get_db),
    token: str = Depends(oauth2_scheme),
) -> User:
    """
    Resolve the current user from the Bearer token.
    """
    email = verify_token(token)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return user

@router.get("/me", response_model=UserOut)
def read_me(current_user: User = Depends(get_current_user)):
    """
    Return the authenticated user's profile.
    """
    return current_user
