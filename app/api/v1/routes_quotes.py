# app/api/v1/routes_quotes.py
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Header, status, Query
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.rfq import RFQ
from app.db.models.vendor_profile import VendorProfile
from app.db.models.rfq_quote import RFQQuote
from app.services.auth import decode_subject
from app.db.models.order import Order
from sqlalchemy.exc import IntegrityError

router = APIRouter()



# -------- auth dependency (same behavior as in your other routes) --------
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

# -------------------- Schemas --------------------
class QuoteItemIn(BaseModel):
    unit_price: float = Field(ge=0)

class QuoteSubmitIn(BaseModel):
    rfq_id: int
    currency: str = Field(default="USD", max_length=8)
    items: List[QuoteItemIn] = Field(min_items=1)
    shipping_cost: float = 0
    discount_pct: float = Field(default=0, ge=0, le=100)
    tax_pct: float = Field(default=0, ge=0, le=100)
    delivery_time_days: Optional[int] = Field(default=None, ge=1)
    vendor_notes: Optional[str] = None

class QuoteItemOut(BaseModel):
    name: str
    quantity: float
    unit: Optional[str] = None
    unit_price: float
    line_total: float

class QuoteOut(BaseModel):
    id: int
    rfq_id: int
    vendor_user_id: int
    vendor_company: Optional[str] = None
    currency: str
    items: List[QuoteItemOut]
    shipping_cost: float
    discount_pct: float
    tax_pct: float
    subtotal: float
    tax_amount: float
    grand_total: float
    delivery_time_days: Optional[int]
    vendor_notes: Optional[str]
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

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
    items: list[QuoteItemOut]
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

# -------- utils --------
def _money(x: Union[float, Decimal]) -> Decimal:
    """Quantize to 2 decimals with HALF_UP."""
    d = Decimal(str(x))
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

