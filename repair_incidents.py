import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def repair():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    print("🛠 Repairing 'incidents' table...")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # 1. Update existing NULL values
        print("Updating NULL values...")
        cur.execute("UPDATE incidents SET created_at = NOW() WHERE created_at IS NULL;")
        cur.execute("UPDATE incidents SET updated_at = NOW() WHERE updated_at IS NULL;")
        cur.execute("UPDATE incidents SET status = 'ACTIVE' WHERE status IS NULL;")
        
        # 2. Add defaults to schema
        print("Setting column defaults...")
        cur.execute("ALTER TABLE incidents ALTER COLUMN created_at SET DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("ALTER TABLE incidents ALTER COLUMN updated_at SET DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("ALTER TABLE incidents ALTER COLUMN status SET DEFAULT 'ACTIVE';")
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Repair complete!")
        
    except Exception as e:
        print(f"❌ Error during repair: {e}")

if __name__ == "__main__":
    repair()
