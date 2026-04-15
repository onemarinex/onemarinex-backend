
from sqlalchemy import text, inspect
from app.db.session import engine
import sys

def check_columns():
    inspector = inspect(engine)
    tables = ["restaurants", "hotels", "pubs", "sightseeings"]
    results = {}
    
    for table_name in tables:
        columns = [c['name'] for c in inspector.get_columns(table_name)]
        has_port_id = "port_id" in columns
        results[table_name] = has_port_id
        print(f"Table '{table_name}' has 'port_id': {has_port_id}")
    
    return results

if __name__ == "__main__":
    check_columns()
