from app.db.session import engine
from app.db.base import Base
import app.db.models.vessel
import app.db.models.vessel_crew

print("Creating tables...")
Base.metadata.create_all(bind=engine)
print("Tables created successfully.")

from sqlalchemy import inspect
inspector = inspect(engine)
print("Current tables:", inspector.get_table_names())
