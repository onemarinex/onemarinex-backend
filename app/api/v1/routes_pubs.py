import os
import qrcode
import io
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models.pub import Pub
from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User

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


class PubBase(BaseModel):
    name: str
    location_name: str
    distance_from_port: float
    rating: float
    price_per_person: float
    timings: str
    service_type: str
    popular_for: Optional[List[str]] = None
    phone: Optional[str] = None
    lat: float
    lng: float
    image_url: Optional[str] = None
    description: Optional[str] = None

class PubOut(PubBase):
    id: int

    class Config:
        from_attributes = True

@router.get("/", response_model=List[PubOut])
def get_pubs(
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Pub)
    if port_id:
        query = query.filter(Pub.port_id == port_id)
    pubs = query.all()
    return pubs

@router.get("/{pub_id}", response_model=PubOut)
def get_pub_details(
    pub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    pub = db.query(Pub).filter(Pub.id == pub_id).first()
    if not pub:
        raise HTTPException(status_code=404, detail="Pub not found")
    return pub

# Admin endpoint to seed data (for development)
@router.post("/seed", status_code=status.HTTP_201_CREATED)
def seed_pubs(
    db: Session = Depends(get_db),
    # current_user: User = Depends(get_current_user) # In production we'd protect this
):
    # Check if data already exists
    if db.query(Pub).first():
        return {"message": "Data already seeded"}
    
    dummy_pubs = [
        {
            "name": "Pakka local",
            "location_name": "Kondapur, 3.6 KM",
            "distance_from_port": 3.6,
            "rating": 4.3,
            "price_per_person": 69.0,
            "timings": "11am to 12am",
            "service_type": "Andhra, Biriyani",
            "popular_for": ["Andhra Food", "Lively", "Local"],
            "phone": "+91 9857297638",
            "lat": 17.4622,
            "lng": 78.3568,
            "image_url": "https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=400&q=80",
            "description": "A popular spot for authentic Andhra cuisine and a lively local atmosphere."
        },
        {
            "name": "Masala Republic by Dadu's",
            "location_name": "Kondapur, 7.8 KM",
            "distance_from_port": 7.8,
            "rating": 4.3,
            "price_per_person": 75.0,
            "timings": "8pm to 12am",
            "service_type": "Asian, Continental",
            "popular_for": ["Vegetarian", "Elegant", "Fusion"],
            "phone": "+91 98572976382",
            "lat": 17.4504,
            "lng": 78.3808,
            "image_url": "https://images.unsplash.com/photo-1552566626-52f8b828add9?auto=format&fit=crop&w=400&q=80",
            "description": "Masala Republic offers a unique vegetarian fine-dining experience with Asian and Continental fusion."
        },
        {
            "name": "Flechazo GOLD",
            "location_name": "Road Number 10, Jubilee Hills",
            "distance_from_port": 9.2,
            "rating": 4.3,
            "price_per_person": 85.0,
            "timings": "12pm to 11pm",
            "service_type": "Mediterranean, North Indian",
            "popular_for": ["Buffet", "Celebration", "Fusion"],
            "phone": "+91 98572976383",
            "lat": 17.4334,
            "lng": 78.4116,
            "image_url": "https://images.unsplash.com/photo-1470337458703-46ad1756a187?auto=format&fit=crop&w=400&q=80",
            "description": "A celebration of food from the Mediterranean and North India, known for its extensive buffet."
        }
    ]
    
    for pub_data in dummy_pubs:
        db_pub = Pub(**pub_data)
        db.add(db_pub)
    
    db.commit()
    return {"message": "Pubs seeded successfully"}


# Generate QR code for a pub
@router.get("/{id}/qr")
def get_pub_qr(id: int, db: Session = Depends(get_db)):
    pub = db.query(Pub).filter(Pub.id == id).first()
    if not pub:
        raise HTTPException(status_code=404, detail="Pub not found")

    cache_path = f"{QR_DIR}/pub_{id}.png"
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            png_bytes = f.read()
    else:
        url = f"{FRONTEND_BASE_URL}/review?type=pub&id={id}"
        png_bytes = generate_qr_png(url)
        with open(cache_path, "wb") as f:
            f.write(png_bytes)

    return Response(content=png_bytes, media_type="image/png")
