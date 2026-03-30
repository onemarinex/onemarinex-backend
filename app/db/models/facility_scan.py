from sqlalchemy import Column, Integer, String, DateTime, ForeignKey
from sqlalchemy.sql import func

from app.db.base import Base

class FacilityScan(Base):
    __tablename__ = "facility_scans"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    port_code = Column(String)
    scanned_data = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
