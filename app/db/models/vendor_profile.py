from sqlalchemy import Column, Integer, String, Text, ForeignKey, JSON, DateTime, func
from sqlalchemy.orm import relationship
from app.db.base import Base

class VendorProfile(Base):
    __tablename__ = "vendor_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True)

    company_name = Column(String(255), nullable=False)
    contact_person = Column(String(120))
    phone_number = Column(String(32))
    logo_url = Column(Text)

    # For Postgres text[]: ARRAY(String) is fine; defaults stay as empty arrays
    ports_served = Column(JSON, server_default="[]")
    categories_supplied = Column(JSON, server_default="[]")

    trade_license_url = Column(Text)
    gst_vat_url = Column(Text)
    bank_details_url = Column(Text)
    iso_certifications_url = Column(Text)

    verification_status = Column(String(32), default="pending")
    verification_notes = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationship back to user ---
    user = relationship("User", back_populates="vendor_profile")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<VendorProfile id={self.id} user_id={self.user_id}>"
