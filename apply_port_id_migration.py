
from sqlalchemy import text
from app.db.session import engine
import sys

def apply_migration():
    queries = [
        # 1. Restaurants
        "ALTER TABLE restaurants ADD COLUMN IF NOT EXISTS port_id INTEGER REFERENCES ports(id);",
        # 2. Hotels
        "ALTER TABLE hotels ADD COLUMN IF NOT EXISTS port_id INTEGER REFERENCES ports(id);",
        # 3. Pubs
        "ALTER TABLE pubs ADD COLUMN IF NOT EXISTS port_id INTEGER REFERENCES ports(id);",
    ]
    
    with engine.connect() as conn:
        print("Applying migration to add 'port_id' columns...")
        for query in queries:
            try:
                conn.execute(text(query))
                conn.commit()
                print(f"Executed: {query}")
            except Exception as e:
                print(f"Error executing '{query}': {e}")
                conn.rollback()
        print("Migration complete.")

if __name__ == "__main__":
    apply_migration()
