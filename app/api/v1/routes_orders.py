from app.services.collection_pages import page_all
from typing import List, Optional, Literal, Union
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Header, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.order import Order
from app.db.models.vendor_profile import VendorProfile
from app.services.auth import decode_subject

from typing import List, Optional, Literal
from datetime import datetime
from app.db.models.order_event import OrderEvent


router = APIRouter()

# ---- auth (same as other routers) ----
def get_current_user(
    db: Session = Depends(get_db),
    authorization: Optional[str] = Header(None),
) -> User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    email = decode_subject(token)
    if not email:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user

# ---- Schemas ----
class QuoteItemOut(BaseModel):
    name: str
    quantity: float
    unit: Optional[str] = None
    unit_price: float
    line_total: float

class OrderOut(BaseModel):
    id: int
    order_number: str
    rfq_id: int
    quote_id: Optional[int]
    buyer_user_id: Optional[int]
    vendor_user_id: Optional[int]
    vendor_company: Optional[str]
    port: Optional[str]
    currency: str
    items: List[QuoteItemOut]
    shipping_cost: float
    discount_pct: float
    tax_pct: float
    subtotal: float
    tax_amount: float
    grand_total: float
    delivery_time_days: Optional[int]
    notes: Optional[str]
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

TrackingStatus = Literal[
    "processing",
    "packed",
    "departed_origin",
    "arrived_hub",
    "customs_cleared",
    "in_transit",
    "out_for_delivery",
    "delivered",
    "delayed",
    "cancelled",
]

STATUS_TO_ORDER = {
    "processing": "processing",
    "packed": "processing",
    "departed_origin": "processing",
    "arrived_hub": "processing",
    "customs_cleared": "processing",
    "in_transit": "processing",
    "out_for_delivery": "processing",
    "delivered": "fulfilled",
    "delayed": "processing",   # remains processing but event shows delay
    "cancelled": "cancelled",
}

class OrderEventIn(BaseModel):
    status: TrackingStatus
    location: Optional[str] = None
    hub_name: Optional[str] = None
    note: Optional[str] = None
    delay_reason: Optional[str] = None
    delay_hours: Optional[int] = None
    eta: Optional[datetime] = None

class OrderEventOut(BaseModel):
    id: int
    order_id: int
    actor_user_id: Optional[int]
    actor_role: Optional[str]
    status: TrackingStatus
    location: Optional[str]
    hub_name: Optional[str]
    note: Optional[str]
    delay_reason: Optional[str]
    delay_hours: Optional[int]
    eta: Optional[datetime]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

def _to_out(row: Order) -> OrderOut:
    items = [QuoteItemOut(**it) for it in (row.items or [])]
    return OrderOut(
        id=row.id,
        order_number=row.order_number,
        rfq_id=row.rfq_id,
        quote_id=row.quote_id,
        buyer_user_id=row.buyer_user_id,
        vendor_user_id=row.vendor_user_id,
        vendor_company=row.vendor_company,
        port=row.port,
        currency=row.currency,
        items=items,
        shipping_cost=float(row.shipping_cost),
        discount_pct=float(row.discount_pct),
        tax_pct=float(row.tax_pct),
        subtotal=float(row.subtotal),
        tax_amount=float(row.tax_amount),
        grand_total=float(row.grand_total),
        delivery_time_days=row.delivery_time_days,
        notes=row.notes,
        status=row.status,
        created_at=row.created_at,
    )

# ---- List my orders ----
# role-based visibility:
# - shipping_company: orders where buyer_user_id = me.id
# - vendor:            orders where vendor_user_id = me.id
# - agent:             show all (or restrict per your business rules)
StatusLiteral = Literal["confirmed", "processing", "fulfilled", "cancelled"]

