# app/services/storage.py
"""File storage for uploads.

Uses DigitalOcean Spaces (S3-compatible) when configured, so files survive
deploys/restarts and are shared across instances. Falls back to local disk
(uploads/, served by the /uploads static mount) for local dev when Spaces
env vars are absent — keeps the app runnable without cloud credentials.

Environment:
    STORAGE_BACKEND                 "local", "spaces", or "auto". Defaults to
                                    local. Production must explicitly opt in to
                                    Spaces, so copied credentials alone can
                                    never receive development uploads.
    SPACES_KEY, SPACES_SECRET        access keys
    SPACES_BUCKET                    bucket / space name
    SPACES_REGION                    e.g. blr1, nyc3 (default blr1)
    SPACES_ENDPOINT                  optional; default
                                     https://<region>.digitaloceanspaces.com
    SPACES_CDN_ENDPOINT              optional CDN base for public URLs
    SPACES_PUBLIC                    "true" (default) → public-read objects,
                                     return a direct URL; else presign on read.
                                     **Only consulted for files stored as
                                     public**; see below.
    PRIVATE_UPLOAD_DIR               local-disk home for private files, kept
                                     outside the /uploads static mount.

Public or private is a property of the file, not of the deployment
-----------------------------------------------------------------
`save_fileobj` and `save_upload` both require an explicit `public=`. There is
no default, because the previous behaviour was to take one from the
environment: with `SPACES_PUBLIC` unset — its default — a vendor's bank
details and GST certificate were uploaded `public-read` and handed back as a
permanent direct URL, and on local disk they landed under the unauthenticated
`/uploads` mount under a *guessable* `<timestamp>_<their filename>` name. The
read-only audit raised this as "these defaults are not an identity-document
storage policy". They were not a policy at all; nothing in the codebase
expressed which files were sensitive, so there was nothing for a default to be
wrong about.

A private file is never `public-read` and never reachable without a
credential, whichever backend is configured:

    Spaces   → "spaces://<key>",  resolve() presigns, expires
    local    → "private://<key>", resolve() signs a short-lived API URL

Stored values already in the database are untouched and still resolve: both
public URLs and `/uploads/...` paths pass through `resolve()` unchanged. This
changes where *new* files go, and rewrites no rows.
"""
import base64
import re
import uuid
import hashlib
import hmac
import logging
import os
import time
from datetime import datetime
from typing import BinaryIO, Optional

from fastapi import UploadFile

logger = logging.getLogger("heyports.storage")

UPLOAD_DIR = os.getenv("UPLOAD_DIR", "uploads")
# Deliberately not under UPLOAD_DIR: app.main mounts that directory with
# StaticFiles, which asks for no credential at all.
PRIVATE_UPLOAD_DIR = os.getenv("PRIVATE_UPLOAD_DIR", "uploads-private")
# How long a signed local-disk URL stays usable. Matches the Spaces presign
# TTL so the two backends behave the same way.
PRIVATE_URL_TTL_SECONDS = int(os.getenv("PRIVATE_URL_TTL", "3600"))

SPACES_KEY = os.getenv("SPACES_KEY", "")
SPACES_SECRET = os.getenv("SPACES_SECRET", "")
SPACES_BUCKET = os.getenv("SPACES_BUCKET", "")
SPACES_REGION = os.getenv("SPACES_REGION", "blr1")
SPACES_ENDPOINT = os.getenv("SPACES_ENDPOINT", f"https://{SPACES_REGION}.digitaloceanspaces.com")
SPACES_CDN_ENDPOINT = os.getenv("SPACES_CDN_ENDPOINT", "")
SPACES_PUBLIC = os.getenv("SPACES_PUBLIC", "true").lower() not in ("0", "false", "no")
PRESIGN_TTL_SECONDS = int(os.getenv("SPACES_PRESIGN_TTL", "3600"))
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()
if STORAGE_BACKEND not in {"local", "spaces", "auto"}:
    raise RuntimeError("STORAGE_BACKEND must be local, spaces, or auto")


def spaces_enabled() -> bool:
    configured = bool(SPACES_KEY and SPACES_SECRET and SPACES_BUCKET)
    if STORAGE_BACKEND == "local":
        return False
    if STORAGE_BACKEND == "spaces" and not configured:
        raise RuntimeError("STORAGE_BACKEND=spaces requires SPACES_KEY, SPACES_SECRET and SPACES_BUCKET")
    return configured


