from sqlalchemy import Column, Integer, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.sql import func

from app.db.base import Base


class NotificationRead(Base):
    __tablename__ = "notification_reads"
    __table_args__ = (
        UniqueConstraint("notification_id", "user_id", name="uq_notification_read"),
    )

    id = Column(Integer, primary_key=True, index=True)
    notification_id = Column(Integer, ForeignKey("notifications.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    read_at = Column(DateTime(timezone=True), server_default=func.now())
