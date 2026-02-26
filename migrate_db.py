import os
import psycopg2
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

def migrate():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    print(f"🔄 Connecting to database to apply schema changes...")
    
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Add driver_plate
        print("Adding 'driver_plate' column...")
        try:
            cur.execute("ALTER TABLE cab_bookings ADD COLUMN driver_plate VARCHAR;")
            print("✅ Added 'driver_plate'")
        except psycopg2.errors.DuplicateColumn:
            print("⚠️ 'driver_plate' already exists")
            conn.rollback()
        except Exception as e:
            print(f"❌ Error adding 'driver_plate': {e}")
            conn.rollback()
        else:
            conn.commit()

        # Add aggregator_name
        print("Adding 'aggregator_name' column...")
        try:
            cur.execute("ALTER TABLE cab_bookings ADD COLUMN aggregator_name VARCHAR;")
            print("✅ Added 'aggregator_name'")
        except psycopg2.errors.DuplicateColumn:
            print("⚠️ 'aggregator_name' already exists")
            conn.rollback()
        except Exception as e:
            print(f"❌ Error adding 'aggregator_name': {e}")
            conn.rollback()
        else:
            conn.commit()

        cur.close()
        conn.close()
        print("✨ Migration complete!")
        
    except Exception as e:
        print(f"❌ Critical error during migration: {e}")

if __name__ == "__main__":
    migrate()
