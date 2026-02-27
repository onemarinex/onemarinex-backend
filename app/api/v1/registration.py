from datetime import timedelta, date
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.agent_profile import AgentProfile
from app.db.models.aggregator_profile import AggregatorProfile
from app.services.auth import get_password_hash, create_access_token
from app.core.config import settings
from app.api.v1.routes_auth import AuthOut
import random
import string

router = APIRouter()

class CrewRegistrationIn(BaseModel):
    # User fields
    email: EmailStr
    password: str = Field(min_length=6)
    mobile_number: str
    
    # Profile fields
    full_name: str
    rank: str
    nationality: str
    passport_number: str
    date_of_birth: date

class AgentRegistrationIn(BaseModel):
    # User fields
    email: EmailStr
    password: str = Field(min_length=6)
    full_name: str
    mobile_number: str
    
    # Profile fields
    agency_name: str
    location: str
    agent_identifier: Optional[str] = None

class AggregatorRegistrationIn(BaseModel):
    # User fields
    email: EmailStr
    password: str = Field(min_length=6)
    full_name: str
    mobile_number: str
    
    # Profile fields
    company_name: str
    operating_port: str
    aggregator_identifier: Optional[str] = None

@router.post("/crew", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register_crew(body: CrewRegistrationIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # Check if user already exists
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

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
        rank=body.rank,
        nationality=body.nationality,
        passport_number=body.passport_number,
        date_of_birth=body.date_of_birth
    )
    db.add(crew_profile)
    
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
    
    return AuthOut(access_token=token, role=user.role)

@router.post("/agent", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register_agent(body: AgentRegistrationIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # Check if user already exists
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

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
        agent_identifier=agent_id
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
    
    return AuthOut(access_token=token, role=user.role)

@router.post("/aggregator", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
def register_aggregator(body: AggregatorRegistrationIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()

    # Check if user already exists
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")

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
        operating_port=body.operating_port,
        aggregator_identifier=agg_id
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
    
    return AuthOut(access_token=token, role=user.role)
