import sys
import os
from sqlalchemy import text

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal

def add_hpid_column():
    db = SessionLocal()
    try:
        # Check if column exists first (extra safety)
        result = db.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='crew_profiles' AND column_name='hpid'"))
        if result.fetchone():
            print("Column 'hpid' already exists in 'crew_profiles'.")
            return

        print("Adding 'hpid' column to 'crew_profiles'...")
        db.execute(text("ALTER TABLE crew_profiles ADD COLUMN hpid VARCHAR(64) UNIQUE"))
        db.commit()
        print("✅ Column 'hpid' added successfully!")
    except Exception as e:
        print(f"❌ Error adding column: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    add_hpid_column()
