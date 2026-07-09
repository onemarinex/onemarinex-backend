import logging
from sqlalchemy import text
from app.db.session import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
    logger.info("Starting database migration to add must_change_password...")
    query = text("ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN DEFAULT FALSE NOT NULL")
    
    with engine.begin() as conn:
        conn.execute(query)
        logger.info("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
