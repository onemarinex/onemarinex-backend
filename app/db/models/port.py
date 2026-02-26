from sqlalchemy import Column, Integer, String, Boolean, DateTime, func
from app.db.base import Base

class Port(Base):
    __tablename__ = "ports"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    code = Column(String(100), unique=True, index=True, nullable=False) # e.g. port_vishakapatnam
    is_active = Column(Boolean, default=True)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<Port id={self.id} name={self.name} code={self.code}>"
