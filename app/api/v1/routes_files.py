import io
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response

from app.api.v1.routes_auth import get_current_user
from app.db.models.user import User
from app.services import storage
from app.services.storage import save_fileobj

router = APIRouter()

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf",
    "image/jpeg",
    "image/png",
    "image/webp",
}


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    """Store a validated authenticated upload using the configured backend."""
    content_type = (file.content_type or "").lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Only PDF, JPEG, PNG and WebP files are supported")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload exceeds the 10 MB limit")

    original = (file.filename or "upload").split("/")[-1].split("\\")[-1]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", original).strip(".-") or "upload"
    key = f"general/{current_user.id}/{uuid.uuid4().hex}_{safe_name}"
    # Private: this endpoint accepts any PDF or image from any signed-in user,
    # so it is exactly where a passport scan arrives. Nothing here knows what
    # the file is, which is the reason to assume the worst about it.
    stored = save_fileobj(io.BytesIO(data), key, content_type=content_type, public=False)
    return {
        "filename": original,
        "content_type": content_type,
        "size": len(data),
        "url": storage.resolve(stored),
        "stored_ref": stored,
    }


@router.get("/files/private/{token}")
def read_private_file(token: str):
    """Serve one private local-disk file against a short-lived signed token.

    The Spaces backend presigns and never reaches this route. On local disk
    there is nothing to presign, so this is the equivalent: the route that
    handed out the URL made the access decision, and the signature carries it.
    Unauthenticated by design — the token *is* the credential — and it expires.
    """
    key = storage.verify_private_token(token)
    if key is None:
        raise HTTPException(status_code=404, detail="This link is not valid or has expired")

    payload = storage.read_bytes(f"{storage.PRIVATE_PREFIX}{key}")
    if payload is None:
        raise HTTPException(status_code=404, detail="File not found")

    body, content_type = payload
    return Response(
        content=body,
        media_type=content_type,
        headers={
            # A private document should not sit in a shared cache, and the
            # signed URL outliving the page that fetched it is the point.
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
