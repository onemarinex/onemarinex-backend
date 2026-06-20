import os
import qrcode
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from typing import List, Optional
from app.db.session import get_db
from app.db.models.vendors import Vendors

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
    search: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Vendors).filter(Vendors.category.ilike("restaurant"), Vendors.status == "Active")
    if port_id is not None:
        query = query.filter(Vendors.port_id == port_id)
    if max_dist is not None:
        query = query.filter(Vendors.distance_from_port <= max_dist)
    if search is not None:
        query = query.filter(Vendors.name.ilike(f"%{search}%"))
        
    vendors = query.all()
    results = []
    for v in vendors:
        other = v.other_information or {}
        price = other.get("price_per_person", 0.0)
        if max_price is not None and price > max_price:
            continue
            
        results.append({
            "id": v.id,
            "port_id": v.port_id,
            "name": v.name,
            "location_name": v.location_name,
            "distance_from_port": v.distance_from_port,
            "rating": v.rating,
            "price_per_person": price,
            "timings": other.get("timings", "10:00 AM - 10:00 PM"),
            "service_type": other.get("category_cuisines") or other.get("service_type") or "Standard",
            "category_cuisines": other.get("category_cuisines") or other.get("service_type") or "Standard",
            "phone": v.phone,
            "lat": v.lat,
            "lng": v.lng,
            "image_url": v.images[0] if (v.images and len(v.images) > 0) else "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?q=80&w=2070&auto=format&fit=crop",
            "description": other.get("about") or other.get("description") or "",
            "about": other.get("about") or other.get("description") or "",
            "facilities": other.get("facilities", []),
            "address": v.location_name,
        })
    return results


@router.get("/{id}")
def get_restaurant(id: int, db: Session = Depends(get_db)):
    v = db.query(Vendors).filter(Vendors.id == id, Vendors.category.ilike("restaurant")).first()
    if not v:
        raise HTTPException(status_code=404, detail="Restaurant not found")
    other = v.other_information or {}
    return {
        "id": v.id,
        "port_id": v.port_id,
        "name": v.name,
        "location_name": v.location_name,
        "distance_from_port": v.distance_from_port,
        "rating": v.rating,
        "price_per_person": other.get("price_per_person", 0.0),
        "timings": other.get("timings", "10:00 AM - 10:00 PM"),
        "service_type": other.get("category_cuisines") or other.get("service_type") or "Standard",
        "category_cuisines": other.get("category_cuisines") or other.get("service_type") or "Standard",
        "phone": v.phone,
        "lat": v.lat,
        "lng": v.lng,
        "image_url": v.images[0] if (v.images and len(v.images) > 0) else "https://images.unsplash.com/photo-1517248135467-4c7edcad34c4?q=80&w=2070&auto=format&fit=crop",
        "description": other.get("about") or other.get("description") or "",
        "about": other.get("about") or other.get("description") or "",
        "facilities": other.get("facilities", []),
        "address": v.location_name,
    }


# Generate QR code for a restaurant
@router.get("/{id}/qr")
def get_restaurant_qr(id: int, db: Session = Depends(get_db)):
    v = db.query(Vendors).filter(Vendors.id == id, Vendors.category.ilike("restaurant")).first()
    if not v:
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
