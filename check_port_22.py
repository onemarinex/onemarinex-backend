
from app.db.session import SessionLocal
from app.db.models.port import Port
import sys

def check_port():
    db = SessionLocal()
    try:
        ports = db.query(Port).all()
        print(f"Total ports: {len(ports)}")
        for p in ports:
            print(f"ID: {p.id}, Name: {p.name}, Code: {p.code}")
        
        target = db.query(Port).filter(Port.id == 22).first()
        if target:
            print(f"FOUND PORT 22: {target.name}")
        else:
            print("PORT 22 NOT FOUND")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_port()
