from fastapi.testclient import TestClient
from app.main import app
import json

client = TestClient(app)

from app.api.v1.routes_ports import get_current_user

class DummyUser:
    role = "superadmin"

app.dependency_overrides[get_current_user] = lambda: DummyUser()

payload = {
  "rules": [{"title": "Test", "description": "desc", "icon_type": "time"}]
  # NOTE: I am NOT sending working_days in the payload
}

res = client.post('/api/v1/ports/port_dubai/rules', json=payload)
print(res.status_code, res.text)
