import requests
import json

BASE_URL = "http://localhost:8000/api/v1"

def test_trip_monitoring():
    # Login as agent
    login_data = {
        "username": "9876543210", # Assuming this is a valid agent phone
        "password": "password123"
    }
    
    # In a real test we would need valid credentials
    # For now, let's just check if the endpoint exists and returns 403 for unauthenticated
    
    print("Testing /api/v1/trips/monitoring...")
    try:
        response = requests.get(f"{BASE_URL}/trips/monitoring")
        print(f"Status Code (Unauthenticated): {response.status_code}")
        
        # If we had a token, we would test with it
        # token = "..."
        # headers = {"Authorization": f"Bearer {token}"}
        # response = requests.get(f"{BASE_URL}/trips/monitoring", headers=headers)
        # print(f"Status Code (Authenticated): {response.status_code}")
        # print(json.dumps(response.json(), indent=2))
        
    except Exception as e:
        print(f"Error connecting to server: {e}")

if __name__ == "__main__":
    test_trip_monitoring()
