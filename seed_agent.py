import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

def seed_agent():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    print("🌱 Seeding agent profile data...")
    try:
        conn = psycopg2.connect(db_url)
        cur = conn.cursor()
        
        # Update USer 17 (paramaboin@gmail.com)
        cur.execute("""
            UPDATE users 
            SET name = 'Vikram' 
            WHERE id = 17;
        """)
        
        # Update Agent Profile for User 17
        cur.execute("""
            UPDATE agent_profiles 
            SET 
                agency_name = 'Maritime Shipping Agency Pvt. Ltd.',
                contact_person = 'Vikram',
                location = 'Mumbai',
                assigned_port = 'Mumbai Port Trust',
                gst_number = '783774GHJY7487H',
                license_number = '783774GHJY7487H',
                agent_identifier = '12287-28792-87258',
                profile_image = 'https://randomuser.me/api/portraits/men/32.jpg'
            WHERE user_id = 17;
        """)
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Data seeded successfully!")
        
    except Exception as e:
        print(f"❌ Error seeding agent data: {e}")

if __name__ == "__main__":
    seed_agent()
