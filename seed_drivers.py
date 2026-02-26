from app.db.session import SessionLocal
from app.db.models.driver import Driver
from app.db.models.aggregator_profile import AggregatorProfile

def seed_drivers():
    db = SessionLocal()
    try:
        aggregator = db.query(AggregatorProfile).first()
        if not aggregator:
            print("No aggregator found to link drivers to.")
            return

        drivers_data = [
            {
                "name": "Rajesh Kumar",
                "phone": "+91 9876543210",
                "license_number": "DL123456789",
                "vehicle_number": "MH 01 AG 1282",
                "rating": 4.9,
                "status": "Available"
            },
            {
                "name": "Suresh Raina",
                "phone": "+91 9876543211",
                "license_number": "DL987654321",
                "vehicle_number": "MH 02 BG 4567",
                "rating": 4.7,
                "status": "Available"
            },
            {
                "name": "Amit Shah",
                "phone": "+91 9876543212",
                "license_number": "DL567890123",
                "vehicle_number": "MH 03 CG 8901",
                "rating": 4.8,
                "status": "Busy"
            }
        ]

        for data in drivers_data:
            exists = db.query(Driver).filter(Driver.vehicle_number == data["vehicle_number"]).first()
            if not exists:
                driver = Driver(**data, aggregator_id=aggregator.id)
                db.add(driver)
        
        db.commit()
        print("Dummy drivers seeded successfully!")
    except Exception as e:
        print(f"Error seeding drivers: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed_drivers()
