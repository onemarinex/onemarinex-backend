from app.db.session import SessionLocal
from app.db.models.port import Port
from app.db.models.sightseeing import Sightseeing

def find_sightseeing():
    db = SessionLocal()
    try:
        # Find Vishakapatnam port
        port = db.query(Port).filter(Port.name.ilike('%Vishakapatnam%')).first()
        if not port:
            # Try Vizag
            port = db.query(Port).filter(Port.name.ilike('%Vizag%')).first()
        
        if not port:
            print("Port Vishakapatnam not found.")
            return

        print(f"Port Found: {port.name} (ID: {port.id})")

        # Find a sightseeing place for this port
        place = db.query(Sightseeing).filter(Sightseeing.port_id == port.id).first()
        if not place:
            print(f"No sightseeing places found for port {port.name}")
            return

        print(f"Sightseeing Place Found: {place.name} (ID: {place.id})")
    finally:
        db.close()

if __name__ == "__main__":
    find_sightseeing()
