import sys
import os

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.db.models.crew_profile import CrewProfile
from app.services.crew_service import generate_hpid

def populate_hpids():
    db = SessionLocal()
    try:
        crew_members = db.query(CrewProfile).all()
        if not crew_members:
            print("No crew members found to populate HPIDs.")
            return

        print(f"Starting HPID population for {len(crew_members)} crew members...")
        
        for crew in crew_members:
            # Determine port (use current_port if available, else port_general)
            port = crew.current_port if crew.current_port else "port_general"
            crew.hpid = generate_hpid(crew.id, crew.nationality, port)
            print(f"Generated HPID for {crew.full_name} (ID: {crew.id}): {crew.hpid}")
        
        db.commit()
        print(f"Successfully populated HPIDs for {len(crew_members)} crew members.")
    except Exception as e:
        print(f"Error populating HPIDs: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    populate_hpids()
