from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Set
from datetime import datetime
import json

from app.db.session import get_db
from app.db.models.user import User
from app.db.models.chat import ChatMessage
from app.db.models.port import Port
from app.api.v1.routes_auth import get_current_user
# If get_current_user requires Bearer we'll need to parse token for WS manually or use query params.

router = APIRouter()

# --- Connection Manager for WebSockets ---
class ConnectionManager:
    def __init__(self):
        # Maps port_id -> set of active WebSockets
        self.active_connections: Dict[int, Set[WebSocket]] = {}
        # We can also keep track of users per port if needed: maps port_id -> set of user_ids
        self.active_users: Dict[int, Set[int]] = {}

    async def connect(self, websocket: WebSocket, port_id: int, user_id: int):
        await websocket.accept()
        if port_id not in self.active_connections:
            self.active_connections[port_id] = set()
            self.active_users[port_id] = set()
            
        self.active_connections[port_id].add(websocket)
        self.active_users[port_id].add(user_id)
        
        # Broadcast that the count changed
        await self.broadcast_system_message(port_id, "user_joined", {"online_count": self.get_online_count(port_id)})

    def disconnect(self, websocket: WebSocket, port_id: int, user_id: int):
        if port_id in self.active_connections:
            if websocket in self.active_connections[port_id]:
                self.active_connections[port_id].remove(websocket)
            # Remove user_id logic: ideally reference counted or loop through all sockets to see if user is still connected.
            # For simplicity, if one socket disconnects, let's just assume one user session.
            if user_id in self.active_users[port_id]:
                self.active_users[port_id].discard(user_id)
                
            if len(self.active_connections[port_id]) == 0:
                del self.active_connections[port_id]
                del self.active_users[port_id]
                
    def get_online_count(self, port_id: int) -> int:
        users = self.active_users.get(port_id)
        return len(users) if users is not None else 0

    async def broadcast(self, port_id: int, message: str, sender: User):
        print(f"[WS] Broadcasting to port {port_id}: {message} from {sender.email}")
        if port_id in self.active_connections:
            payload = {
                "type": "chat_message",
                "data": {
                    "user_id": sender.id,
                    "name": sender.name or sender.email,
                    "role": sender.role,
                    "message": message,
                    "created_at": datetime.utcnow().isoformat()
                }
            }
            json_payload = json.dumps(payload)
            # Send to all connected sockets for this port
            for connection in self.active_connections[port_id]:
                try:
                    await connection.send_text(json_payload)
                except Exception:
                    pass
                    
    async def broadcast_system_message(self, port_id: int, event_type: str, data: dict):
        if port_id in self.active_connections:
            payload = {
                "type": "system",
                "event": event_type,
                "data": data
            }
            json_payload = json.dumps(payload)
            for connection in self.active_connections[port_id]:
                try:
                    await connection.send_text(json_payload)
                except Exception:
                    pass

manager = ConnectionManager()

# --- HTTP Endpoints ---

@router.get("/channels")
def get_channels(db: Session = Depends(get_db)):
    """Fetch all ports that acts as chat channels."""
    ports = db.query(Port).filter(Port.is_active == True).all()
    result = []
    for port in ports:
        result.append({
            "id": port.id,
            "name": port.name,
            "code": port.code,
            "online_count": manager.get_online_count(port.id)
        })
    return result

@router.get("/{port_id}/messages")
def get_chat_history(port_id: int, limit: int = 50, db: Session = Depends(get_db)):
    """Fetch past messages for a channel."""
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.port_id == port_id)
        .order_by(ChatMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    result = []
    # Reverse so oldest is first or frontend can handle sorting
    for m in messages[::-1]:
        sender = db.query(User).filter(User.id == m.user_id).first()
        result.append({
            "id": m.id,
            "user_id": m.user_id,
            "name": sender.name or sender.email if sender else "Unknown User",
            "role": sender.role if sender else "user",
            "message": m.message,
            "created_at": m.created_at.isoformat() if m.created_at else None
        })
    return result


# --- WebSocket Endpoint ---
from jose import jwt
from app.core.config import settings

def get_user_from_token(token: str, db: Session) -> User:
    try:
        # The token sub contains the email, not the ID
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        email = payload.get("sub")
        if email is None:
            return None
        return db.query(User).filter(User.email == email).first()
    except Exception as e:
        print(f"[WS] Token decode error: {e}")
        return None

@router.websocket("/ws/{port_id}")
async def websocket_endpoint(websocket: WebSocket, port_id: int, token: str = Query(...), db: Session = Depends(get_db)):
    user = get_user_from_token(token, db)
    if not user:
        await websocket.close(code=1008)  # Policy Violation
        return

    # Check if port exists
    port = db.query(Port).filter(Port.id == port_id, Port.is_active == True).first()
    if not port:
        await websocket.close(code=1008)
        return

    await manager.connect(websocket, port_id, user.id)
    print(f"[WS] User {user.id} ({user.email}) connected to port {port_id}")
    try:
        while True:
            # Receive message
            data = await websocket.receive_text()
            print(f"[WS] Received data from user {user.id} on port {port_id}: {data}")
            try:
                msg_data = json.loads(data)
                text = msg_data.get("message", "").strip()
                
                if text:
                    # Save to DB
                    new_msg = ChatMessage(port_id=port_id, user_id=user.id, message=text)
                    db.add(new_msg)
                    db.commit()
                    
                    # Broadcast
                    await manager.broadcast(port_id, text, user)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket, port_id, user.id)
        print(f"[WS] User {user.id} disconnected from port {port_id}")
        # Broadcast updated count
        await manager.broadcast_system_message(port_id, "user_left", {"online_count": manager.get_online_count(port_id)})
