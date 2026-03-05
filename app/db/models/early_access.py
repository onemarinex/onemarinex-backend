from sqlalchemy import Column, Integer, String, DateTime, func
from app.db.base import Base

class EarlyAccess(Base):
    __tablename__ = "early_access"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<EarlyAccess email={self.email!r}>"
