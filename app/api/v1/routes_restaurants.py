import os
import qrcode
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List, Optional
from app.db.session import get_db
from app.db.models.restaurant import Restaurant

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


@router.get("/")
def get_restaurants(
    port_id: Optional[int] = None,
    max_dist: Optional[float] = None,
    max_price: Optional[float] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Restaurant)
    if port_id is not None:
        query = query.filter(Restaurant.port_id == port_id)
    if max_dist is not None:
        query = query.filter(Restaurant.distance_from_port <= max_dist)
    if max_price is not None:
        query = query.filter(Restaurant.price_per_person <= max_price)
    
    return query.all()


@router.get("/{id}")
def get_restaurant(id: int, db: Session = Depends(get_db)):
    restaurant = db.query(Restaurant).filter(Restaurant.id == id).first()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    return restaurant


# Generate QR code for a restaurant
@router.get("/{id}/qr")
def get_restaurant_qr(id: int, db: Session = Depends(get_db)):
    restaurant = db.query(Restaurant).filter(Restaurant.id == id).first()
    if not restaurant:
        raise HTTPException(status_code=404, detail="Restaurant not found")

    cache_path = f"{QR_DIR}/restaurant_{id}.png"
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            png_bytes = f.read()
    else:
        url = f"{FRONTEND_BASE_URL}/review?type=restaurant&id={id}"
        png_bytes = generate_qr_png(url)
        with open(cache_path, "wb") as f:
            f.write(png_bytes)

    return Response(content=png_bytes, media_type="image/png")

