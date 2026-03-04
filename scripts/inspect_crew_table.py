import sys
import os
from sqlalchemy import inspect

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import engine

def inspect_table():
    inspector = inspect(engine)
    columns = inspector.get_columns('crew_profiles')
    print(f"Columns in 'crew_profiles':")
    for column in columns:
        print(f"- {column['name']} ({column['type']})")

if __name__ == "__main__":
    inspect_table()
