import os
import psycopg2
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

def seed():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Get a valid crew member ID
        cur.execute("SELECT id FROM crew_profiles LIMIT 1;")
        res = cur.fetchone()
        crew_id = res[0] if res else None
        
        if not crew_id:
            print("⚠️ No crew members found, skipping incident creation.")
            return

        print(f"🌱 Seeding dummy incidents for crew_id: {crew_id}")
        
        # Delete existing dummy data to avoid duplicates
        cur.execute("DELETE FROM incident_notes WHERE incident_id IN (SELECT id FROM incidents WHERE incident_id LIKE 'INC-DUM%');")
        cur.execute("DELETE FROM incidents WHERE incident_id LIKE 'INC-DUM%';")

        # Incident 1: Late arrival
        cur.execute("""
            INSERT INTO incidents (incident_id, type, description, crew_id, trip_id, status)
            VALUES ('INC-DUM-001', 'CREW', 'Driver arrived 20 minutes late. Crew member waited at Gate 4 for extended period. Driver cited traffic but no prior communication.', %s, 'TR-101', 'ACTIVE')
            RETURNING id;
        """, (crew_id,))
        inc1_id = cur.fetchone()[0]
        
        # Incident 2: Overcharging
        cur.execute("""
            INSERT INTO incidents (incident_id, type, description, crew_id, status)
            VALUES ('INC-DUM-002', 'AGGREGATOR', 'Overcharging complaint. Crew claims fare was higher than estimated. Difference of ₹150.', %s, 'ACTIVE')
            RETURNING id;
        """, (crew_id,))
        inc2_id = cur.fetchone()[0]

        # Add notes
        cur.execute("INSERT INTO incident_notes (incident_id, note) VALUES (%s, 'Spoke with the driver, he confirmed the delay due to a protest on the main road.')", (inc1_id,))
        cur.execute("INSERT INTO incident_notes (incident_id, note) VALUES (%s, 'Waiting for the vendor to provide a refund for the excess amount.')", (inc2_id,))

        conn.commit()
        cur.close()
        conn.close()
        print("✅ Seeding complete!")
        
    except Exception as e:
        print(f"❌ Error seeding data: {e}")

if __name__ == "__main__":
    seed()
