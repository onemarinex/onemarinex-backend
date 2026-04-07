import os
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    print("DATABASE_URL not found in environment")
    exit(1)

# Fix for Heroku/Render postgres:// to postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)

def update_schema():
    columns = [
        ("data_sharing", "BOOLEAN DEFAULT TRUE"),
        ("share_visits", "BOOLEAN DEFAULT TRUE"),
        ("safety_tracking", "BOOLEAN DEFAULT TRUE"),
        ("communication", "BOOLEAN DEFAULT TRUE"),
        ("notifications", "BOOLEAN DEFAULT TRUE")
    ]
    
    with engine.connect() as conn:
        print("Checking crew_profiles table columns...")
        for col_name, col_type in columns:
            try:
                # Check if column exists
                check_query = text(f"""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='crew_profiles' AND column_name='{col_name}';
                """)
                result = conn.execute(check_query).fetchone()
                
                if not result:
                    print(f"Adding column '{col_name}'...")
                    conn.execute(text(f"ALTER TABLE crew_profiles ADD COLUMN {col_name} {col_type};"))
                    conn.commit()
                    print(f"Column '{col_name}' added successfully.")
                else:
                    print(f"Column '{col_name}' already exists.")
            except Exception as e:
                print(f"Error processing column '{col_name}': {e}")
                conn.rollback()

if __name__ == "__main__":
    update_schema()
    print("Schema update completed.")