_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3  # lazy so local dev without boto3/creds still boots
        _client = boto3.client(
            "s3",
            region_name=SPACES_REGION,
            endpoint_url=SPACES_ENDPOINT,
            aws_access_key_id=SPACES_KEY,
            aws_secret_access_key=SPACES_SECRET,
        )
    return _client


def _public_url(key: str) -> str:
    base = SPACES_CDN_ENDPOINT or f"{SPACES_ENDPOINT}/{SPACES_BUCKET}"
    return f"{base.rstrip('/')}/{key}"


# ---------- legacy helpers (kept for existing callers) ----------

def ensure_dir():
    os.makedirs(UPLOAD_DIR, exist_ok=True)


async def save_upload(file: UploadFile, *, public: bool) -> str:
    """Store an UploadFile. `public` says whether the world may read it.

    The name is a random key rather than `<timestamp>_<their filename>`. The
    old name was guessable — a timestamp and a filename the uploader chose —
    and these are served by an unauthenticated static mount, so the name was
    the only thing standing between a vendor's bank details and anyone who
    cared to try. The original name is kept as a suffix for recognisability
    only, sanitised.
    """
    original = (file.filename or "upload").split("/")[-1].split("\\")[-1]
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", original).strip(".-") or "upload"
    key = f"vendor_docs/{uuid.uuid4().hex}_{safe_name}"
    data = await file.read()
    import io as _io
    return save_fileobj(_io.BytesIO(data), key, content_type=file.content_type, public=public)


# ---------- Spaces-aware API ----------

def save_fileobj(
    fileobj: BinaryIO,
    key: str,
    content_type: Optional[str] = None,
    *,
    public: bool,
) -> str:
    """Persist a file-like object under `key`, returning its stored reference.

    `public` is required and has no default: it is the one thing about a file
    that cannot be inferred from the environment, and inferring it is what put
    bank details in a world-readable bucket. An agency logo is public; a
    passport scan, a receipt or a GST certificate is not.

        public,  Spaces   → direct/CDN object URL, public-read
        public,  local    → "/uploads/<key>", served by the static mount
        private, Spaces   → "spaces://<key>",  resolve() presigns
        private, local    → "private://<key>", resolve() signs an API URL

    A private file never receives a public-read ACL and never lands under the
    static mount, whatever `SPACES_PUBLIC` says.
    """
    if spaces_enabled():
        extra = {"ContentType": content_type or "application/octet-stream"}
        if public and SPACES_PUBLIC:
            extra["ACL"] = "public-read"
        _get_client().upload_fileobj(fileobj, SPACES_BUCKET, key, ExtraArgs=extra)
        return _public_url(key) if (public and SPACES_PUBLIC) else f"spaces://{key}"

    root = UPLOAD_DIR if public else PRIVATE_UPLOAD_DIR
    dest = os.path.join(root, key)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as buffer:
        while chunk := fileobj.read(1024 * 1024):
            buffer.write(chunk)
    return f"/{dest}" if public else f"private://{key}"


PRIVATE_PREFIX = "private://"


def is_private(stored: str) -> bool:
    """Whether a stored reference needs a credential or a signature to read."""
    return stored.startswith(("spaces://", PRIVATE_PREFIX))


def _private_path(key: str) -> Optional[str]:
    """The on-disk path for a private key, or None if the key escapes the root.

    The key arrives inside a signature we minted, so this is belt and braces —
    but a signing bug should not become an arbitrary file read.
    """
    root = os.path.realpath(PRIVATE_UPLOAD_DIR)
    candidate = os.path.realpath(os.path.join(root, key))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def sign_private_key(key: str, ttl_seconds: Optional[int] = None) -> str:
    """A short-lived token authorising a read of one private local file.

    The same shape as a Spaces presigned URL and for the same reason: the
    route that hands it out has already decided the caller may see this file,
    so the token carries that decision for a few minutes rather than making
    the file endpoint re-derive it for every kind of document in the app.
    """
    from app.core.config import settings

    expires = int(time.time()) + (ttl_seconds or PRIVATE_URL_TTL_SECONDS)
    payload = f"{key}|{expires}".encode()
    digest = hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest()
    return (
        base64.urlsafe_b64encode(payload).decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(digest).decode().rstrip("=")
    )


