import os
import sys

from app.core.config import database_is_local
from app.db.session import DATABASE_URL

# Re-exported so callers and tests have one name to reach for. The definition
# lives in app.core.config, which has no app imports, so the startup guard that
# refuses an insecure signing key can share it without a circular import.
__all__ = ["database_is_local", "refuse_if_remote"]


def refuse_if_remote(what: str = "seed") -> None:
    """Exit unless the configured database is local.

    `ALLOW_REMOTE_SEED=1` is the deliberate override, for the case where a
    developer's database genuinely is not local. It is not a production switch.
    """
    if os.getenv("ALLOW_REMOTE_SEED") == "1":
        return
    if database_is_local(DATABASE_URL):
        return
    sys.exit(
        f"Refusing to {what}: DATABASE_URL does not look local.\n"
        "This script creates accounts with known passwords, which must not "
        "reach a deployed environment.\n"
        "Set ALLOW_REMOTE_SEED=1 only if you are certain the target is yours."
    )
