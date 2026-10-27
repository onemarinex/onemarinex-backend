"""Create the system superadmin account.

Run as a module, `python -m app.db.seed_superadmin`: running the file by
path puts `app/db` on sys.path, where `types.py` shadows the standard
library and the import of sqlalchemy dies. The old sys.path.append here
never made that work and has been removed.

Nothing in the application calls it. It used to hardcode the
password `admin123` with no check on which database it was pointed at, so one
command in a deployed console was enough to create an administrator with a
guessable password. Now it refuses a non-local database, and the password comes
from the environment.
"""

import os
import sys

from app.db.models.user import User
from app.db.seed_guard import refuse_if_remote
from app.db.session import SessionLocal
from app.services.auth import get_password_hash

EMAIL = "superadmin@heyports.com"

# The weak local default, kept so a developer setup still works in one command.
# It is only reachable once refuse_if_remote() has confirmed a local database.
LOCAL_DEFAULT_PASSWORD = "Test1234!"


def _password() -> str:
    supplied = os.getenv("SUPERADMIN_PASSWORD")
    if supplied:
        return supplied
    if os.getenv("ALLOW_REMOTE_SEED") == "1":
        sys.exit(
            "Set SUPERADMIN_PASSWORD when seeding with ALLOW_REMOTE_SEED=1. "
            "The local default must not be used on a database you had to "
            "override a guard to reach."
        )
    return LOCAL_DEFAULT_PASSWORD


def seed_superadmin():
    refuse_if_remote("create the superadmin")
    password = _password()
    db = SessionLocal()
    try:
        if db.query(User).filter(User.email == EMAIL).first():
            print(f"Super Admin with email {EMAIL} already exists.")
            return
        db.add(User(
            name="System Superadmin",
            email=EMAIL,
            hashed_password=get_password_hash(password),
            role="superadmin",
        ))
        db.commit()
        print(f"Successfully created Super Admin user: {EMAIL}")
        if password is LOCAL_DEFAULT_PASSWORD:
            print("Password: the local default documented in docs/TEST-LOGINS.md")
    except Exception as e:
        db.rollback()
        print(f"Error creating Super Admin: {e}")
    finally:
        db.close()


if __name__ == "__main__":
    seed_superadmin()
