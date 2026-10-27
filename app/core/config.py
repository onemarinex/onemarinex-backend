import os
import sys
from dotenv import load_dotenv
from urllib.parse import urlsplit
load_dotenv()

# Hosts that mean "this developer's machine". Kept here, with no app imports,
# so both this module and app/db/seed_guard.py can use one definition without
# a circular import.
LOCAL_DB_HOSTS = frozenset({
    "localhost", "127.0.0.1", "::1", "[::1]", "db", "postgres",
})

# The signing key shipped in this repository. Fine on a laptop, and a forged
# token for any account -- superadmin included -- anywhere else.
_INSECURE_SECRET_KEY = "onemarinexsecret"


def database_is_local(url: str) -> bool:
    """Whether a database URL points at this machine.

    The host is parsed rather than matched as a substring: `localhost` appears
    inside `notlocalhostile.example.com`, and a check that reads that as local
    fails in the direction that matters.
    """
    try:
        host = urlsplit(url).hostname
    except Exception:
        return False
    if host is None:
        return True   # a unix socket or a file-backed database is local
    return host.lower() in LOCAL_DB_HOSTS


def _env_flag(name: str, default: str) -> bool:
    """Read a boolean env var without being fussy about how it was written.

    The previous `os.getenv(...) == "true"` silently treated the perfectly
    ordinary `WHATSAPP_ENABLED=1` (and `yes`/`on`/`True `) as *off*, which
    turns every notification in the app into a no-op with nothing in the logs
    to say why. Accept the usual spellings and only treat an explicit falsy
    value as off.
    """
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    DATABASE_URL = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://onemarinex_user:onemarinex123!@localhost:5432/onemarinex",
    )
    SECRET_KEY = os.getenv("SECRET_KEY", _INSECURE_SECRET_KEY)
    ALGORITHM = os.getenv("ALGORITHM", "HS256")
    # Was hardcoded at 20160 while the deployed environment set the variable,
    # so whoever set it believed a shorter lifetime was in force and it was not.
    ACCESS_TOKEN_EXPIRE_MINUTES = int(
        os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "20160")  # 14 days
    )
    REFRESH_TOKEN_EXPIRE_MINUTES = int(os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", "43200"))  # 30 days
    APP_NAME = "OneMarinex API"

    # --- WhatsApp Business Cloud API (Meta Graph API) ---
    WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
    WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    WHATSAPP_API_VERSION = os.getenv("WHATSAPP_API_VERSION", "v21.0")
    WHATSAPP_DEFAULT_COUNTRY_CODE = os.getenv("WHATSAPP_DEFAULT_COUNTRY_CODE", "91")
    WHATSAPP_ENABLED = _env_flag("WHATSAPP_ENABLED", "true")
    APP_PUBLIC_BASE_URL = os.getenv("APP_PUBLIC_BASE_URL", "https://heyports-56we8.ondigitalocean.app")
    # --- Anthropic AI Moderation & Chat Safety ---
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5")
    CHAT_RATE_LIMIT_MAX = int(os.getenv("CHAT_RATE_LIMIT_MAX", "5"))
    CHAT_RATE_LIMIT_SECONDS = int(os.getenv("CHAT_RATE_LIMIT_SECONDS", "10"))
    CHAT_MAX_MESSAGE_LENGTH = int(os.getenv("CHAT_MAX_MESSAGE_LENGTH", "1000"))
    CHAT_BLOCK_PII = _env_flag("CHAT_BLOCK_PII", "true")
    CHAT_BLOCK_URLS = _env_flag("CHAT_BLOCK_URLS", "true")
    CHAT_EXTRA_BLOCKED_WORDS = [
        w.strip().lower() for w in os.getenv("CHAT_EXTRA_BLOCKED_WORDS", "").split(",") if w.strip()
    ]

settings = Settings()


def _refuse_insecure_secret_key() -> None:
    """Stop a deployment from signing tokens with the key in this repository.

    Only the fallback is refused, and only when the database is not local, so a
    laptop keeps working untouched. Raised at import so it fails the deploy
    rather than surfacing later as tokens that anyone can mint.
    """
    if settings.SECRET_KEY != _INSECURE_SECRET_KEY:
        return
    if database_is_local(settings.DATABASE_URL):
        return
    sys.exit(
        "Refusing to start: SECRET_KEY is unset, so the fallback in this "
        "repository would be used to sign every token against a non-local "
        "database. Anyone who can read the source could mint a token for any "
        "account. Set SECRET_KEY to a secret value for this environment."
    )


_refuse_insecure_secret_key()
