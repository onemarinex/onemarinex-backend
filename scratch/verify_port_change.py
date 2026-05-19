import urllib.request
import urllib.error
import json
import uuid
import sys

def make_request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
        req.data = json.dumps(data).encode('utf-8')
    
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode('utf-8'))
        except Exception:
            err_body = e.reason
        return e.code, err_body
    except urllib.error.URLError as e:
        return None, str(e)

def verify_port_change():
    base_url = "http://localhost:8000/api/v1"
    random_id = uuid.uuid4().hex[:6]
    email = f"crew_{random_id}@example.com"
    password = "password123"
    passport = f"PP{random_id.upper()}"
    
    # 1. Register a new Crew Member
    payload = {
        "email": email,
        "password": password,
        "mobile_number": f"+1200{random_id}",
        "full_name": f"Test Crew {random_id}",
        "rank": "captain",
        "nationality": "US",
        "passport_number": passport,
        "date_of_birth": "1990-01-01"
    }
    
    print(f"Creating crew user: {email}")
    status, res = make_request(f"{base_url}/registration/crew", "POST", payload)
    if status != 201:
        print(f"Failed to register crew (status {status}): {res}")
        return False
        
    token = res["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # 2. Get Profile initially
    status, profile = make_request(f"{base_url}/crew/profile", "GET", headers=headers)
    if status != 200:
        print(f"Failed to get profile (status {status}): {profile}")
        return False
    
    print(f"Initial port: {profile.get('current_port')}")
    
    # 3. Patch port to 'port_singapore'
    print("Updating port to 'port_singapore'...")
    status, patch_res = make_request(f"{base_url}/crew/profile", "PATCH", {"current_port": "port_singapore"}, headers=headers)
    if status != 200:
        print(f"Failed to update profile (status {status}): {patch_res}")
        return False
        
    # 4. Get Profile again and verify the port is 'port_singapore'
    status, updated_profile = make_request(f"{base_url}/crew/profile", "GET", headers=headers)
    if status != 200:
        print(f"Failed to get profile after update (status {status}): {updated_profile}")
        return False
        
    updated_port = updated_profile.get("current_port")
    print(f"Updated port in profile: {updated_port}")
    
    if updated_port == "port_singapore":
        print("✅ SUCCESS: Port changed and preserved successfully!")
        return True
    else:
        print(f"❌ FAILURE: Port is '{updated_port}', expected 'port_singapore'.")
        return False

if __name__ == "__main__":
    if verify_port_change():
        print("End-to-End Port Change Verification Passed!")
        sys.exit(0)
    else:
        print("End-to-End Port Change Verification Failed.")
        sys.exit(1)
