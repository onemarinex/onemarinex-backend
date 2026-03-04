import sys
import os

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.db.base import PortRule

def seed_port_rules():
    db = SessionLocal()
    try:
        # Dubai Port Rules
        port_name = "port_dubai"
        rules = [
            {
                "title": "Shore Leave Timings",
                "description": "0800-2200 hrs daily",
                "icon_type": "time"
            },
            {
                "title": "Transport Policy",
                "description": "HeyPorts-verified cabs only",
                "icon_type": "policy"
            },
            {
                "title": "Documents Required",
                "description": "Shore Pass & Seafarer ID",
                "icon_type": "doc"
            },
            {
                "title": "Restricted Zones",
                "description": "No photography in dock area",
                "icon_type": "alert"
            }
        ]
        
        existing = db.query(PortRule).filter(PortRule.port_name == port_name).first()
        if existing:
            existing.rules = rules
            print(f"Updated rules for {port_name}")
        else:
            new_rules = PortRule(port_name=port_name, rules=rules)
            db.add(new_rules)
            print(f"Added rules for {port_name}")
        
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"Error seeding rules: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    seed_port_rules()
