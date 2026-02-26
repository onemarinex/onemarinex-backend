import os
import psycopg2
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def migrate():
    print("🔄 Migrating Driver table...")
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        columns_to_add = [
            ("email", "VARCHAR(255)"),
            ("hpid", "VARCHAR(32) UNIQUE"),
            ("vehicle_type", "VARCHAR(64)"),
            ("vehicle_name", "VARCHAR(255)")
        ]

        for col_name, col_type in columns_to_add:
            print(f"Checking column '{col_name}'...")
            try:
                cur.execute(f"ALTER TABLE drivers ADD COLUMN {col_name} {col_type};")
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
        print("✨ Driver Migration complete!")
        
    except Exception as e:
        print(f"❌ Critical error during migration: {e}")

if __name__ == "__main__":
    migrate()
