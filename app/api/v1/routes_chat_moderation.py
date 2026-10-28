"""Chat moderation superadmin endpoints.

Superadmin-only CRUD for restricted words, moderation events, and settings.
Mounted at /api/v1/superadmin alongside routes_pricing_controls.
"""

from app.services.collection_pages import page_all
import csv
import io
import logging
from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException, Query, File, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.chat_restricted_word import ChatRestrictedWord
from app.db.models.chat_moderation_event import ChatModerationEvent
from app.db.models.chat_moderation_setting import ChatModerationSetting
from app.db.models.chat import ChatMessage
from app.api.v1.routes_auth import get_current_user
from app.services.chat_moderation import reload_restricted_words

logger = logging.getLogger("heyports.chat_moderation_api")

router = APIRouter()


def verify_superadmin(current_user: User) -> User:
    """Verify user is superadmin. Raises 403 if not."""
    if current_user.role != "superadmin":
        raise HTTPException(status_code=403, detail="Superadmin access required")
    return current_user


def _ensure_settings_row(db: Session) -> ChatModerationSetting:
    """Ensure settings singleton exists."""
    settings = db.query(ChatModerationSetting).filter(ChatModerationSetting.id == 1).first()
    if not settings:
        settings = ChatModerationSetting(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


class RestrictedWordIn(BaseModel):
    word: str = Field(..., min_length=1, max_length=128)
    category: Optional[str] = Field(None, max_length=64)


class RestrictedWordOut(BaseModel):
    id: int
    word: str
    category: Optional[str]
    is_phrase: bool
    is_active: bool
    created_by: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class ModerationEventOut(BaseModel):
    id: int
    port_id: int
    user_id: int
    chat_message_id: Optional[int]
    raw_message: str
    normalized_message: str
    decision: str
    rejected_by: Optional[str]
    reason_code: Optional[str]
    matched_term: Optional[str]
    ai_route: Optional[str]
    ai_model: Optional[str]
    ai_latency_ms: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


class CrewChatToggleIn(BaseModel):
    crew_chat_enabled: bool


class DeleteMessageRequest(BaseModel):
    pass


class ModerationSettingsIn(BaseModel):
    max_message_length: int = Field(default=200, ge=10, le=5000)
    rate_limit_count: int = Field(default=5, ge=1, le=100)
    rate_limit_window_seconds: int = Field(default=10, ge=1, le=300)
    duplicate_window_seconds: int = Field(default=60, ge=1, le=3600)
    language_ai_enabled: bool = Field(default=True)
    moderation_ai_enabled: bool = Field(default=True)
    fail_closed: bool = Field(default=True)
    block_external_links: bool = Field(default=False)
    block_contact_info: bool = Field(default=False)
    block_payment_info: bool = Field(default=False)
    crew_chat_enabled: bool = Field(default=True)


class ModerationSettingsOut(ModerationSettingsIn):
    id: int
    updated_by: Optional[str]
    updated_at: datetime

    class Config:
        from_attributes = True


class DeleteMessageResponse(BaseModel):
    success: bool
    message_id: int


class CSVImportResult(BaseModel):
    total_rows: int
    imported: int
    already_exists: int
    invalid_blank: int


@router.get("/chat/restrictedwords", response_model=List[RestrictedWordOut])
def list_restricted_words(
    search: Optional[str] = Query(None, description="Search by word prefix"),
    category: Optional[str] = Query(None, description="Filter by category"),
    include_inactive: bool = Query(False, description="Include inactive words"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[RestrictedWordOut]:
    """List restricted words with optional search and filters."""
    verify_superadmin(current_user)

    query = db.query(ChatRestrictedWord)

    if not include_inactive:
        query = query.filter(ChatRestrictedWord.is_active)

    if search:
        query = query.filter(ChatRestrictedWord.word.ilike(f"{search}%"))

    if category:
        query = query.filter(ChatRestrictedWord.category == category)

    words = query.order_by(ChatRestrictedWord.created_at.desc()).offset(offset).limit(limit).all()
    return words


@router.post("/chat/restrictedwords", response_model=RestrictedWordOut, status_code=201)
def create_restricted_word(
    word_in: RestrictedWordIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> RestrictedWordOut:
    """Add a new restricted word. Returns 409 if already exists."""
    verify_superadmin(current_user)

    normalized = word_in.word.lower()
    existing = db.query(ChatRestrictedWord).filter(ChatRestrictedWord.word == normalized).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="This word already exists in the restricted words list.",
        )

    is_phrase = " " in normalized
    word = ChatRestrictedWord(
        word=normalized,
        category=word_in.category,
        is_phrase=is_phrase,
        is_active=True,
        created_by=current_user.email,
    )
    db.add(word)
    db.commit()
    db.refresh(word)
    reload_restricted_words(db)
    return word


@router.delete("/chat/restrictedwords/{word_id}", status_code=204)
def delete_restricted_word(
    word_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Deactivate a restricted word (soft delete for history) and reload cache."""
    verify_superadmin(current_user)

    word = db.query(ChatRestrictedWord).filter(ChatRestrictedWord.id == word_id).first()
    if not word:
        raise HTTPException(status_code=404, detail="Word not found")

    word.is_active = False
    db.commit()
    reload_restricted_words(db)


@router.post("/chat/restrictedwords/import", response_model=CSVImportResult)
async def import_restricted_words(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> CSVImportResult:
    """Import restricted words from CSV (single word column, no header).

    Rules applied per spec:
    - Skip header row if detected
    - Trim whitespace from each word
    - Lowercase all words
    - Skip blank rows
    - In-file dedupe: ignore duplicate rows in same file
    - DB dedupe: skip if already exists (without error)
    - Continue processing even if individual rows are invalid
    """
    verify_superadmin(current_user)

    content = await file.read()
    text = content.decode("utf-8")
    reader = csv.reader(io.StringIO(text))

    imported = 0
    already_exists = 0
    invalid_blank = 0
    total_rows = 0
    seen_in_file = set()

    for row_idx, row in enumerate(reader):
        total_rows += 1

        if not row or not row[0].strip():
            invalid_blank += 1
            continue

        word = row[0].strip().lower()

        if not word:
            invalid_blank += 1
            continue

        if word in seen_in_file:
            continue

        seen_in_file.add(word)

        try:
            existing = db.query(ChatRestrictedWord).filter(ChatRestrictedWord.word == word).first()
            if existing:
                already_exists += 1
                continue

            is_phrase = " " in word
            entry = ChatRestrictedWord(
                word=word,
                is_phrase=is_phrase,
                is_active=True,
                created_by=current_user.email,
            )
            db.add(entry)
            imported += 1
        except Exception as e:
            logger.exception(f"Error importing word on row {row_idx}: {e}")
            continue

    db.commit()
    reload_restricted_words(db)

    return CSVImportResult(
        total_rows=total_rows,
        imported=imported,
        already_exists=already_exists,
        invalid_blank=invalid_blank,
    )


@router.get("/chat/restrictedwords/export")
def export_restricted_words(
    include_inactive: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export all restricted words as CSV."""
    verify_superadmin(current_user)

    query = db.query(ChatRestrictedWord)
    if not include_inactive:
        query = query.filter(ChatRestrictedWord.is_active)

    words = page_all(
        query.order_by(ChatRestrictedWord.word),
        collection="routes_chat_moderation.export_restricted_words.0",
        label="Export restricted words",
    )

    output = io.StringIO()
    writer = csv.writer(output)
    for word in words:
        writer.writerow([word.word])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=restricted_words.csv"},
    )


@router.post("/chat/restrictedwords/reload", status_code=204)
def manual_reload_cache(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Manually reload the restricted words cache (after add/delete/import via another instance)."""
    verify_superadmin(current_user)
    reload_restricted_words(db)


@router.get("/chat/moderationevents", response_model=List[ModerationEventOut])
def list_moderation_events(
    port_id: Optional[int] = Query(None),
    decision: Optional[str] = Query(None, description="'allowed' or 'rejected'"),
    rejected_by: Optional[str] = Query(None, description="'level_1', 'language_ai', 'moderation_ai'"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> List[ModerationEventOut]:
    """Query moderation events with optional filters."""
    verify_superadmin(current_user)

    query = db.query(ChatModerationEvent)

    if port_id:
        query = query.filter(ChatModerationEvent.port_id == port_id)

    if decision:
        query = query.filter(ChatModerationEvent.decision == decision)

    if rejected_by:
        query = query.filter(ChatModerationEvent.rejected_by == rejected_by)

    if start_date:
        query = query.filter(ChatModerationEvent.created_at >= start_date)

    if end_date:
        query = query.filter(ChatModerationEvent.created_at <= end_date)

    events = query.order_by(ChatModerationEvent.created_at.desc()).offset(offset).limit(limit).all()
    return events


@router.get("/chat/moderationsettings", response_model=ModerationSettingsOut)
def get_moderation_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ModerationSettingsOut:
    """Get current moderation settings."""
    verify_superadmin(current_user)
    settings = _ensure_settings_row(db)
    return settings


@router.put("/chat/moderationsettings", response_model=ModerationSettingsOut)
def update_moderation_settings(
    settings_in: ModerationSettingsIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ModerationSettingsOut:
    """Update moderation settings."""
    verify_superadmin(current_user)
    settings = _ensure_settings_row(db)

    settings.max_message_length = settings_in.max_message_length
    settings.rate_limit_count = settings_in.rate_limit_count
    settings.rate_limit_window_seconds = settings_in.rate_limit_window_seconds
    settings.duplicate_window_seconds = settings_in.duplicate_window_seconds
    settings.language_ai_enabled = settings_in.language_ai_enabled
    settings.moderation_ai_enabled = settings_in.moderation_ai_enabled
    settings.fail_closed = settings_in.fail_closed
    settings.block_external_links = settings_in.block_external_links
    settings.block_contact_info = settings_in.block_contact_info
    settings.block_payment_info = settings_in.block_payment_info
    settings.crew_chat_enabled = settings_in.crew_chat_enabled
    settings.updated_by = current_user.email
    settings.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(settings)
    return settings


@router.get("/chat/crew-chat-enabled", response_model=dict)
def get_crew_chat_status(
    db: Session = Depends(get_db),
) -> dict:
    """Get crew chat enabled status (public endpoint, no auth required)."""
    settings = db.query(ChatModerationSetting).filter(ChatModerationSetting.id == 1).first()
    enabled = settings.crew_chat_enabled if settings else True
    return {"crew_chat_enabled": enabled}


@router.put("/chat/crew-chat-enabled", response_model=ModerationSettingsOut)
def toggle_crew_chat(
    toggle_in: CrewChatToggleIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ModerationSettingsOut:
    """Toggle crew chat enabled/disabled status."""
    verify_superadmin(current_user)
    settings = _ensure_settings_row(db)
    settings.crew_chat_enabled = toggle_in.crew_chat_enabled
    settings.updated_by = current_user.email
    settings.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(settings)
    return settings


@router.post("/chat/messages/{message_id}/delete", response_model=DeleteMessageResponse)
async def delete_chat_message(
    message_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> DeleteMessageResponse:
    """Delete a chat message (soft delete - sets deleted_at timestamp).

    Broadcasts deletion to all connected users in the port's WebSocket channel.
    """
    verify_superadmin(current_user)

    message = db.query(ChatMessage).filter(ChatMessage.id == message_id).first()
    if not message:
        raise HTTPException(status_code=404, detail="Message not found")

    if message.deleted_at:
        raise HTTPException(status_code=410, detail="Message already deleted")

    port_id = message.port_id
    message.deleted_at = datetime.utcnow()
    db.commit()

    logger.info(f"Message {message_id} deleted by {current_user.email}")

    # Broadcast deletion to connected users via WebSocket
    try:
        from app.api.v1.routes_chat import manager
        await manager.broadcast_system_message(
            port_id,
            "message_deleted",
            {"message_id": message_id}
        )
    except Exception as e:
        logger.warning(f"Failed to broadcast message deletion: {e}")

    return DeleteMessageResponse(success=True, message_id=message_id)
