"""A person asking for their data to be deleted — owner decision, this release.

The owner's answer is specific about what this is and is not:

    "Add a visible 'Request deletion of my data' action... It should initiate
    an authenticated request and confirmation, not immediate destructive
    deletion... Prevent an ordinary account deletion from cascading through
    historical assignments, shore passes, bookings, incidents or SOS."

So this table is a *request*, reviewed by a person. Nothing in the application
erases a row because one exists. The decision is recorded here with its
retention exception, because the same answer requires notifying the requester
"of the result and any lawful retention exception", and a refusal with no
stated reason is not that.

Fulfilment is recorded, not performed. `fulfilled_at` says a human carried out
the approved erasure out of band and when; there is deliberately no code path
that anonymises rows in bulk. Two reasons: the owner's own instruction that
response deadlines and notices be defined with privacy counsel *before launch*,
and the standing instruction that nothing in a deliverable may delete or move
existing production data. A button that quietly rewrote history would satisfy
neither.
"""

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, String, Text, func,
)

from app.db.base import Base


class DataDeletionRequest(Base):
    """One person's request, and what was decided about it.

    The states are kept on the row rather than in a separate audit table: each
    transition has its own who-and-when columns, so the row is already the
    history and there is nothing to keep in step with it.
    """

    __tablename__ = "data_deletion_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # REQUESTED -> UNDER_REVIEW -> APPROVED / REFUSED; APPROVED -> FULFILLED.
    # REQUESTED or UNDER_REVIEW -> CANCELLED, by the requester.
    status = Column(String(32), nullable=False, default="REQUESTED",
                    server_default="REQUESTED")
    # Why they are asking. Optional: a person does not have to justify it.
    reason = Column(Text, nullable=True)

    reviewed_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at = Column(DateTime(timezone=True), nullable=True)
    decision_reason = Column(Text, nullable=True)
    # The lawful reason some data stays, in words the requester is shown.
    # Required on a refusal, and allowed on an approval that is partial.
    retention_note = Column(Text, nullable=True)

    fulfilled_at = Column(DateTime(timezone=True), nullable=True)
    fulfilled_by_user_id = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # What was actually erased or anonymised, written by the person who did it.
    fulfilment_note = Column(Text, nullable=True)

    cancelled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        Index("ix_deletion_requests_queue", "status", "created_at"),
    )
