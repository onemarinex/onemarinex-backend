from app.services.collection_pages import page_all

# app/api/routes/routesrfqs.py
from datetime import datetime
from typing import List, Optional, Literal

from fastapi import APIRouter, Depends, HTTPException, Header, status
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.rfq import RFQ
from app.db.models.user import User
from app.db.models.vendor_profile import VendorProfile
from app.services.auth import decode_subject

router = APIRouter()


# -------- Auth dependency (reads Bearer token) --------
def get_current_user(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
) -> User:
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


# ------------- Schemas -------------
class Item(BaseModel):
    name: str
    quantity: Optional[float] = None
    unit: Optional[
        Literal[
            "units",
            "sets",
            "pieces",
            "kg",
            "g",
            "tonnes",
            "liters",
            "ml",
            "barrels",
            "meters",
            "cm",
            "mm",
            "feet",
            "inches",
            "packs",
            "boxes",
            "rolls",
            "cans",
            "bottles",
        ]
    ] = None
    essential: bool = False
    size_spec: Optional[str] = None
    note: Optional[str] = None
    # Legacy free-form qty for backward compatibility
    qty: Optional[str] = None


class Terms(BaseModel):
    delivery: Optional[str] = None
    payment: Optional[str] = None


class RFQIn(BaseModel):
    title: str
    buyer_company: str
    port: str
    deadline_days: int = 5
    budget_min: Optional[int] = None
    budget_max: Optional[int] = None
    tags: List[str] = Field(default_factory=list)
    required_items: List[Item] = Field(default_factory=list)
    terms: Optional[Terms] = None


class RFQOut(RFQIn):
    id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)


# ------------- Helpers -------------
def _get_vendor_ports(db: Session, user_id: int) -> List[str]:
    vp = (
        db.query(VendorProfile)
        .filter(VendorProfile.user_id == user_id)
        .first()
    )
    # ports_served is a Postgres text[] (can be None)
    return list(vp.ports_served or []) if vp else []


def _ensure_rfq_visible_to_user(db: Session, rfq: RFQ, me: User) -> None:
    """
    Raises 404 if the RFQ should not be visible to the current user.
    We use 404 (not 403) to avoid leaking existence of RFQs.
    """
    if me.role == "shipping_company":
        if rfq.user_id != me.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="RFQ not found"
            )
    elif me.role == "vendor":
        ports = _get_vendor_ports(db, me.id)
        if not ports or rfq.port not in ports:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="RFQ not found"
            )
    else:
        # agent or other roles currently have no visibility
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="RFQ not found"
        )


# ------------- Routes -------------
@router.get("/rfqs", response_model=List[RFQOut])
def list_rfqs(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """
    - shipping_company: list only my RFQs
    - vendor: list RFQs where RFQ.port is in my vendor profile ports_served
    - agent/others: return empty for now
    """
    if me.role == "shipping_company":
        return page_all(
            db.query(RFQ).filter(RFQ.user_id == me.id).order_by(RFQ.id.desc()),
            collection="routes_rfqs.list_rfqs.0",
            label="Rfqs",
        )

    if me.role == "vendor":
        ports = _get_vendor_ports(db, me.id)
        if not ports:
            return []
        return page_all(
            db.query(RFQ).filter(RFQ.port.in_(ports)).order_by(RFQ.id.desc()),
            collection="routes_rfqs.list_rfqs.1",
            label="Rfqs",
        )

    # Agents (and any other roles) – no RFQs yet
    return []


@router.post("/rfqs", response_model=RFQOut, status_code=status.HTTP_201_CREATED)
def create_rfq(
    body: RFQIn,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Create a new RFQ; only shipping companies can create."""
    if me.role != "shipping_company":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only shipping companies can create RFQs",
        )

    payload = body.model_dump()
    if payload.get("terms") is None:
        payload["terms"] = {}

    rfq = RFQ(user_id=me.id, **payload)
    db.add(rfq)
    db.commit()
    db.refresh(rfq)
    return rfq

@router.get("/rfqs/market", response_model=List[RFQOut])
def vendor_market(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    # only vendors see the market feed
    if me.role != "vendor":
        raise HTTPException(status_code=403, detail="Only vendors can access RFQ market")

    profile = (
        db.query(VendorProfile)
        .filter(VendorProfile.user_id == me.id)
        .first()
    )
    # no profile or no ports -> empty list
    if not profile or not profile.ports_served:
        return []

    return page_all(
        db.query(RFQ)
        .filter(RFQ.port.in_(profile.ports_served))
        .order_by(RFQ.id.desc()),
        collection="routes_rfqs.vendor_market.0",
        label="Vendor market",
    )


@router.get("/rfqs/{rfq_id}", response_model=RFQOut)
def get_rfq(
    rfq_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Fetch a single RFQ if visible to the current user."""
    rfq = db.get(RFQ, rfq_id)
    if not rfq:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="RFQ not found"
        )
    _ensure_rfq_visible_to_user(db, rfq, me)
    return rfq


@router.get("/rfqs/{rfq_id}/pdf")
def rfq_pdf(
    rfq_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    """Stub for RFQ PDF; visibility rules apply."""
    rfq = db.get(RFQ, rfq_id)
    if not rfq:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="RFQ not found"
        )
    _ensure_rfq_visible_to_user(db, rfq, me)
    # TODO: stream/generate real PDF
    return {"ok": True, "rfq_id": rfq_id}
