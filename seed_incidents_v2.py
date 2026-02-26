import os
import sys

# Add current directory to path so we can import app
sys.path.append(os.getcwd())

from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.db.models.incident import Incident, IncidentType, IncidentStatus, IncidentNote
from app.db.models.aggregator_profile import AggregatorProfile
from datetime import datetime

def seed_v2():
    db = SessionLocal()
    try:
        aggregator = db.query(AggregatorProfile).first()
        if not aggregator:
            print("⚠️ No aggregators found, cannot seed incidents.")
            return

        print(f"🌱 Seeding incidents for aggregator: {aggregator.company_name}")
        
        # Clear existing (if any)
        db.query(IncidentNote).delete()
        db.query(Incident).delete()
        db.commit()

        # 1. Crew Incident
        inc1 = Incident(
            incident_id="INC-19382-A",
            aggregator_id=aggregator.id,
            type=IncidentType.CREW,
            title="Late Pickup at Gate 4",
            description="Driver arrived 25 minutes late for the scheduled pickup. Crew member was waiting in the rain. Driver gave no prior notification.",
            status=IncidentStatus.ACTIVE,
            reporter_name="John Doe",
            reporter_role="Chief Officer",
            reporter_id="HPID-1029-928",
            trip_id="TR 402"
        )
        db.add(inc1)
        db.flush()

        # 2. Driver Incident
        inc2 = Incident(
            incident_id="INC-82711-B",
            aggregator_id=aggregator.id,
            type=IncidentType.DRIVER,
            title="Unauthorized Route Change",
            description="Driver took a significantly longer route than suggested, claiming road closures that were not verified. Added 15km to the trip.",
            status=IncidentStatus.INVESTIGATING,
            reporter_name="Alex Smith",
            reporter_role="Port Coordinator",
            reporter_id="HPID-5521-112",
            trip_id="TR 881"
        )
        db.add(inc2)
        db.flush()

        # Add notes to inc2
        note1 = IncidentNote(
            incident_id=inc2.id,
            author_name="System",
            note="Incident flagged for investigation by Aggregator."
        )
        db.add(note1)

        db.commit()
        print("✅ Seeding complete!")

    finally:
        db.close()

if __name__ == "__main__":
    seed_v2()
