import logging
from sqlalchemy import text
from app.db.session import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
    logger.info("Starting database migration to add flag column to vessels...")
    query = text("ALTER TABLE vessels ADD COLUMN IF NOT EXISTS flag VARCHAR(100)")
    
    with engine.begin() as conn:
        conn.execute(query)
        logger.info("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
