from sqlalchemy import text
from app.db.session import engine

def migrate_shore_pass_columns():
    # Use separate connections/transactions for each column to avoid aborted transaction block
    
    # 1. Add approved_by_id
    with engine.connect() as conn:
        print("Checking approved_by_id in shore_passes table...")
        try:
            conn.execute(text("ALTER TABLE shore_passes ADD COLUMN approved_by_id INTEGER;"))
            conn.execute(text("ALTER TABLE shore_passes ADD CONSTRAINT fk_approved_by_id FOREIGN KEY (approved_by_id) REFERENCES users (id);"))
            conn.commit()
            print("Successfully added approved_by_id column.")
        except Exception as e:
            conn.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                print("approved_by_id column already exists.")
            else:
                print(f"Error adding approved_by_id: {e}")

    # 2. Add approved_by_name
    with engine.connect() as conn:
        print("Checking approved_by_name in shore_passes table...")
        try:
            conn.execute(text("ALTER TABLE shore_passes ADD COLUMN approved_by_name VARCHAR(120);"))
            conn.commit()
            print("Successfully added approved_by_name column.")
        except Exception as e:
            conn.rollback()
            if "already exists" in str(e).lower() or "duplicate column" in str(e).lower():
                print("approved_by_name column already exists.")
            else:
                print(f"Error adding approved_by_name: {e}")
        
    print("Migration check complete!")

if __name__ == "__main__":
    migrate_shore_pass_columns()
