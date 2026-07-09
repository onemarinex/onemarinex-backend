import logging
from sqlalchemy import text
from app.db.session import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migration")

def run_migration():
    logger.info("Starting database migration to add auth_document_url...")
    query = text("ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS auth_document_url VARCHAR(512)")
    
    with engine.begin() as conn:
        conn.execute(query)
        logger.info("Migration completed successfully!")

if __name__ == "__main__":
    run_migration()
