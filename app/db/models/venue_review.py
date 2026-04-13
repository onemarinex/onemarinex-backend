from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Text
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.base import Base


class VenueReview(Base):
    __tablename__ = "venue_reviews"

    id = Column(Integer, primary_key=True, index=True)
    venue_type = Column(String(32), nullable=False)   # "hotel" or "restaurant"
    venue_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    rating = Column(Float, nullable=False)           # 1.0 – 5.0
    review_text = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    user = relationship("User", foreign_keys=[user_id])
