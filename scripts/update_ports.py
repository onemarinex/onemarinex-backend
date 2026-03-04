import sys
import os
from sqlalchemy import text

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.db.session import SessionLocal

def update_ports():
    db = SessionLocal()
    try:
        # 1. Clear ports except Dubai and Mumbai
        print("Cleaning up ports table...")
        db.execute(text("DELETE FROM ports WHERE name NOT IN ('Dubai', 'Mumbai')"))
        db.commit()
        
        # 2. Get existing ports to avoid duplicates
        result = db.execute(text("SELECT name FROM ports"))
        existing_ports = {row[0] for row in result}
        
        # 3. New ports to add
        new_ports = [
            "Kakinada", "Visakhapatnam", "KrishnaPatnam", "Chennai", 
            "Kolkata", "Mundra", "Kandla", "Mormogua", "JNPT", 
            "Paradip", "New Mangalore", "Cochin", "Haldia", 
            "Kamarajar", "Chidambaranar", "Port Blair"
        ]
        
        print(f"Adding {len(new_ports)} new ports...")
        for port_name in new_ports:
            if port_name not in existing_ports:
                port_code = f"port_{port_name.lower().replace(' ', '_')}"
                db.execute(
                    text("INSERT INTO ports (name, code, is_active, created_at) VALUES (:name, :code, :is_active, CURRENT_TIMESTAMP)"),
                    {"name": port_name, "code": port_code, "is_active": True}
                )
        
        db.commit()
        print("Port update completed successfully!")
        
        # Verify
        result = db.execute(text("SELECT name, code FROM ports"))
        print("Current ports in database:")
        for row in result:
            print(f" - {row[0]} ({row[1]})")

    except Exception as e:
        db.rollback()
        print(f"Error updating ports: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    update_ports()
