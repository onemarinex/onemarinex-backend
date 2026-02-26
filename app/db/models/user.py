from sqlalchemy import Column, Integer, String, DateTime, func, UniqueConstraint
from sqlalchemy.orm import relationship
from app.db.base import Base

class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=True)
    email = Column(String(255), nullable=False, index=True)
    mobile_number = Column(String(32), nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(32), nullable=False, server_default="crew")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    # --- relationships ---
    # One user -> many RFQs
    rfqs = relationship(
        "RFQ",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # One user -> one vendor profile
    vendor_profile = relationship(
        "VendorProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # One user -> one crew profile
    crew_profile = relationship(
        "CrewProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # One user -> one client profile
    client_profile = relationship(
        "ClientProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # One user -> one agent profile
    agent_profile = relationship(
        "AgentProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # One user -> one aggregator profile
    aggregator_profile = relationship(
        "AggregatorProfile",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User id={self.id} email={self.email!r} role={self.role!r}>"
