from app.services.collection_pages import page_all
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, HTTPException
from jose import jwt
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.models.chat import ChatMessage
from app.db.models.port import Port
from app.db.models.user import User
from app.db.session import get_db
from app.services.chat_moderation import moderate_message
from app.services.moderation_logger import ModerationLogger
from app.services.moderation_policy import PolicyVerdict, Decision, Category
from app.utils.text_normalization import normalize
from app.api.v1.routes_auth import get_current_user

router = APIRouter()
logger = logging.getLogger("heyports.chat")


def _moderation_result_to_verdict(mod_result):
    """Convert ModerationResult to PolicyVerdict for logging."""
    if mod_result.rejected:
        decision = Decision.REJECT
        if mod_result.reason_code == "duplicate":
            category = Category.DUPLICATE
        elif mod_result.reason_code == "rate_limited":
            category = Category.FLOOD
        elif mod_result.reason_code == "restricted_word":
            category = Category.PROFANITY
        elif mod_result.reason_code == "contact_info":
            category = Category.CONTACT_INFO
        elif mod_result.reason_code == "payment_info":
            category = Category.PAYMENT_INFO
        elif mod_result.reason_code == "external_link":
            category = Category.EXTERNAL_LINK
        elif mod_result.reason_code == "language_violation":
            category = Category.LANGUAGE_VIOLATION
        elif mod_result.reason_code == "guidelines_violation":
            category = Category.HARASSMENT
        else:
            category = Category.CLEAN
    else:
        decision = Decision.ALLOW
        category = Category.CLEAN

    return PolicyVerdict(
        decision=decision,
        category=category,
        confidence=0.95,
        reason=mod_result.reason_code or "No reason provided",
        level=mod_result.rejected_by or "unknown"
    )

MAX_MESSAGE_LENGTH = 4000
MESSAGE_EDIT_WINDOW = timedelta(hours=1)


def _display_name(user: Optional[User]) -> str:
    if not user:
        return "Unknown User"
    return user.name or user.email


