import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from app.db.session import SessionLocal
from app.db.models.user import User
from app.services.auth import get_password_hash

def seed_superadmin():
    db = SessionLocal()
    email = "superadmin@heyports.com"
    existing = db.query(User).filter(User.email == email).first()
    
    if existing:
        print(f"Super Admin with email {email} already exists.")
        db.close()
        return

    admin = User(
        name="System Superadmin",
        email=email,
        hashed_password=get_password_hash("admin123"),
        role="superadmin"
    )
    
    try:
        db.add(admin)
        db.commit()
        print(f"Successfully created Super Admin user: {email}")
    except Exception as e:
        db.rollback()
        print(f"Error creating Super Admin: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_superadmin()
