import os
import qrcode
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.sightseeing import Sightseeing
from typing import List, Optional

router = APIRouter()

FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:5173")
QR_DIR = "uploads/qrcodes"
os.makedirs(QR_DIR, exist_ok=True)


def generate_qr_png(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# Get all sightseeing locations
@router.get("/")
def get_sightseeing(
    port_id: Optional[int] = None,
    max_dist: Optional[float] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Sightseeing)
    if port_id is not None:
        query = query.filter(Sightseeing.port_id == port_id)
    if max_dist is not None:
        query = query.filter(Sightseeing.distance_from_port <= max_dist)
    if min_price is not None:
        query = query.filter(Sightseeing.price_per_person >= min_price)
    if max_price is not None:
        query = query.filter(Sightseeing.price_per_person <= max_price)
    
    return query.all()

# Get sightseeing based on filters
@router.get("/filters")
def get_sightseeing_by_filters(
    port_id: Optional[int] = None,
    max_dist: Optional[float] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_rating: Optional[float] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Sightseeing)
    if port_id is not None:
        query = query.filter(Sightseeing.port_id == port_id)
    if max_dist is not None:
        query = query.filter(Sightseeing.distance_from_port <= max_dist)
    if min_price is not None:
        query = query.filter(Sightseeing.price_per_person >= min_price)
    if max_price is not None:
        query = query.filter(Sightseeing.price_per_person <= max_price)
    if min_rating is not None:
        query = query.filter(Sightseeing.rating >= min_rating)
    
    return query.all()

# Get sightseeing by id
@router.get("/{id}")
def get_single_sightseeing(id: int, db: Session = Depends(get_db)):
    item = db.query(Sightseeing).filter(Sightseeing.id == id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Sightseeing not found")
    return item


# Generate QR code for a sightseeing location
@router.get("/{id}/qr")
def get_sightseeing_qr(id: int, db: Session = Depends(get_db)):
    item = db.query(Sightseeing).filter(Sightseeing.id == id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Sightseeing not found")

    cache_path = f"{QR_DIR}/sightseeing_{id}.png"
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            png_bytes = f.read()
    else:
        url = f"{FRONTEND_BASE_URL}/review?type=sightseeing&id={id}"
        png_bytes = generate_qr_png(url)
        with open(cache_path, "wb") as f:
            f.write(png_bytes)

    return Response(content=png_bytes, media_type="image/png")
