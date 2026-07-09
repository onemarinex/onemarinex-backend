import logging
from sqlalchemy import text
from app.db.session import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
    logger.info("Starting database migration to add shore_pass_valid_upto...")
    query = text("ALTER TABLE vessel_crew ADD COLUMN IF NOT EXISTS shore_pass_valid_upto TIMESTAMP")
    
    with engine.begin() as conn:
        conn.execute(query)
        logger.info("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
