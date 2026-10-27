"""Create the test logins documented in docs/TEST-LOGINS.md (idempotent).

One account per surface — crew, agent, superadmin — so the documented
credentials are true by construction rather than by memory. Every other test
account in this project was created by an ad-hoc script that no longer exists,
which left its password knowable only by whoever typed it.

Run:  PYTHONPATH=. ./.venv/bin/python scripts/seed_test_logins.py

By default this creates what is missing and leaves existing accounts alone, so
it is safe to re-run. Pass --reset to also set the password of an account that
already exists, for when someone has changed it and the doc has drifted.

REFUSES TO RUN against a non-local database unless ALLOW_REMOTE_SEED=1 is set.
These are weak, published, shared credentials: they belong on a developer's
machine and nowhere else.
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.seed_guard import refuse_if_remote
from app.db.session import SessionLocal
from app.services.auth import get_password_hash

PASSWORD = "Test1234!"

CREW_EMAIL = "sl-test-crew@heyports.dev"
AGENT_EMAIL = "testagent@heyports.dev"
SUPERADMIN_EMAIL = "superadmin@heyports.com"



def _user(db, email, role, name, reset):
    """Create the user, or optionally reset an existing one's password."""
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        user = User(
            email=email, name=name, role=role,
            hashed_password=get_password_hash(PASSWORD),
        )
        db.add(user)
        db.flush()
        print(f"created  {email:<28} ({role})")
        return user, True

    if reset:
        user.hashed_password = get_password_hash(PASSWORD)
        print(f"reset    {email:<28} ({user.role})")
    else:
        print(f"exists   {email:<28} ({user.role}) — password left as-is")
    return user, False


def seed(reset: bool) -> None:
    refuse_if_remote()
    db = SessionLocal()
    try:
        crew_user, _ = _user(db, CREW_EMAIL, "crew", "Test Crew", reset)
        if not db.query(CrewProfile).filter(
            CrewProfile.user_id == crew_user.id
        ).first():
            db.add(CrewProfile(
                user_id=crew_user.id, full_name="Test Crew",
                rank="able_seaman", nationality="IN",
            ))
            print(f"         + crew profile for {CREW_EMAIL}")

        agent_user, _ = _user(db, AGENT_EMAIL, "agent", "Test Agent", reset)
        if not db.query(AgentProfile).filter(
            AgentProfile.user_id == agent_user.id
        ).first():
            db.add(AgentProfile(
                user_id=agent_user.id, agency_name="Test Agency",
                location="Port of Visakhapatnam",
            ))
            print(f"         + agent profile for {AGENT_EMAIL}")

        _user(db, SUPERADMIN_EMAIL, "superadmin", "System Superadmin", reset)

        db.commit()
        print(f"\nPassword for all three: {PASSWORD}")
        print("app/db/seed_superadmin.py creates the same superadmin account")
        print("with the same password, so the two no longer disagree. Pass")
        print("--reset if someone has since changed one by hand.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset", action="store_true",
        help="also reset the password of accounts that already exist",
    )
    seed(parser.parse_args().reset)
