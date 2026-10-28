import unittest
from datetime import timedelta
import test_company_operations_http as fixture
from app.api.v1 import (
    routes_company_disclosure as disclosure,
    routes_company_reports as reports,
)
from app.db.models.incident import Incident, IncidentNote, IncidentType
from app.db.models.company_disclosure import (
    CompanyDisclosureRequest,
)
from app.db.models.shipping_company import CompanyUserMembership, VesselRoleClaim
from app.db.models.cab_booking import CabBooking


class DisclosureHTTP(unittest.TestCase):
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request
    booking = fixture.OperationsHTTP.booking

    def setUp(self):
        fixture.OperationsHTTP.setUp(self)
        self.client.app.include_router(
            disclosure.company_router, prefix="/api/v1/shipping-company"
        )
        self.client.app.include_router(
            disclosure.admin_router, prefix="/api/v1/superadmin"
        )
        self.client.app.include_router(
            reports.router, prefix="/api/v1/shipping-company/report-centre"
        )
        self.case = Incident(
            incident_id="FIXTURE-CASE",
            type=IncidentType.CREW,
            title="Sensitive case",
            description="PRIVATE DESCRIPTION",
            vessel_id=self.vessel.id,
            vessel_call_id=self.call.id,
            agency_id=self.agency.id,
            context_resolution="CONFIRMED_AGENCY",
            created_at=fixture.NOW,
        )
        self.db.add(self.case)
        self.db.flush()
        self.note = IncidentNote(
            incident_id=self.case.id, note="Alice private note", created_at=fixture.NOW
        )
        self.db.add(self.note)
        self.db.commit()

    def escalation(self):
        return self.request(
            "POST",
            "/api/v1/superadmin/safety-escalations",
            self.sa,
            {
                "source_kind": "INCIDENT",
                "source_id": self.case.id,
                "company_id": self.company.id,
                "summary": "Approved limited case summary",
                "urgency": "HIGH",
            },
            201,
        ).json()["id"]

    def ask(self, escalation_id):
        return self.request(
            "POST",
            "/api/v1/shipping-company/disclosure-requests",
            self.admin,
            {
                "source_kind": "CASE",
                "source_id": escalation_id,
                "fields": ["case.title", "case.description", "case.status", "notes"],
                "reason": "Company needs reviewed case information",
                "event_start": fixture.JAN.isoformat(),
                "event_end": (fixture.NOW + timedelta(hours=2)).isoformat(),
            },
            201,
        ).json()["id"]

    def review(self, request_id, **extra):
        body = {
            "action": "APPROVE",
            "fields": ["case.title", "case.status", "notes"],
            "reason": "Release only reviewed fields and redacted notes",
            "note_ids": [self.note.id],
            "redact_terms": ["Alice"],
            "expires_at": (fixture.NOW + timedelta(hours=1)).isoformat(),
        }
        body.update(extra)
        if body["action"] == "APPROVE":
            preview = self.request(
                "POST",
                f"/api/v1/superadmin/disclosure-requests/{request_id}/preview",
                self.sa,
                body,
            ).json()
            body["preview_sha256"] = preview["preview_sha256"]
            body["expires_at"] = preview["expires_at"]
        return self.request(
            "POST",
            f"/api/v1/superadmin/disclosure-requests/{request_id}/decision",
            self.sa,
            body,
        )

    def test_only_superadmin_can_escalate_and_summary_has_no_raw_case(self):
        body = {
            "source_kind": "INCIDENT",
            "source_id": self.case.id,
            "company_id": self.company.id,
            "summary": "Approved limited case summary",
        }
        self.request(
            "POST", "/api/v1/superadmin/safety-escalations", self.agent, body, 403
        )
        e = self.escalation()
        data = self.request(
            "GET", f"/api/v1/shipping-company/safety-escalations/{e}", self.admin
        ).json()
        self.assertNotIn("PRIVATE DESCRIPTION", str(data))
        self.assertNotIn("Alice", str(data))

    def test_unescalated_case_cannot_be_requested(self):
        self.request(
            "POST",
            "/api/v1/shipping-company/disclosure-requests",
            self.admin,
            {
                "source_kind": "CASE",
                "source_id": 999,
                "fields": ["case.description"],
                "reason": "Need private information",
                "event_start": fixture.JAN.isoformat(),
                "event_end": fixture.NOW.isoformat(),
            },
            404,
        )

    def test_partial_approval_redaction_and_changed_note_fail_closed(self):
        rid = self.ask(self.escalation())
        self.review(rid)
        path = f"/api/v1/shipping-company/disclosure-requests/{rid}/data"
        data = self.request("GET", path, self.admin).json()["data"]
        self.assertNotIn("case.description", data)
        self.assertNotIn("Alice", str(data))
        self.assertIn("[redacted]", str(data))
        self.note.note = "New private content"
        self.case.description = "NEW SENSITIVE DESCRIPTION"
        self.db.commit()
        data = self.request("GET", path, self.admin).json()["data"]
        self.assertEqual(data["notes"], [])
        self.assertNotIn("NEW SENSITIVE", str(data))

    def test_expired_grant_denies_read_and_pdf(self):
        rid = self.ask(self.escalation())
        self.review(rid)
        row = self.db.get(CompanyDisclosureRequest, rid)
        row.access_expires_at = fixture.NOW - timedelta(seconds=1)
        self.db.commit()
        for suffix in ("", "?format=pdf"):
            self.request(
                "GET",
                f"/api/v1/shipping-company/disclosure-requests/{rid}/data" + suffix,
                self.admin,
                status=403,
            )

    def test_approval_cannot_add_unrequested_fields_or_unrelated_items(self):
        rid = self.ask(self.escalation())
        self.request(
            "POST",
            f"/api/v1/superadmin/disclosure-requests/{rid}/decision",
            self.sa,
            {
                "action": "APPROVE",
                "fields": ["crew.phone"],
                "reason": "Not requested fields must remain denied",
            },
            422,
        )
        self.request(
            "POST",
            f"/api/v1/superadmin/disclosure-requests/{rid}/decision",
            self.sa,
            {
                "action": "APPROVE",
                "fields": ["notes"],
                "note_ids": [999],
                "reason": "Unrelated case items must remain denied",
            },
            422,
        )

    def test_revoked_escalation_and_tenure_deny_existing_grant(self):
        e = self.escalation()
        rid = self.ask(e)
        self.review(rid)
        self.request(
            "DELETE", f"/api/v1/superadmin/safety-escalations/{e}", self.sa, status=204
        )
        self.request(
            "GET",
            f"/api/v1/shipping-company/disclosure-requests/{rid}/data",
            self.admin,
            status=404,
        )

    def report(self):
        self.booking()
        return self.request(
            "POST",
            "/api/v1/shipping-company/report-centre",
            self.admin,
            {
                "report_type": "FLEET",
                "start": fixture.JAN.isoformat(),
                "end": (fixture.NOW + timedelta(hours=2)).isoformat(),
            },
            201,
        ).json()

    def test_report_payload_is_frozen_and_export_is_pdf(self):
        result = self.report()
        rid = result["id"]
        original = result["payload"]
        self.db.query(CabBooking).update({"num_passengers": 77})
        self.db.commit()
        stored = self.request(
            "GET", f"/api/v1/shipping-company/report-centre/{rid}", self.admin
        ).json()
        self.assertEqual(stored["payload"], original)
        pdf = self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{rid}/export?format=pdf",
            self.admin,
        )
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        self.assertNotIn("PRIVATE DESCRIPTION", str(original))
        self.assertNotIn("sos", str(original).lower())

    def test_report_other_company_and_removed_member_are_denied(self):
        result = self.report()
        rid = result["id"]
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{rid}",
            self.other_agent,
            status=403,
        )
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update(
            {"enabled": False}
        )
        self.db.commit()
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{rid}/export",
            self.admin,
            status=403,
        )

    def test_report_revoked_claim_does_not_bypass_current_access(self):
        rid = self.report()["id"]
        self.db.query(VesselRoleClaim).update({"revoked_at": fixture.NOW})
        self.db.commit()
        self.request(
            "GET",
            f"/api/v1/shipping-company/report-centre/{rid}/export",
            self.admin,
            status=404,
        )

    def test_new_lowercase_lifecycle_event_releases_only_its_generic_label(self):
        from app.db.models.incident import IncidentTimelineEvent

        eid = self.escalation()
        rid = self.request(
            "POST",
            "/api/v1/shipping-company/disclosure-requests",
            self.admin,
            {
                "source_kind": "CASE",
                "source_id": eid,
                "fields": ["timeline"],
                "reason": "Follow ordinary case progress without names",
                "event_start": fixture.JAN.isoformat(),
                "event_end": (fixture.NOW + timedelta(hours=2)).isoformat(),
            },
            201,
        ).json()["id"]
        self.review(rid, fields=["timeline"], note_ids=[])
        self.db.add(
            IncidentTimelineEvent(
                incident_id=self.case.id,
                source="system",
                event_type="resolved",
                label="Alice resolved the case",
                detail="PRIVATE agent detail",
                event_time=fixture.NOW,
            )
        )
        self.db.commit()
        data = self.request(
            "GET",
            f"/api/v1/shipping-company/disclosure-requests/{rid}/data",
            self.admin,
        ).json()["data"]
        self.assertEqual(data["timeline"][0]["text"], "Resolved")
        self.assertNotIn("Alice", str(data))
        self.assertNotIn("PRIVATE", str(data))

    def test_changed_source_requires_a_new_preview_before_approval(self):
        rid = self.ask(self.escalation())
        body = {
            "action": "APPROVE",
            "fields": ["notes"],
            "note_ids": [self.note.id],
            "reason": "Approve only the note shown in this preview",
            "expires_at": (fixture.NOW + timedelta(hours=1)).isoformat(),
        }
        path = f"/api/v1/superadmin/disclosure-requests/{rid}"
        preview = self.request("POST", path + "/preview", self.sa, body).json()
        self.note.note = "Changed source after preview"
        self.db.commit()
        self.request(
            "POST",
            path + "/decision",
            self.sa,
            dict(body, preview_sha256=preview["preview_sha256"]),
            409,
        )
        self.assertEqual(self.db.get(CompanyDisclosureRequest, rid).status, "REQUESTED")

    def test_tracking_status_filter_counts_expiry_before_pagination(self):
        rid = self.ask(self.escalation())
        self.review(rid)
        row = self.db.get(CompanyDisclosureRequest, rid)
        row.access_expires_at = fixture.NOW - timedelta(hours=1)
        self.db.commit()
        base = "/api/v1/shipping-company/disclosure-requests"
        self.assertEqual(
            self.request("GET", base + "?status=EXPIRED&limit=1", self.admin).json()[
                "total"
            ],
            1,
        )
        self.assertEqual(
            self.request("GET", base + "?status=APPROVED&limit=1", self.admin).json()[
                "total"
            ],
            0,
        )
