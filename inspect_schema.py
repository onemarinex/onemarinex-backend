
from sqlalchemy import inspect
from app.db.session import engine
import sys

def inspect_table():
    inspector = inspect(engine)
    for table_name in ["restaurants", "hotels", "pubs", "sightseeings"]:
        print(f"\nTable: {table_name}")
        columns = inspector.get_columns(table_name)
        for column in columns:
            print(f"  Column: {column['name']}, Type: {column['type']}")

if __name__ == "__main__":
    inspect_table()
