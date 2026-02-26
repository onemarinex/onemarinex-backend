import os
import sys

# Add current directory to path so we can import app
sys.path.append(os.getcwd())

import psycopg2
from dotenv import load_dotenv
from app.db.base import Base
from app.db.session import engine

load_dotenv()

def sync():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found")
        return

    print(f"Connecting to database...")
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    
    # 1. Drop old incident tables to allow recreate with new schema
    print("Dropping old incident tables (if they exist)...")
    cur.execute("DROP TABLE IF EXISTS incident_notes CASCADE;")
    cur.execute("DROP TABLE IF EXISTS incidents CASCADE;")
    
    # Update Enums if they exist
    print("Updating Enum types...")
    try:
        cur.execute("ALTER TYPE incidenttype ADD VALUE IF NOT EXISTS 'DRIVER';")
        cur.execute("ALTER TYPE incidentstatus ADD VALUE IF NOT EXISTS 'INVESTIGATING';")
    except Exception as e:
        print(f"Note: Enum update might have failed (this is expected if types don't exist yet): {e}")
        conn.rollback() # rollback if failed
        conn.commit()   # start new trans
    
    # 2. Fix cab_bookings schema if columns are missing
    print("Checking cab_bookings columns...")
    cur.execute("ALTER TABLE cab_bookings ADD COLUMN IF NOT EXISTS driver_id INTEGER;")
    cur.execute("ALTER TABLE cab_bookings ADD COLUMN IF NOT EXISTS aggregator_id INTEGER;")
    
    # 3. Fix drivers schema if vehicle_type/name are missing (from previous task)
    print("Checking drivers columns...")
    cur.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS vehicle_type VARCHAR(255);")
    cur.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS vehicle_name VARCHAR(255);")
    cur.execute("ALTER TABLE drivers ADD COLUMN IF NOT EXISTS hpid VARCHAR(64);")
    
    conn.commit()
    cur.close()
    conn.close()
    
    print("Triggering SQLAlchemy create_all to recreate missing tables...")
    # This will recreate 'incidents' and 'incident_notes' with the new schema
    Base.metadata.create_all(bind=engine)
    
    print("✅ Database sync complete!")

if __name__ == "__main__":
    sync()