@router.get("/orders", response_model=List[OrderOut])
def list_orders(
    status: Optional[StatusLiteral] = Query(default=None),
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    q = db.query(Order)
    if me.role == "shipping_company":
        q = q.filter(Order.buyer_user_id == me.id)
    elif me.role == "vendor":
        q = q.filter(Order.vendor_user_id == me.id)
    elif me.role == "agent":
        # Was: "Agents see all; change if needed" -- and it saw all. Every
        # order carries line items and negotiated totals, so any agent could
        # read every shipping company's pricing with every vendor, across all
        # tenants. Neither Order nor RFQ has an agency key, so there is no
        # rule to narrow this to; until one exists it has to be nothing.
        # See AGENT-MARKETPLACE-ACCESS in docs/ for what has to be decided.
        q = q.filter(False)
    else:
        # no role -> nothing
        q = q.filter(False)

    if status:
        q = q.filter(Order.status == status)

    rows = page_all(
        q.order_by(Order.id.desc()),
        collection="routes_orders.list_orders.0",
        label="Orders",
    )
    return [_to_out(r) for r in rows]

# ---- Get one order ----
@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    if me.role == "shipping_company" and o.buyer_user_id != me.id:
        raise HTTPException(403, "Not allowed")
    if me.role == "vendor" and o.vendor_user_id != me.id:
        raise HTTPException(403, "Not allowed")
    if me.role not in ("shipping_company", "vendor"):
        # Previously "agent allowed (or restrict if needed)". Fail closed
        # until the rule exists -- see list_orders.
        raise HTTPException(403, "Not allowed")

    return _to_out(o)

# ---- Update order status ----
class OrderStatusIn(BaseModel):
    status: StatusLiteral = Field(description="confirmed|processing|fulfilled|cancelled")

@router.patch("/orders/{order_id}/status", response_model=OrderOut)
def update_order_status(
    order_id: int,
    body: OrderStatusIn,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    # who can update? allow buyer, vendor, or agent
    # An agent used to satisfy this on its own, which made cancelling any
    # tenant's order a single authenticated request.
    can = (
        (me.role == "shipping_company" and o.buyer_user_id == me.id) or
        (me.role == "vendor" and o.vendor_user_id == me.id)
    )
    if not can:
        raise HTTPException(403, "Not allowed")

    o.status = body.status
    db.commit()
    db.refresh(o)
    return _to_out(o)


# --- Post a new tracking event (vendor or agent; shipping can post too if you want) ---
@router.post("/orders/{order_id}/events", response_model=OrderEventOut, status_code=status.HTTP_201_CREATED)
def create_order_event(
    order_id: int,
    body: OrderEventIn,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    # permissions: vendor of this order, buyer (shipping), or agent
    allowed = (
        (me.role == "vendor" and o.vendor_user_id == me.id) or
        (me.role == "shipping_company" and o.buyer_user_id == me.id)
    )
    if not allowed:
        raise HTTPException(403, "Not allowed")

    ev = OrderEvent(
        order_id=order_id,
        actor_user_id=me.id,
        actor_role=me.role,
        status=body.status,
        location=body.location,
        hub_name=body.hub_name,
        note=body.note,
        delay_reason=body.delay_reason,
        delay_hours=body.delay_hours,
        eta=body.eta,
    )
    db.add(ev)

    # Update high-level order.status if needed
    new_order_status = STATUS_TO_ORDER.get(body.status)
    if new_order_status and new_order_status != o.status:
        o.status = new_order_status

    db.commit()
    db.refresh(ev)
    db.refresh(o)

    return OrderEventOut.model_validate(ev)

# --- List tracking events for an order ---
@router.get("/orders/{order_id}/events", response_model=List[OrderEventOut])
def list_order_events(
    order_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404, "Order not found")

    # visibility: buyer, vendor, agent
    allowed = (
        (me.role == "shipping_company" and o.buyer_user_id == me.id) or
        (me.role == "vendor" and o.vendor_user_id == me.id)
    )
    if not allowed:
        raise HTTPException(403, "Not allowed")

    rows = page_all(
        db.query(OrderEvent)
        .filter(OrderEvent.order_id == order_id)
        .order_by(OrderEvent.created_at.asc(), OrderEvent.id.asc()),
        collection="routes_orders.list_order_events.0",
        label="Order events",
    )
    return [OrderEventOut.model_validate(r) for r in rows]
