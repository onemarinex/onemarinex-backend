"""Private, resumable fleet uploads and their immutable import result."""

from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, JSON, func
from app.db.base import Base


class CompanyFleetImport(Base):
    __tablename__ = "company_fleet_imports"
    id = Column(Integer, primary_key=True)
    company_id = Column(
        Integer, ForeignKey("shipping_companies.id"), nullable=False, index=True
    )
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    filename = Column(String(255), nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    columns = Column(JSON, nullable=False)
    rows = Column(JSON, nullable=False)
    mapping = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    status = Column(String(24), nullable=False, default="UPLOADED")
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    imported_at = Column(DateTime(timezone=True), nullable=True)
