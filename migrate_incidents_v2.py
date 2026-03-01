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

    print(f"🔄 Connecting to database to apply schema changes to 'incidents'...")
    
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Make aggregator_id nullable
        print("Making 'aggregator_id' nullable...")
        try:
            cur.execute("ALTER TABLE incidents ALTER COLUMN aggregator_id DROP NOT NULL;")
            print("✅ 'aggregator_id' is now nullable")
        except Exception as e:
            print(f"❌ Error altering 'aggregator_id': {e}")
            conn.rollback()
        else:
            conn.commit()

        # Add port_name
        print("Adding 'port_name' column...")
        try:
            cur.execute("ALTER TABLE incidents ADD COLUMN port_name VARCHAR(128);")
            print("✅ Added 'port_name'")
        except psycopg2.errors.DuplicateColumn:
            print("⚠️ 'port_name' already exists")
            conn.rollback()
        except Exception as e:
            print(f"❌ Error adding 'port_name': {e}")
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
