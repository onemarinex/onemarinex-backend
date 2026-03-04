from sqlalchemy import Column, Integer, String, JSON, DateTime, func
from app.db.base import Base

class PortRule(Base):
    __tablename__ = "port_rules"

    id = Column(Integer, primary_key=True, index=True)
    port_name = Column(String(128), unique=True, index=True, nullable=False)
    # rules: List of {title, description, icon_type}
    rules = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<PortRule port={self.port_name}>"
