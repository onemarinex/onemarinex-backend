"""Authenticated stateless Demo PDFs never mutate application records."""

import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import text
from app.db.base import Base
from app.db.models.shipping_company import CompanyUserMembership, ShippingCompany
from app.api.v1 import routes_company_demo_reports as demo
from app.services.auth import create_access_token
import test_company_disclosure_reports_http as fixture
import test_company_operations_http as operations

BASE = "/api/v1/shipping-company/report-centre/demo"


class DemoReportsHTTP(unittest.TestCase):
    setUp = fixture.DisclosureHTTP.setUp
    tearDown = fixture.DisclosureHTTP.tearDown
    request = fixture.DisclosureHTTP.request
    headers = fixture.DisclosureHTTP.headers

    def body(self, kind="EXECUTIVE", **extra):
        return {"report_type": kind, "start": operations.JAN.isoformat(),
                "end": (operations.NOW + timedelta(days=1)).isoformat(), **extra}

    def preview(self, actor=None, **extra):
        return self.request("POST", BASE+"/preview", actor or self.admin, self.body(**extra)).json()

    def database_state(self):
        # The fixture contains synthetic records only. Compare complete row
        # content, not merely counts, and never print passwords or row values.
        return {table.name: self.db.execute(text(
            'SELECT md5(coalesce(string_agg(doc,\'|\' ORDER BY doc),\'\')) '
            'FROM (SELECT to_jsonb(t)::text AS doc FROM "'+table.name+'" t) s'
        )).scalar() for table in Base.metadata.sorted_tables}

    def test_preview_render_and_options_have_no_database_or_email_side_effects(self):
        before = self.database_state()
        with patch("app.services.email.send_email") as send:
            options = self.request("GET", BASE+"/options", self.admin).json()
            self.assertEqual(options["scope"]["countries"], ["IN"])
            self.assertEqual(len(options["types"]), 6)
            calls = self.request("GET", BASE+"/calls?limit=1", self.admin).json()
            self.assertGreater(calls["total"], 1)
            self.assertEqual(len(calls["items"]), 1)
            preview = self.preview()
            self.assertEqual(preview["payload"]["data_mode"], "DEMO")
            self.assertEqual(preview["payload"]["company_name"], "OneMarineX")
            self.assertNotIn("Fixture MV", str(preview["payload"]))
            first = self.request("POST", BASE+"/render", self.admin, {"token": preview["token"]})
            second = self.request("POST", BASE+"/render", self.admin, {"token": preview["token"]})
            self.assertTrue(first.content.startswith(b"%PDF-"))
            self.assertEqual(first.content, second.content)
            self.assertIn("no-store", first.headers["cache-control"])
            send.assert_not_called()
        self.db.expire_all()
        self.assertEqual(self.database_state(), before)

    def test_preview_token_is_bound_to_actor_and_company(self):
        preview = self.preview()
        self.request("POST", BASE+"/render", self.admin2, {"token":preview["token"]}, 403)
        other = ShippingCompany(legal_name="Other fixture company", normalized_name="OTHER FIXTURE COMPANY", status="VERIFIED")
        self.db.add(other)
        self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update({"company_id":other.id})
        self.db.commit()
        self.request("POST", BASE+"/render", self.admin, {"token":preview["token"]}, 403)

    def test_disabled_membership_blocks_existing_preview(self):
        preview = self.preview()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update({"enabled":False})
        self.db.commit()
        self.request("POST", BASE+"/render", self.admin, {"token":preview["token"]}, 403)

    def test_expired_tampered_and_access_tokens_cannot_render(self):
        preview = self.preview()
        token = preview["token"]
        head, content, signature = token.split(".")
        damaged = head+"."+content+"."+("A" if signature[0] != "A" else "B")+signature[1:]
        self.request("POST", BASE+"/render", self.admin, {"token":damaged}, 403)
        with patch.object(demo, "now", return_value=operations.NOW-timedelta(minutes=31)):
            expired = self.preview()["token"]
        self.request("POST", BASE+"/render", self.admin, {"token":expired}, 403)
        self.request("POST", BASE+"/render", self.admin, {"token":create_access_token(self.admin.email)}, 403)
        response = self.client.get(BASE+"/options", headers={"Authorization":"Bearer "+token})
        self.assertEqual(response.status_code, 401)

    def test_demo_rejects_live_payload_injection_and_invalid_scope(self):
        self.request("POST", BASE+"/preview", self.admin, {**self.body(), "payload":{"company_name":"Injected"}}, 422)
        self.request("POST", BASE+"/render", self.admin, {"token":"X"*32, "payload":{}}, 422)
        self.request("POST", BASE+"/preview", self.admin, self.body(vessel_id=999), 422)
        self.request("POST", BASE+"/preview", self.admin, self.body(configuration={"sections":["raw_crew"]}), 422)
        self.request("GET", BASE+"/calls?country_code=US", self.admin, status=422)
        self.request("GET", BASE+"/options", status=401)
        self.request("GET", BASE+"/options", self.agent, status=403)

    def test_call_report_scope_and_recorded_time_filters_are_real(self):
        calls = self.request("GET", BASE+"/calls?port_id=2&vessel_id=2", self.admin).json()
        self.assertTrue(calls["items"])
        self.assertTrue(all(c["port_id"] == 2 and c["vessel_id"] == 2 for c in calls["items"]))
        selected = calls["items"][0]
        preview = self.preview(kind="CALL", call_id=selected["id"], port_id=2, vessel_id=2)
        self.assertEqual([c["id"] for c in preview["payload"]["calls"]], [selected["id"]])
        self.assertGreater(preview["payload"]["operations"]["average_time_ashore_minutes"], 0)
        self.request("POST", BASE+"/preview", self.admin, self.body("CALL", call_id=selected["id"], port_id=1), 422)
