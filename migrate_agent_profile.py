import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def migrate():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    print("🔄 Updating 'agent_profiles' table schema...")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Add columns if they don't exist
        columns_to_add = [
            ("contact_person", "VARCHAR(255)"),
            ("assigned_port", "VARCHAR(255)"),
            ("gst_number", "VARCHAR(64)"),
            ("license_number", "VARCHAR(64)"),
            ("status", "VARCHAR(32) DEFAULT 'Active'"),
            ("profile_image", "TEXT")
        ]
        
        for col_name, col_type in columns_to_add:
            print(f"Adding column '{col_name}'...")
            try:
                cur.execute(f"ALTER TABLE agent_profiles ADD COLUMN {col_name} {col_type};")
            except psycopg2.errors.DuplicateColumn:
                conn.rollback()
                print(f"Column '{col_name}' already exists, skipping.")
            except Exception as e:
                conn.rollback()
                print(f"Error adding column '{col_name}': {e}")
            else:
                conn.commit()
                print(f"✅ Added column '{col_name}'")
        
        cur.close()
        conn.close()
        print("✨ Schema update complete!")
        
    except Exception as e:
        print(f"❌ Critical error during migration: {e}")

if __name__ == "__main__":
    migrate()
