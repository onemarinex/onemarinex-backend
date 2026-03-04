import sys
import os

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.db.models.crew_profile import CrewProfile
from app.services.crew_service import generate_hpid

def repopulate_hpids():
    db = SessionLocal()
    try:
        crew_members = db.query(CrewProfile).all()
        if not crew_members:
            print("No crew members found to repopulate HPIDs.")
            return

        print(f"Starting Passport-based HPID repopulation for {len(crew_members)} crew members...")
        
        for crew in crew_members:
            # Determine port (use current_port if available, else port_general)
            port = crew.current_port if crew.current_port else "port_general"
            # NEW: Use passport_number instead of crew.id
            new_hpid = generate_hpid(crew.passport_number, crew.nationality, port)
            
            print(f"Updating {crew.full_name}: {crew.hpid} -> {new_hpid}")
            crew.hpid = new_hpid
        
        db.commit()
        print(f"Successfully repopulated HPIDs for {len(crew_members)} crew members.")
    except Exception as e:
        print(f"Error repopulating HPIDs: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    repopulate_hpids()
