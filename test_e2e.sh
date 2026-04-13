#!/bin/bash

# Login as super admin
TOKEN_JSON=$(curl -s -X POST "http://localhost:8000/api/v1/auth/login" -H "Content-Type: application/json" -d '{"email":"superadmin@heyports.com","password":"admin123"}')
ADMIN_TOKEN=$(echo $TOKEN_JSON | python3 -c "import sys, json; print(json.load(sys.stdin).get('access_token', ''))")

if [ -z "$ADMIN_TOKEN" ]; then
    echo "❌ Failed to get super admin token"
    exit 1
fi

# Get a port ID to attach venues
PORTS_JSON=$(curl -s "http://localhost:8000/api/v1/ports" -H "Authorization: Bearer $ADMIN_TOKEN")
PORT_ID=$(echo $PORTS_JSON | python3 -c "import sys, json; data=json.load(sys.stdin); print(data[0]['id'] if len(data) > 0 else 'null')")

if [ "$PORT_ID" == "null" ]; then
    echo "❌ No ports found in database"
    exit 1
fi

echo "--- 🛠 Testing Super Admin Addition Endpoints to Port $PORT_ID ---"

# Add Restaurant
curl -s -X POST "http://localhost:8000/api/v1/superadmin/restaurants" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "E2E Test Restaurant", "port_id": '"$PORT_ID"', "lat": 1.0, "lng": 1.0, "location_name": "Test Loc", "distance_from_port": 1.5, "price_per_person": 50, "timings": "9-5", "service_type": "Dine in"}' > /dev/null

# Add Pub
curl -s -X POST "http://localhost:8000/api/v1/superadmin/pubs" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "E2E Test Pub", "port_id": '"$PORT_ID"', "lat": 1.0, "lng": 1.0, "location_name": "Test Loc", "distance_from_port": 1.5, "price_per_person": 150, "timings": "9-5", "service_type": "Drinks"}' > /dev/null

# Add Hotel
curl -s -X POST "http://localhost:8000/api/v1/superadmin/hotels" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name": "E2E Test Hotel", "port_id": '"$PORT_ID"', "lat": 1.0, "lng": 1.0, "location": "Test Loc", "distance_from_port": 1.5, "price_per_night": 200}' > /dev/null

echo "✅ Created Restaurant, Pub, and Hotel!"
echo "\n--- 📱 Verifying Crew Application Endpoints ---"

# Check Restaurant
REST_JSON=$(curl -s "http://localhost:8000/api/v1/restaurants/")
echo $REST_JSON | python3 -c "
import sys, json
data = json.load(sys.stdin)
if any(v.get('name') == 'E2E Test Restaurant' for v in data):
    print('✅ YES! E2E Test Restaurant successfully found via Crew App endpoint.')
else:
    print('❌ FAILED: E2E Test Restaurant not found.')
"

# Check Pub
PUB_JSON=$(curl -s "http://localhost:8000/api/v1/pubs/")
echo $PUB_JSON | python3 -c "
import sys, json
data = json.load(sys.stdin)
if any(v.get('name') == 'E2E Test Pub' for v in data):
    print('✅ YES! E2E Test Pub successfully found via Crew App endpoint.')
else:
    print('❌ FAILED: E2E Test Pub not found.')
"

# Check Hotel
HOTEL_JSON=$(curl -s "http://localhost:8000/api/v1/hotels/")
echo $HOTEL_JSON | python3 -c "
import sys, json
data = json.load(sys.stdin)
if any(v.get('name') == 'E2E Test Hotel' for v in data):
    print('✅ YES! E2E Test Hotel successfully found via Crew App endpoint.')
else:
    print('❌ FAILED: E2E Test Hotel not found.')
"
