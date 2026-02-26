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

    print(f"🔄 Connecting to database to create new tables...")
    
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Create incidents table
        print("Creating 'incidents' table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS incidents (
                id SERIAL PRIMARY KEY,
                incident_id VARCHAR UNIQUE NOT NULL,
                type VARCHAR NOT NULL,
                description TEXT NOT NULL,
                crew_id INTEGER REFERENCES crew_profiles(id),
                trip_id VARCHAR,
                status VARCHAR DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("✅ Created 'incidents' table")

        # Create incident_notes table
        print("Creating 'incident_notes' table...")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS incident_notes (
                id SERIAL PRIMARY KEY,
                incident_id INTEGER REFERENCES incidents(id) ON DELETE CASCADE,
                note TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        print("✅ Created 'incident_notes' table")

        conn.commit()
        cur.close()
        conn.close()
        print("✨ Migration complete!")
        
    except Exception as e:
        print(f"❌ Critical error during migration: {e}")

if __name__ == "__main__":
    migrate()
