import os
import qrcode
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.db.models.hotels import Hotel
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


# Get all hotels
@router.get("/")
def get_hotels(
    max_dist: Optional[float] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Hotel)
    if max_dist is not None:
        query = query.filter(Hotel.distance_from_port <= max_dist)
    if min_price is not None:
        query = query.filter(Hotel.price_per_night >= min_price)
    if max_price is not None:
        query = query.filter(Hotel.price_per_night <= max_price)
    
    hotels = query.all()
    return hotels

# Get hotels based on filters
@router.get("/filters")
def get_hotels_by_filters(
    max_dist: Optional[float] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_rating: Optional[float] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Hotel)
    if max_dist is not None:
        query = query.filter(Hotel.distance_from_port <= max_dist)
    if min_price is not None:
        query = query.filter(Hotel.price_per_night >= min_price)
    if max_price is not None:
        query = query.filter(Hotel.price_per_night <= max_price)
    if min_rating is not None:
        query = query.filter(Hotel.rating >= min_rating)
    
    hotels = query.all()
    return hotels

# Get hotel by id
@router.get("/{id}")
def get_hotel(id: int, db: Session = Depends(get_db)):
    hotel = db.query(Hotel).filter(Hotel.id == id).first()
    if not hotel:
        raise HTTPException(status_code=404, detail="Hotel not found")
    return hotel


# Generate QR code for a hotel
@router.get("/{id}/qr")
def get_hotel_qr(id: int, db: Session = Depends(get_db)):
    hotel = db.query(Hotel).filter(Hotel.id == id).first()
    if not hotel:
        raise HTTPException(status_code=404, detail="Hotel not found")

    cache_path = f"{QR_DIR}/hotel_{id}.png"
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            png_bytes = f.read()
    else:
        url = f"{FRONTEND_BASE_URL}/review?type=hotel&id={id}"
        png_bytes = generate_qr_png(url)
        with open(cache_path, "wb") as f:
            f.write(png_bytes)

    return Response(content=png_bytes, media_type="image/png")