def verify_private_token(token: str) -> Optional[str]:
    """The key a token authorises, or None if it is forged, malformed or expired."""
    from app.core.config import settings

    def _unpad(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    try:
        encoded_payload, encoded_digest = token.split(".", 1)
        payload = _unpad(encoded_payload)
        digest = _unpad(encoded_digest)
    except Exception:
        return None

    expected = hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(digest, expected):
        return None

    try:
        key, expires = payload.decode().rsplit("|", 1)
    except Exception:
        return None
    if int(expires) < int(time.time()):
        return None
    return key


def resolve(stored: str) -> str:
    """Turn a stored reference into a client-fetchable URL.

    Public URLs and /uploads paths pass through unchanged, which is what keeps
    every row written before private storage existed working. A private
    reference becomes a URL that expires.
    """
    if stored.startswith(PRIVATE_PREFIX):
        key = stored[len(PRIVATE_PREFIX):]
        return f"/api/v1/files/private/{sign_private_key(key)}"
    if stored.startswith("spaces://"):
        key = stored[len("spaces://"):]
        try:
            return _get_client().generate_presigned_url(
                "get_object",
                Params={"Bucket": SPACES_BUCKET, "Key": key},
                ExpiresIn=PRESIGN_TTL_SECONDS,
            )
        except Exception:
            logger.exception("Failed to presign %s", key)
    return stored


def delete(stored: str) -> None:
    """Best-effort delete; cleanup must never fail the request."""
    try:
        if stored.startswith("spaces://"):
            _get_client().delete_object(Bucket=SPACES_BUCKET, Key=stored[len("spaces://"):])
            return
        if stored.startswith(PRIVATE_PREFIX):
            path = _private_path(stored[len(PRIVATE_PREFIX):])
            if path and os.path.isfile(path):
                os.remove(path)
            return
        if spaces_enabled() and not stored.startswith("/"):
            key = _key_from_url(stored)
            if key:
                _get_client().delete_object(Bucket=SPACES_BUCKET, Key=key)
            return
        local = stored.lstrip("/")
        if os.path.isfile(local):
            os.remove(local)
    except Exception:
        logger.exception("Failed to delete stored file %s", stored)


def _key_from_url(url: str) -> Optional[str]:
    for base in (SPACES_CDN_ENDPOINT, f"{SPACES_ENDPOINT}/{SPACES_BUCKET}"):
        if base and url.startswith(base):
            return url[len(base.rstrip("/")) + 1:]
    return None


def read_bytes(stored: str) -> Optional[tuple]:
    """The stored file's bytes and content type, or None if it cannot be read.

    Reports are rendered to a canvas in the browser and saved as a PDF, and a
    canvas that has drawn a cross-origin image without CORS headers cannot be
    exported. Object storage does not send those headers unless the bucket is
    configured to, so agency logos came out of the PDF as an empty space.

    Serving the bytes back through the API sidesteps the bucket's configuration
    entirely: the API already answers with CORS headers for the app's origin,
    so the same image becomes something the canvas will export.
    """
    try:
        if stored.startswith(PRIVATE_PREFIX):
            path = _private_path(stored[len(PRIVATE_PREFIX):])
            if path and os.path.isfile(path):
                import mimetypes
                with open(path, "rb") as handle:
                    return handle.read(), (mimetypes.guess_type(path)[0] or "application/octet-stream")
            return None
        if stored.startswith("spaces://"):
            key = stored[len("spaces://"):]
        elif spaces_enabled() and not stored.startswith("/"):
            key = _key_from_url(stored)
        else:
            key = None

        if key:
            obj = _get_client().get_object(Bucket=SPACES_BUCKET, Key=key)
            return obj["Body"].read(), obj.get("ContentType") or "application/octet-stream"

        # Local disk: the path is relative to the working directory, as the
        # /uploads static mount serves it.
        local = stored.lstrip("/")
        if os.path.isfile(local):
            import mimetypes
            with open(local, "rb") as handle:
                return handle.read(), (mimetypes.guess_type(local)[0] or "application/octet-stream")

        # An absolute URL we could not turn into a bucket key: a row written
        # while a different CDN endpoint was configured, or a logo hosted
        # somewhere else entirely. Fetching it is safe here because this column
        # is never client-supplied — it only ever holds what save_fileobj
        # returned — and a 404 instead would put us back to a blank masthead.
        if stored.startswith("http://") or stored.startswith("https://"):
            import urllib.request
            with urllib.request.urlopen(stored, timeout=10) as response:
                return (
                    response.read(),
                    response.headers.get("Content-Type") or "application/octet-stream",
                )
    except Exception:
        logger.exception("Failed to read stored file %s", stored)
    return None
