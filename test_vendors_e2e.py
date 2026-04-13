from fastapi.testclient import TestClient
from app.main import app
from app.db.session import SessionLocal
from app.db.models.port import Port
from app.db.models.user import User
from app.services.auth import create_access_token
import datetime 

client = TestClient(app)
db = SessionLocal()

# 1. Get or create a Port
port = db.query(Port).first()
if not port:
    port = Port(name="Test Port", location="Test Location", code="TEST01")
    db.add(port)
    db.commit()
    db.refresh(port)

# 2. Get tokens
admin_token = create_access_token(subject="superadmin@heyports.com", expires_delta=datetime.timedelta(days=1))
# Create dummy crew user
crew = db.query(User).filter(User.email=="testcrew@heyports.com").first()
if not crew:
    crew = User(email="testcrew@heyports.com", hashed_password="fake", role="crew")
    db.add(crew)
    db.commit()
    
crew_token = create_access_token(subject="testcrew@heyports.com", expires_delta=datetime.timedelta(days=1))

admin_headers = {"Authorization": f"Bearer {admin_token}"}
crew_headers = {"Authorization": f"Bearer {crew_token}"}

print(f"--- ADDING VENDORS TO PORT {port.name} (ID: {port.id}) ---")

# 3. Add Restaurant via SuperAdmin API
res = client.post("/api/v1/superadmin/restaurants", headers=admin_headers, json={
    "name": "E2E Test Restaurant",
    "port_id": port.id,
    "lat": 1.0, "lng": 1.0, "location_name": "Test Location", "distance_from_port": 1.5,
    "price_per_person": 50, "timings": "10-10", "service_type": "Dine in"
})
print("Restaurant post:", res.status_code, res.json())

# 4. Add Pub via SuperAdmin API
res = client.post("/api/v1/superadmin/pubs", headers=admin_headers, json={
    "name": "E2E Test Pub",
    "port_id": port.id,
    "lat": 1.0, "lng": 1.0, "location_name": "Test Location", "distance_from_port": 1.5,
    "price_per_person": 150, "timings": "10-10", "service_type": "Drinks"
})
print("Pub post:", res.status_code, res.json())

# 5. Add Hotel via SuperAdmin API
res = client.post("/api/v1/superadmin/hotels", headers=admin_headers, json={
    "name": "E2E Test Hotel",
    "port_id": port.id,
    "lat": 1.0, "lng": 1.0, "location": "Test Location", "distance_from_port": 1.5,
    "price_per_night": 2000
})
print("Hotel post:", res.status_code, res.json())

print("\n--- FETCHING FROM CREW APP ENDPOINTS ---")

c_res = client.get(f"/api/v1/restaurants/?port_id={port.id}", headers=crew_headers)
if c_res.status_code == 200:
    for item in c_res.json():
        if item.get("name") == "E2E Test Restaurant":
            print("✅ Restaurant successfully verified in crew API!")
            break

c_res = client.get(f"/api/v1/pubs/?port_id={port.id}", headers=crew_headers)
if c_res.status_code == 200:
    for item in c_res.json():
        if item.get("name") == "E2E Test Pub":
            print("✅ Pub successfully verified in crew API!")
            break

c_res = client.get(f"/api/v1/hotels/?port_id={port.id}", headers=crew_headers)
if c_res.status_code == 200:
    for item in c_res.json():
        if item.get("name") == "E2E Test Hotel":
            print("✅ Hotel successfully verified in crew API!")
            break

