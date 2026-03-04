from app.db.session import SessionLocal
from app.db.base import Port

def seed_ports():
    db = SessionLocal()
    existing = db.query(Port).count()
    if existing > 0:
        print(f"Detected {existing} existing ports. Skipping seed.")
        db.close()
        return

    ports = [
        {"name": "Dubai Port", "code": "port_dubai"},
        {"name": "Singapore Port", "code": "port_singapore"},
        {"name": "Visakhapatnam Port", "code": "port_vishakapatnam"},
        {"name": "Shanghai Port", "code": "port_shanghai"},
        {"name": "Rotterdam Port", "code": "port_rotterdam"},
    ]

    for p_data in ports:
        port = Port(name=p_data["name"], code=p_data["code"], is_active=True)
        db.add(port)
    
    try:
        db.commit()
        print(f"Successfully seeded {len(ports)} ports.")
    except Exception as e:
        db.rollback()
        print(f"Error seeding ports: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_ports()