# -------------------- Vendor: submit/replace a quote --------------------
@router.post("/vendor/quotes", response_model=QuoteOut, status_code=status.HTTP_201_CREATED)
def submit_quote(
    body: QuoteSubmitIn,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    if me.role != "vendor":
        raise HTTPException(status_code=403, detail="Only vendors can submit quotations")

    rfq = db.get(RFQ, body.rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    # Vendor profile must exist; optionally ensure port is served
    profile = db.query(VendorProfile).filter(VendorProfile.user_id == me.id).first()
    if not profile:
        raise HTTPException(status_code=403, detail="Complete vendor profile before quoting")

    # OPTIONAL strict: ensure vendor serves the RFQ port
    if profile.ports_served and rfq.port not in (profile.ports_served or []):
        raise HTTPException(status_code=403, detail="You don't serve this port")

    # Validate item count vs RFQ items
    rfq_items: list = rfq.required_items or []
    if len(body.items) != len(rfq_items):
        raise HTTPException(
            status_code=422,
            detail=f"Expected {len(rfq_items)} unit prices, got {len(body.items)}",
        )

    # Recompute totals server-side
    built_items: list[dict] = []
    subtotal = Decimal("0.00")

    for idx, rfq_it in enumerate(rfq_items):
        qty = rfq_it.get("quantity")
        try:
            quantity = float(qty) if qty is not None else 1.0
        except Exception:
            quantity = 1.0
        if quantity < 0:
            quantity = 0

        unit_price = float(body.items[idx].unit_price)
        line_total = _money(quantity * unit_price)

        built_items.append({
            "name": rfq_it.get("name") or f"Item {idx+1}",
            "quantity": quantity,
            "unit": rfq_it.get("unit"),
            "unit_price": float(_money(unit_price)),
            "line_total": float(line_total),
        })
        subtotal += line_total

    shipping_cost = _money(body.shipping_cost or 0)
    discount_pct = float(body.discount_pct or 0)
    tax_pct = float(body.tax_pct or 0)

    discounted = subtotal - _money(subtotal * Decimal(discount_pct) / Decimal(100))
    taxable_base = discounted + shipping_cost
    tax_amount = _money(taxable_base * Decimal(tax_pct) / Decimal(100))
    grand_total = taxable_base + tax_amount

    # Upsert (one active quote per vendor per RFQ)
    existing = (
        db.query(RFQQuote)
        .filter(RFQQuote.rfq_id == rfq.id, RFQQuote.vendor_user_id == me.id)
        .first()
    )
    if existing:
        existing.currency = body.currency
        existing.items = built_items
        existing.shipping_cost = shipping_cost
        existing.discount_pct = discount_pct
        existing.tax_pct = tax_pct
        existing.subtotal = _money(subtotal)
        existing.tax_amount = tax_amount
        existing.grand_total = grand_total
        existing.delivery_time_days = body.delivery_time_days
        existing.vendor_notes = body.vendor_notes
        existing.status = "submitted"
        db.commit()
        db.refresh(existing)
        quote = existing
    else:
        quote = RFQQuote(
            rfq_id=rfq.id,
            vendor_user_id=me.id,
            currency=body.currency,
            items=built_items,
            shipping_cost=shipping_cost,
            discount_pct=discount_pct,
            tax_pct=tax_pct,
            subtotal=_money(subtotal),
            tax_amount=tax_amount,
            grand_total=grand_total,
            delivery_time_days=body.delivery_time_days,
            vendor_notes=body.vendor_notes,
            status="submitted",
        )
        db.add(quote)
        db.commit()
        db.refresh(quote)

    # decorate response with vendor company
    vendor_company = profile.company_name if profile else None

    return QuoteOut(
        id=quote.id,
        rfq_id=quote.rfq_id,
        vendor_user_id=quote.vendor_user_id,
        vendor_company=vendor_company,
        currency=quote.currency,
        items=[QuoteItemOut(**it) for it in quote.items],
        shipping_cost=float(quote.shipping_cost),
        discount_pct=float(quote.discount_pct),
        tax_pct=float(quote.tax_pct),
        subtotal=float(quote.subtotal),
        tax_amount=float(quote.tax_amount),
        grand_total=float(quote.grand_total),
        delivery_time_days=quote.delivery_time_days,
        vendor_notes=quote.vendor_notes,
        status=quote.status,
        created_at=quote.created_at,
    )


# -------------------- Shipping company: list quotes for an RFQ --------------------
@router.get("/rfqs/{rfq_id}/quotes", response_model=List[QuoteOut])
def list_quotes_for_rfq(
    rfq_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    rfq = db.get(RFQ, rfq_id)
    if not rfq:
        raise HTTPException(status_code=404, detail="RFQ not found")

    # Only the RFQ owner (shipping company that created it) can view. The
    # agent exemption here was the same placeholder as the one in
    # routes_orders, and it exposed every vendor's bid on every RFQ -- the one
    # thing a sealed quote is for.
    if me.id != rfq.user_id:
        raise HTTPException(status_code=403, detail="Not allowed")

    quotes = (
        db.query(RFQQuote)
        .filter(RFQQuote.rfq_id == rfq_id)
        .order_by(RFQQuote.id.desc())
        .all()
    )

    # attach vendor company
    vendor_profiles = {
        vp.user_id: vp.company_name
        for vp in db.query(VendorProfile).filter(
            VendorProfile.user_id.in_([q.vendor_user_id for q in quotes] or [0])
        )
    }

    out: List[QuoteOut] = []
    for q in quotes:
        out.append(
            QuoteOut(
                id=q.id,
                rfq_id=q.rfq_id,
                vendor_user_id=q.vendor_user_id,
                vendor_company=vendor_profiles.get(q.vendor_user_id),
                currency=q.currency,
                items=[QuoteItemOut(**it) for it in q.items],
                shipping_cost=float(q.shipping_cost),
                discount_pct=float(q.discount_pct),
                tax_pct=float(q.tax_pct),
                subtotal=float(q.subtotal),
                tax_amount=float(q.tax_amount),
                grand_total=float(q.grand_total),
                delivery_time_days=q.delivery_time_days,
                vendor_notes=q.vendor_notes,
                status=q.status,
                created_at=q.created_at,
            )
        )
    return out

# -------------------- (Optional) Vendor: list my quotes --------------------
@router.get("/vendor/quotes", response_model=List[QuoteOut])
def list_my_quotes(
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
    rfq_id: Optional[int] = Query(default=None),
):
    if me.role != "vendor":
        raise HTTPException(status_code=403, detail="Only vendors can view their quotes")

    q = db.query(RFQQuote).filter(RFQQuote.vendor_user_id == me.id)
    if rfq_id:
        q = q.filter(RFQQuote.rfq_id == rfq_id)
    rows = q.order_by(RFQQuote.id.desc()).all()

    profile = db.query(VendorProfile).filter(VendorProfile.user_id == me.id).first()
    company = profile.company_name if profile else None

    out: List[QuoteOut] = []
    for r in rows:
        out.append(
            QuoteOut(
                id=r.id,
                rfq_id=r.rfq_id,
                vendor_user_id=r.vendor_user_id,
                vendor_company=company,
                currency=r.currency,
                items=[QuoteItemOut(**it) for it in r.items],
                shipping_cost=float(r.shipping_cost),
                discount_pct=float(r.discount_pct),
                tax_pct=float(r.tax_pct),
                subtotal=float(r.subtotal),
                tax_amount=float(r.tax_amount),
                grand_total=float(r.grand_total),
                delivery_time_days=r.delivery_time_days,
                vendor_notes=r.vendor_notes,
                status=r.status,
                created_at=r.created_at,
            )
        )
    return out


@router.post("/rfqs/{rfq_id}/quotes/{quote_id}/accept", response_model=OrderOut, status_code=status.HTTP_201_CREATED)
def accept_quote(
    rfq_id: int,
    quote_id: int,
    db: Session = Depends(get_db),
    me: User = Depends(get_current_user),
):
    rfq = db.get(RFQ, rfq_id)
    if not rfq:
        raise HTTPException(404, "RFQ not found")

    # Only RFQ owner (shipping company) or agent can accept
    if me.id != rfq.user_id and me.role != "agent":
        raise HTTPException(403, "Not allowed")

    quote = db.query(RFQQuote).filter(
        RFQQuote.id == quote_id, RFQQuote.rfq_id == rfq_id
    ).first()
    if not quote:
        raise HTTPException(404, "Quote not found")

    # Enforce one accepted per RFQ: either an accepted quote or existing order
    accepted = db.query(RFQQuote).filter(
        RFQQuote.rfq_id == rfq_id, RFQQuote.status == "accepted"
    ).first()
    if accepted and accepted.id != quote_id:
        raise HTTPException(409, "Another quote for this RFQ is already accepted")

    existing_order = db.query(Order).filter(Order.rfq_id == rfq_id).first()
    if existing_order:
        raise HTTPException(409, "Order already created for this RFQ")

    # accept this quote, reject others
    quote.status = "accepted"
    db.query(RFQQuote).filter(
        RFQQuote.rfq_id == rfq_id,
        RFQQuote.id != quote_id,
        RFQQuote.status == "submitted"
    ).update({RFQQuote.status: "rejected"})

    # vendor company
    vp = db.query(VendorProfile).filter(VendorProfile.user_id == quote.vendor_user_id).first()
    vendor_company = vp.company_name if vp else None

    # Create order from quote
    order = Order(
        order_number=f"ORD-{rfq_id}-{quote_id}-{int(datetime.utcnow().timestamp())}",
        rfq_id=rfq_id,
        quote_id=quote_id,
        buyer_user_id=rfq.user_id,
        vendor_user_id=quote.vendor_user_id,
        vendor_company=vendor_company,
        port=rfq.port,
        currency=quote.currency,
        items=quote.items,  # already [{name, quantity, unit, unit_price, line_total}]
        shipping_cost=quote.shipping_cost,
        discount_pct=quote.discount_pct,
        tax_pct=quote.tax_pct,
        subtotal=quote.subtotal,
        tax_amount=quote.tax_amount,
        grand_total=quote.grand_total,
        delivery_time_days=quote.delivery_time_days,
        notes=quote.vendor_notes,
        status="confirmed",
    )
    db.add(order)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Order already exists for this RFQ")

    db.refresh(order)
    db.refresh(quote)

    return OrderOut(
        id=order.id,
        order_number=order.order_number,
        rfq_id=order.rfq_id,
        quote_id=order.quote_id,
        buyer_user_id=order.buyer_user_id,
        vendor_user_id=order.vendor_user_id,
        vendor_company=order.vendor_company,
        port=order.port,
        currency=order.currency,
        items=[QuoteItemOut(**it) for it in order.items or []],
        shipping_cost=float(order.shipping_cost),
        discount_pct=float(order.discount_pct),
        tax_pct=float(order.tax_pct),
        subtotal=float(order.subtotal),
        tax_amount=float(order.tax_amount),
        grand_total=float(order.grand_total),
        delivery_time_days=order.delivery_time_days,
        notes=order.notes,
        status=order.status,
        created_at=order.created_at,
    )
