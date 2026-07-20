from sqlalchemy import Column, Integer, String, JSON, DateTime, func
from app.db.base import Base

class PortRule(Base):
    __tablename__ = "port_rules"

    id = Column(Integer, primary_key=True, index=True)
    port_name = Column(String(128), unique=True, index=True, nullable=False)
    rules = Column(JSON, nullable=True)
    opening_time = Column(String(8), nullable=True)
    closing_time = Column(String(8), nullable=True)
    working_days = Column(JSON, nullable=True)  # List of weekday abbreviations: ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    advance_booking_buffer_minutes = Column(Integer, default=30)
    contact_email = Column(String(255), nullable=True)
    helpline_number = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<PortRule port={self.port_name}>"