def _as_utc(value: datetime) -> datetime:
    """Normalize timestamps from both timezone-aware and legacy naive rows."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _edit_expires_at(message: ChatMessage) -> Optional[datetime]:
    if not message.created_at:
        return None
    return _as_utc(message.created_at) + MESSAGE_EDIT_WINDOW


def _message_is_editable(
    message: ChatMessage,
    now: Optional[datetime] = None,
) -> bool:
    expires_at = _edit_expires_at(message)
    if message.deleted_at or not expires_at:
        return False
    current_time = _as_utc(now) if now else datetime.now(timezone.utc)
    return current_time < expires_at


def _serialize_message(message: ChatMessage) -> dict:
    """Return the canonical message shape used by history and WebSockets."""
    edit_expires_at = _edit_expires_at(message)
    reply = message.reply_to
    reply_payload = None
    if reply:
        reply_payload = {
            "id": reply.id,
            "user_id": reply.user_id,
            "name": _display_name(reply.user),
            "message": "" if reply.deleted_at else reply.message,
            "deleted": bool(reply.deleted_at),
        }

    return {
        "id": message.id,
        "user_id": message.user_id,
        "name": _display_name(message.user),
        "role": message.user.role if message.user else "user",
        "message": "" if message.deleted_at else message.message,
        "created_at": message.created_at.isoformat() if message.created_at else None,
        "edit_expires_at": edit_expires_at.isoformat() if edit_expires_at else None,
        "edited_at": message.edited_at.isoformat() if message.edited_at else None,
        "deleted_at": message.deleted_at.isoformat() if message.deleted_at else None,
        "deleted": bool(message.deleted_at),
        "reply_to": reply_payload,
    }


def _message_query(db: Session):
    return db.query(ChatMessage).options(
        joinedload(ChatMessage.user),
        joinedload(ChatMessage.reply_to).joinedload(ChatMessage.user),
    )


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[int, Set[WebSocket]] = {}
        self.active_users: Dict[int, Set[int]] = {}

    async def connect(self, websocket: WebSocket, port_id: int, user_id: int):
        await websocket.accept()
        if port_id not in self.active_connections:
            self.active_connections[port_id] = set()
            self.active_users[port_id] = set()

        self.active_connections[port_id].add(websocket)
        self.active_users[port_id].add(user_id)
        await self.broadcast_system_message(
            port_id,
            "user_joined",
            {"online_count": self.get_online_count(port_id)},
        )

    def disconnect(self, websocket: WebSocket, port_id: int, user_id: int):
        connections = self.active_connections.get(port_id)
        if not connections:
            return

        connections.discard(websocket)
        self.active_users[port_id].discard(user_id)
        if not connections:
            del self.active_connections[port_id]
            del self.active_users[port_id]

    def get_online_count(self, port_id: int) -> int:
        return len(self.active_users.get(port_id, set()))

    async def broadcast_event(self, port_id: int, event_type: str, data: dict):
        payload = json.dumps({"type": event_type, "data": data})
        for connection in list(self.active_connections.get(port_id, set())):
            try:
                await connection.send_text(payload)
            except Exception:
                logger.exception("Failed to broadcast %s to port %s", event_type, port_id)

    async def broadcast_system_message(self, port_id: int, event_type: str, data: dict):
        payload = json.dumps({"type": "system", "event": event_type, "data": data})
        for connection in list(self.active_connections.get(port_id, set())):
            try:
                await connection.send_text(payload)
            except Exception:
                logger.exception("Failed to broadcast system event to port %s", port_id)


manager = ConnectionManager()


@router.get("/channels")
def get_channels(db: Session = Depends(get_db)):
    """Fetch active ports as chat channels."""
    ports = page_all(
        db.query(Port).filter(Port.is_active == True),
        collection="routes_chat.get_channels.0",
        label="Channels",
    )  # noqa: E712
    return [
        {
            "id": port.id,
            "name": port.name,
            "code": port.code,
            "online_count": manager.get_online_count(port.id),
        }
        for port in ports
    ]


@router.get("/{port_id}/messages")
def get_chat_history(
    port_id: int,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """Fetch the latest messages for a channel, oldest first. Excludes deleted messages."""
    messages = (
        _message_query(db)
        .filter(ChatMessage.port_id == port_id, ChatMessage.deleted_at.is_(None))
        .order_by(ChatMessage.created_at.desc())
        .limit(min(max(limit, 1), 100))
        .all()
    )
    return [_serialize_message(message) for message in messages[::-1]]


@router.get("/moderation-config")
def get_moderation_config(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get moderation config for frontend (authenticated crew only).

    Only crew members can access community chat moderation config.
    """
    if current_user.role != "crew":
        raise HTTPException(status_code=403, detail="Crew access required for community chat")

    from app.db.models.chat_moderation_setting import ChatModerationSetting
    settings = db.query(ChatModerationSetting).filter(ChatModerationSetting.id == 1).first()
    if not settings:
        settings = ChatModerationSetting(id=1)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return {
        "max_message_length": settings.max_message_length,
    }


# --- WebSocket Endpoint ---

def get_user_from_token(token: str, db: Session) -> Optional[User]:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email = payload.get("sub")
        if not email:
            return None
        return db.query(User).filter(User.email == email).first()
    except Exception:
        logger.exception("WebSocket token decode failed")
        return None


async def _send_action_error(websocket: WebSocket, message: str, code: str):
    await websocket.send_json(
        {"type": "error", "data": {"message": message, "code": code}}
    )


def _owned_message(
    db: Session,
    port_id: int,
    message_id: object,
) -> Optional[ChatMessage]:
    if not isinstance(message_id, int):
        return None
    return (
        _message_query(db)
        .filter(
            ChatMessage.id == message_id,
            ChatMessage.port_id == port_id,
        )
        .first()
    )


