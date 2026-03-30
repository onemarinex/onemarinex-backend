from sqlalchemy import Column, Integer, String, DateTime, func
from app.db.base import Base


class PortServiceRequest(Base):
    __tablename__ = "port_service_requests"

    id = Column(Integer, primary_key=True, index=True)
    port_code = Column(String(100), nullable=False, index=True)
    email = Column(String(255), nullable=True, index=True)
    request_type = Column(String(50), nullable=False, default="service_request")  # 'service_request' or 'notify_me'
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<PortServiceRequest port={self.port_code!r} email={self.email!r}>"
