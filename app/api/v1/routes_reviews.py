from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime

from app.db.session import get_db
from app.db.models.venue_review import VenueReview
from app.db.models.user import User
from app.api.v1.deps import get_current_user

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class ReviewCreate(BaseModel):
    venue_type: str = Field(..., pattern="^(hotel|restaurant|sightseeing|pub)$")
    venue_id: int
    rating: float = Field(..., ge=1, le=5)
    review_text: Optional[str] = None


class ReviewOut(BaseModel):
    id: int
    venue_type: str
    venue_id: int
    rating: float
    review_text: Optional[str]
    reviewer_name: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/", response_model=ReviewOut)
def submit_review(
    payload: ReviewCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Submit a crew review for a hotel or restaurant."""
    review = VenueReview(
        venue_type=payload.venue_type,
        venue_id=payload.venue_id,
        user_id=current_user.id,
        rating=payload.rating,
        review_text=payload.review_text,
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    return ReviewOut(
        id=review.id,
        venue_type=review.venue_type,
        venue_id=review.venue_id,
        rating=review.rating,
        review_text=review.review_text,
        reviewer_name=current_user.name or current_user.email,
        created_at=review.created_at,
    )


@router.get("/", response_model=List[ReviewOut])
def get_reviews(
    venue_type: str = Query(..., pattern="^(hotel|restaurant|sightseeing|pub)$"),
    venue_id: int = Query(...),
    db: Session = Depends(get_db),
):
    """Get all reviews for a specific hotel or restaurant."""
    reviews = (
        db.query(VenueReview)
        .filter(
            VenueReview.venue_type == venue_type,
            VenueReview.venue_id == venue_id,
        )
        .order_by(VenueReview.created_at.desc())
        .all()
    )

    results = []
    for r in reviews:
        user = db.query(User).filter(User.id == r.user_id).first()
        results.append(
            ReviewOut(
                id=r.id,
                venue_type=r.venue_type,
                venue_id=r.venue_id,
                rating=r.rating,
                review_text=r.review_text,
                reviewer_name=user.name or user.email if user else "Crew Member",
                created_at=r.created_at,
            )
        )
    return results