@router.websocket("/ws/{port_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    port_id: int,
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    user = get_user_from_token(token, db)
    if not user:
        await websocket.close(code=1008)
        return

    port = (
        db.query(Port)
        .filter(Port.id == port_id, Port.is_active == True)  # noqa: E712
        .first()
    )
    if not port:
        logger.error(f"🔴 PORT {port_id} NOT FOUND")
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, port_id, user.id)
    logger.info("User %s connected to chat port %s", user.id, port_id)

    try:
        while True:
            raw_data = await websocket.receive_text()
            try:
                data = json.loads(raw_data)
            except json.JSONDecodeError:
                await _send_action_error(websocket, "Invalid message payload.", "invalid_json")
                continue

            action = data.get("type", "message.create")
            try:
                if action == "message.create":
                    text = str(data.get("message", "")).strip()
                    if not text:
                        continue
                    if len(text) > MAX_MESSAGE_LENGTH:
                        await _send_action_error(
                            websocket,
                            f"Messages can be up to {MAX_MESSAGE_LENGTH} characters.",
                            "message_too_long",
                        )
                        continue

                    reply_to_id = data.get("reply_to_id")
                    reply_to = None
                    if reply_to_id is not None:
                        reply_to = _owned_message(db, port_id, reply_to_id)
                        if not reply_to or reply_to.deleted_at:
                            await _send_action_error(
                                websocket,
                                "The message you are replying to is no longer available.",
                                "reply_unavailable",
                            )
                            continue

                    mod_result = await moderate_message(db, user.id, port_id, text)

                    if mod_result.rejected:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "data": {
                                    "message": "Your message couldn't be sent because it violates our community guidelines.",
                                    "code": mod_result.code,
                                    "tier": mod_result.rejected_by,
                                },
                            }
                        )
                        normalized_text = normalize(text)
                        policy_verdict = _moderation_result_to_verdict(mod_result)
                        ModerationLogger.log_event(
                            db=db,
                            user_id=user.id,
                            port_id=port_id,
                            raw_message=text,
                            normalized_message=normalized_text,
                            policy_verdict=policy_verdict,
                            matched_term=mod_result.matched_term,
                            rejected_by=mod_result.rejected_by,
                            reason_code=mod_result.reason_code,
                            ai_route=mod_result.ai_route,
                            ai_model=mod_result.ai_model,
                            ai_latency_ms=mod_result.ai_latency_ms,
                        )
                        continue

                    new_message = ChatMessage(
                        port_id=port_id,
                        user_id=user.id,
                        message=text,
                        reply_to_id=reply_to.id if reply_to else None,
                    )
                    db.add(new_message)
                    db.commit()
                    db.refresh(new_message)

                    normalized_text = normalize(text)
                    policy_verdict = _moderation_result_to_verdict(mod_result)
                    ModerationLogger.log_event(
                        db=db,
                        user_id=user.id,
                        port_id=port_id,
                        raw_message=text,
                        normalized_message=normalized_text,
                        policy_verdict=policy_verdict,
                        matched_term=mod_result.matched_term,
                        rejected_by=mod_result.rejected_by,
                        reason_code=mod_result.reason_code,
                        ai_route=mod_result.ai_route,
                        ai_model=mod_result.ai_model,
                        ai_latency_ms=mod_result.ai_latency_ms,
                        chat_message_id=new_message.id,
                    )

                    payload = _serialize_message(
                        _message_query(db).filter(ChatMessage.id == new_message.id).one()
                    )
                    await manager.broadcast_event(port_id, "chat_message", payload)

                elif action == "message.edit":
                    message = _owned_message(db, port_id, data.get("message_id"))
                    if not message or message.deleted_at:
                        await _send_action_error(
                            websocket, "This message is no longer available.", "message_not_found"
                        )
                        continue
                    if message.user_id != user.id:
                        await _send_action_error(
                            websocket, "You can only edit your own messages.", "forbidden"
                        )
                        continue
                    if not _message_is_editable(message):
                        await _send_action_error(
                            websocket,
                            "Messages can only be edited within one hour of sending.",
                            "edit_window_expired",
                        )
                        continue

                    text = str(data.get("message", "")).strip()
                    if not text:
                        await _send_action_error(
                            websocket, "A message cannot be empty.", "empty_message"
                        )
                        continue
                    if len(text) > MAX_MESSAGE_LENGTH:
                        await _send_action_error(
                            websocket,
                            f"Messages can be up to {MAX_MESSAGE_LENGTH} characters.",
                            "message_too_long",
                        )
                        continue

                    mod_result = await moderate_message(db, user.id, port_id, text)
                    if mod_result.rejected:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "data": {
                                    "message": "Your edit couldn't be saved because it violates our community guidelines.",
                                    "code": mod_result.code,
                                    "tier": mod_result.rejected_by,
                                },
                            }
                        )
                        normalized_text = normalize(text)
                        policy_verdict = _moderation_result_to_verdict(mod_result)
                        ModerationLogger.log_event(
                            db=db,
                            user_id=user.id,
                            port_id=port_id,
                            raw_message=text,
                            normalized_message=normalized_text,
                            policy_verdict=policy_verdict,
                            matched_term=mod_result.matched_term,
                            rejected_by=mod_result.rejected_by,
                            reason_code=mod_result.reason_code,
                            ai_route=mod_result.ai_route,
                            ai_model=mod_result.ai_model,
                            ai_latency_ms=mod_result.ai_latency_ms,
                            chat_message_id=message.id,
                        )
                        continue

                    message.message = text
                    message.edited_at = datetime.now(timezone.utc)
                    db.commit()

                    normalized_text = normalize(text)
                    policy_verdict = _moderation_result_to_verdict(mod_result)
                    ModerationLogger.log_event(
                        db=db,
                        user_id=user.id,
                        port_id=port_id,
                        raw_message=text,
                        normalized_message=normalized_text,
                        policy_verdict=policy_verdict,
                        matched_term=mod_result.matched_term,
                        rejected_by=mod_result.rejected_by,
                        reason_code=mod_result.reason_code,
                        ai_route=mod_result.ai_route,
                        ai_model=mod_result.ai_model,
                        ai_latency_ms=mod_result.ai_latency_ms,
                        chat_message_id=message.id,
                    )

                    payload = _serialize_message(
                        _message_query(db).filter(ChatMessage.id == message.id).one()
                    )
                    await manager.broadcast_event(port_id, "message_updated", payload)

                elif action == "message.delete":
                    message = _owned_message(db, port_id, data.get("message_id"))
                    if not message or message.deleted_at:
                        await _send_action_error(
                            websocket, "This message is already deleted.", "message_not_found"
                        )
                        continue
                    if message.user_id != user.id:
                        await _send_action_error(
                            websocket, "You can only delete your own messages.", "forbidden"
                        )
                        continue

                    message.message = ""
                    message.deleted_at = datetime.now(timezone.utc)
                    db.commit()
                    payload = _serialize_message(
                        _message_query(db).filter(ChatMessage.id == message.id).one()
                    )
                    await manager.broadcast_event(port_id, "message_deleted", payload)

                else:
                    await _send_action_error(
                        websocket, "Unsupported chat action.", "unsupported_action"
                    )
            except Exception:
                db.rollback()
                logger.exception("Chat action %s failed for user %s", action, user.id)
                await _send_action_error(
                    websocket, "The chat action could not be completed.", "action_failed"
                )
    except WebSocketDisconnect:
        manager.disconnect(websocket, port_id, user.id)
        logger.info("User %s disconnected from chat port %s", user.id, port_id)
        await manager.broadcast_system_message(
            port_id,
            "user_left",
            {"online_count": manager.get_online_count(port_id)},
        )
