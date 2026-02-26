import os
import psycopg2
from dotenv import load_dotenv
from app.db.base import Base
from app.db.session import engine

# Load environment variables
load_dotenv()

def migrate():
    print("🔄 Starting database migration...")
    
    # 1. Ensure all tables are created (especially the new 'drivers' table)
    try:
        print("Ensuring all tables exist...")
        Base.metadata.create_all(bind=engine)
        print("✅ Tables check/creation complete.")
    except Exception as e:
        print(f"❌ Error during table creation: {e}")

    # 2. Add missing columns to cab_bookings
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        columns_to_add = [
            ("port", "VARCHAR(255)"),
            ("driver_id", "INTEGER REFERENCES drivers(id)"),
            ("aggregator_id", "INTEGER REFERENCES aggregator_profiles(id)"),
            ("driver_name", "VARCHAR"),
            ("driver_phone", "VARCHAR"),
            ("driver_plate", "VARCHAR"),
            ("aggregator_name", "VARCHAR"),
            ("agent_number", "VARCHAR DEFAULT '+91 9876543251'"),
            ("otp", "VARCHAR(10)")
        ]

        for col_name, col_type in columns_to_add:
            print(f"Checking column '{col_name}'...")
            try:
                cur.execute(f"ALTER TABLE cab_bookings ADD COLUMN {col_name} {col_type};")
                print(f"✅ Added '{col_name}'")
                conn.commit()
            except psycopg2.errors.DuplicateColumn:
                print(f"⚠️ '{col_name}' already exists")
                conn.rollback()
            except Exception as e:
                print(f"❌ Error adding '{col_name}': {e}")
                conn.rollback()

        cur.close()
        conn.close()
        print("✨ Migration complete!")
        
    except Exception as e:
        print(f"❌ Critical error during migration: {e}")

if __name__ == "__main__":
    migrate()
