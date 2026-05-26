import json
from app.db.session import SessionLocal
from app.db.models.vendors import Vendors

def inspect_vendors():
    db = SessionLocal()
    try:
        vendors = db.query(Vendors).all()
        print(f"Total Vendors found: {len(vendors)}")
        for v in vendors:
            print("-" * 50)
            print(f"ID: {v.id}")
            print(f"Name: {v.name}")
            print(f"Category: {v.category}")
            print(f"Other Info keys: {list(v.other_information.keys()) if v.other_information else 'None'}")
            print(f"Other Info Content: {v.other_information}")
    finally:
        db.close()

if __name__ == "__main__":
    inspect_vendors()
