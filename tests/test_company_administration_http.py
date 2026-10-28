"""Tenant, mail and image validation acceptance on the disposable fixture."""

import unittest, base64, io, re
from unittest.mock import patch
from PIL import Image
import test_company_operations_http as fixture
from app.api.v1 import (
    routes_company_administration as administration,
    routes_company_onboarding as onboarding,
)
from app.db.models.company_profile import CompanyProfile, CompanyOnboardingDraft
from app.db.models.company_invitation import CompanyInvitation
from app.db.models.shipping_company import (
    CompanyUserMembership,
    ShippingCompany,
    CompanyAuditEvent,
    VesselRoleClaim,
)
from app.services import email as mail


class AdministrationHTTP(unittest.TestCase):
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request

    def setUp(self):
        fixture.OperationsHTTP.setUp(self)
        self.client.app.include_router(
            administration.router, prefix="/api/v1/shipping-company/administration"
        )
        self.client.app.include_router(
            onboarding.router, prefix="/api/v1/superadmin/company-onboarding"
        )

    def logo(self):
        out = io.BytesIO()
        Image.new("RGB", (24, 12), "blue").save(out, "PNG")
        return base64.b64encode(out.getvalue()).decode()

    def draft(self):
        data = dict(
            legal_name="New Shipping Company",
            country="India",
            registration_number="FIXTURE-REG",
            address="Fixture registered address",
            representative_name="Fixture representative",
            email="newadmin@example.com",
            evidence_type="company_register",
            evidence_reference="FIXTURE-PROOF",
            authority_evidence="Signed representative authority checked",
        )
        return self.request(
            "POST", "/api/v1/superadmin/company-onboarding", self.sa, data, 201
        ).json()

    def test_logo_upload_is_admin_only_and_strips_to_png(self):
        path = "/api/v1/shipping-company/administration/profile/logo"
        result = self.request("PUT", path, self.admin, {"content": self.logo()}).json()
        self.assertTrue(result["logo"].startswith("data:image/png;base64,"))
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update(
            {"role": "VIEWER"}
        )
        self.db.commit()
        self.request("PUT", path, self.admin, {"content": self.logo()}, 403)
        self.request("DELETE", path, self.admin, status=403)
        self.assertTrue(
            self.request(
                "GET", "/api/v1/shipping-company/administration/profile", self.admin
            ).json()["logo"]
        )

    def test_svg_and_corrupt_images_are_refused(self):
        for value in (
            base64.b64encode(b'<svg onload="alert(1)"></svg>').decode(),
            "not base64",
            base64.b64encode(b"\x89PNG\r\n").decode(),
        ):
            self.request(
                "PUT",
                "/api/v1/shipping-company/administration/profile/logo",
                self.admin,
                {"content": value},
                422,
            )
        self.assertIsNone(self.db.get(CompanyProfile, self.company.id))

    def test_company_logo_and_activity_are_tenant_bound(self):
        self.request(
            "PUT",
            "/api/v1/shipping-company/administration/profile/logo",
            self.admin,
            {"content": self.logo()},
        )
        self.db.add(
            CompanyAuditEvent(
                company_id=self.company.id,
                actor_user_id=self.admin.id,
                target_type="company_branding",
                target_id=self.company.id,
                action="logo_updated",
                after_state={"has_logo": True, "secret": "DO NOT DISCLOSE"},
            )
        )
        self.db.commit()
        data = self.request(
            "GET",
            "/api/v1/shipping-company/administration/activity?limit=1",
            self.admin,
        ).json()
        self.assertEqual(len(data["items"]), 1)
        self.assertNotIn("DO NOT DISCLOSE", str(data))
        self.request(
            "GET",
            "/api/v1/shipping-company/administration/activity",
            self.agent,
            status=403,
        )
        other = ShippingCompany(
            legal_name="Other tenant", normalized_name="OTHER TENANT", status="VERIFIED"
        )
        self.db.add(other)
        self.db.flush()
        self.db.query(CompanyUserMembership).filter_by(user_id=self.admin.id).update(
            {"company_id": other.id}
        )
        self.db.commit()
        self.assertIsNone(
            self.request(
                "GET", "/api/v1/shipping-company/administration/profile", self.admin
            ).json()["logo"]
        )
        self.assertEqual(
            self.request(
                "GET", "/api/v1/shipping-company/administration/activity", self.admin
            ).json()["total"],
            0,
        )

    def test_resend_rotates_invitation_and_failed_resend_preserves_old(self):
        def send(*args, **kwargs):
            return True

        with (
            patch.object(mail, "is_configured", return_value=True),
            patch.object(mail, "send_email", side_effect=send),
        ):
            first = self.request(
                "POST",
                "/api/v1/shipping-company/invitations",
                self.admin,
                {"email": "invite@example.com"},
                201,
            ).json()
            second = self.request(
                "POST",
                f"/api/v1/shipping-company/administration/invitations/{first['id']}/resend",
                self.admin,
                status=201,
            ).json()
        self.db.expire_all()
        self.assertIsNotNone(self.db.get(CompanyInvitation, first["id"]).revoked_at)
        with (
            patch.object(mail, "is_configured", return_value=True),
            patch.object(mail, "send_email", return_value=False),
        ):
            self.request(
                "POST",
                f"/api/v1/shipping-company/administration/invitations/{second['id']}/resend",
                self.admin,
                status=503,
            )
        self.db.expire_all()
        self.assertIsNone(self.db.get(CompanyInvitation, second["id"]).revoked_at)

    def test_onboarding_requires_evidence_and_superadmin(self):
        self.request(
            "POST", "/api/v1/superadmin/company-onboarding", self.admin, {}, 403
        )
        draft = self.request(
            "POST", "/api/v1/superadmin/company-onboarding", self.sa, {}, 201
        ).json()
        self.request(
            "POST",
            f"/api/v1/superadmin/company-onboarding/{draft['id']}/complete",
            self.sa,
            status=422,
        )

    def test_onboarding_verifies_sends_invitation_and_is_idempotent(self):
        draft = self.draft()
        path = f"/api/v1/superadmin/company-onboarding/{draft['id']}/complete"
        with (
            patch.object(mail, "is_configured", return_value=True),
            patch.object(mail, "send_email", return_value=True) as sent,
        ):
            result = self.request("POST", path, self.sa).json()
            again = self.request("POST", path, self.sa).json()
            self.assertEqual(result["company_id"], again["company_id"])
            self.assertEqual(sent.call_count, 1)
            self.assertIn("/company-invitation#", sent.call_args.args[2])
        self.db.expire_all()
        company = self.db.get(ShippingCompany, result["company_id"])
        self.assertEqual(company.status, "VERIFIED")
        self.assertEqual(
            self.db.query(VesselRoleClaim).filter_by(company_id=company.id).count(), 0
        )
        invitation = (
            self.db.query(CompanyInvitation).filter_by(company_id=company.id).one()
        )
        self.assertEqual(invitation.role, "ADMIN")
        self.assertEqual(len(invitation.token_hash), 64)

    def test_failed_onboarding_does_not_leave_verified_company(self):
        draft = self.draft()
        with (
            patch.object(mail, "is_configured", return_value=True),
            patch.object(mail, "send_email", return_value=False),
        ):
            self.request(
                "POST",
                f"/api/v1/superadmin/company-onboarding/{draft['id']}/complete",
                self.sa,
                status=503,
            )
        self.db.expire_all()
        self.assertEqual(
            self.db.get(CompanyOnboardingDraft, draft["id"]).status, "DRAFT"
        )
        self.assertIsNone(
            self.db.query(ShippingCompany)
            .filter_by(normalized_name="NEW SHIPPING COMPANY")
            .first()
        )
