from fastapi.testclient import TestClient
from app.main import app
import json

client = TestClient(app)

# Bypass auth for this test or we can just test GET which might not require auth?
# GET /api/v1/ports/{port_name}/rules does not require auth.
print('GET test_port:', client.get('/api/v1/ports/test_port/rules').json())
print('GET Visakhapatnam Port:', client.get('/api/v1/ports/Visakhapatnam%20Port/rules').json())
