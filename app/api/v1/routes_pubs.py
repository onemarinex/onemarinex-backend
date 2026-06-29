import os
import qrcode
import io
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime
from pydantic import BaseModel

from app.db.session import get_db
from app.db.models.vendors import Vendors
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
    pub_type: Optional[str] = None
    category: Optional[str] = None
    best_for: Optional[str] = None
    facilities: Optional[List[str]] = None
    about: Optional[str] = None

class PubOut(BaseModel):
    id: int
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
    image_url: List[str] = []
    description: Optional[str] = None
    pub_type: Optional[str] = None
    category: Optional[str] = None
    best_for: Optional[str] = None
    facilities: Optional[List[str]] = None
    about: Optional[str] = None

    class Config:
        from_attributes = True

@router.get("/", response_model=List[PubOut])
def get_pubs(
    port_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Vendors).filter(Vendors.category.ilike("pub"), Vendors.status == "Active")
    if port_id:
        query = query.filter(Vendors.port_id == port_id)
    vendors = query.all()
    results = []
    for v in vendors:
        other = v.other_information or {}
        results.append(PubOut(
            id=v.id,
            name=v.name,
            location_name=v.location_name,
            distance_from_port=v.distance_from_port,
            rating=v.rating,
            price_per_person=other.get("price_per_person", 0.0),
            timings=other.get("timings", ""),
            service_type=other.get("category", "Standard"),
            popular_for=other.get("facilities", []),
            phone=v.phone,
            lat=v.lat,
            lng=v.lng,
            image_url=v.images if (v.images and len(v.images) > 0) else ["https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=400&q=80"],
            description=other.get("about") or other.get("description") or "",
            pub_type=other.get("pub_type", ""),
            category=other.get("category", ""),
            best_for=other.get("best_for", ""),
            facilities=other.get("facilities", []),
            about=other.get("about") or other.get("description") or ""
        ))
    return results

@router.get("/{pub_id}", response_model=PubOut)
def get_pub_details(
    pub_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    v = db.query(Vendors).filter(Vendors.id == pub_id, Vendors.category.ilike("pub")).first()
    if not v:
        raise HTTPException(status_code=404, detail="Pub not found")
    other = v.other_information or {}
    return PubOut(
        id=v.id,
        name=v.name,
        location_name=v.location_name,
        distance_from_port=v.distance_from_port,
        rating=v.rating,
        price_per_person=other.get("price_per_person", 0.0),
        timings=other.get("timings", ""),
        service_type=other.get("category", "Standard"),
        popular_for=other.get("facilities", []),
        phone=v.phone,
        lat=v.lat,
        lng=v.lng,
        image_url=v.images if (v.images and len(v.images) > 0) else ["https://images.unsplash.com/photo-1514933651103-005eec06c04b?auto=format&fit=crop&w=400&q=80"],
        description=other.get("about") or other.get("description") or "",
        pub_type=other.get("pub_type", ""),
        category=other.get("category", ""),
        best_for=other.get("best_for", ""),
        facilities=other.get("facilities", []),
        about=other.get("about") or other.get("description") or ""
    )

# Generate QR code for a pub
@router.get("/{id}/qr")
def get_pub_qr(id: int, db: Session = Depends(get_db)):
    v = db.query(Vendors).filter(Vendors.id == id, Vendors.category.ilike("pub")).first()
    if not v:
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
