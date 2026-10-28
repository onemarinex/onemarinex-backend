import unittest
from datetime import timedelta
import test_company_operations_http as fixture
from app.api.v1 import routes_welfare as welfare
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.welfare_feedback import WelfareFeedback
from app.db.models.incident import Incident


class WelfareHTTP(unittest.TestCase):
    tearDown = fixture.OperationsHTTP.tearDown
    headers = fixture.OperationsHTTP.headers
    request = fixture.OperationsHTTP.request

    def setUp(self):
        fixture.OperationsHTTP.setUp(self)
        self.client.app.include_router(welfare.crew_router, prefix="/api/v1/crew")
        self.client.app.include_router(
            welfare.company_router,
            prefix="/api/v1/shipping-company/welfare-intelligence",
        )
        self.client.app.include_router(
            welfare.admin_router, prefix="/api/v1/superadmin/welfare-intelligence"
        )
        self.crew, self.crew_assignment = self.person(0)

    def person(self, n):
        user = User(
            email=f"crew{n}@example.com",
            role="crew",
            name="Synthetic crew",
            hashed_password=fixture.FIXTURE_PASSWORD_HASH,
        )
        self.db.add(user)
        self.db.flush()
        profile = CrewProfile(
            user_id=user.id,
            full_name="Synthetic crew",
            rank="AB",
            passport_number=f"FIXTURE{n}",
            nationality="IN",
        )
        self.db.add(profile)
        self.db.flush()
        assignment = CrewAssignment(
            vessel_call_id=self.call.id,
            crew_profile_id=profile.id,
            crew_name="Synthetic crew",
            passport_number=profile.passport_number,
            nationality="IN",
            source="MANIFEST",
            started_at=fixture.JAN,
        )
        self.db.add(assignment)
        self.db.commit()
        return user, assignment

    def body(self, a):
        return {
            "assignment_id": a.id,
            "experience_at": fixture.NOW.isoformat(),
            "went_ashore": "NO",
            "difficulty": "PREVENTED",
            "barriers": ["agent_extra_money"],
            "extra_charge": "REQUESTED_NOT_PAID",
            "message": "Synthetic unverified account",
        }

    def summary(self):
        start = (fixture.NOW - timedelta(days=1)).isoformat()
        end = (fixture.NOW + timedelta(days=1)).isoformat()
        from urllib.parse import urlencode

        return self.request(
            "GET",
            "/api/v1/shipping-company/welfare-intelligence/summary?"
            + urlencode({"start": start, "end": end}),
            self.admin,
        ).json()

    def test_nonbooker_can_submit_and_does_not_create_incident(self):
        self.request(
            "POST",
            "/api/v1/crew/welfare-feedback",
            self.crew,
            self.body(self.crew_assignment),
            201,
        )
        self.assertEqual(self.db.query(Incident).count(), 0)
        self.assertEqual(self.db.query(WelfareFeedback).count(), 1)
        self.assertTrue(self.summary()["suppressed"])

    def test_general_feedback_does_not_require_call(self):
        self.request(
            "POST",
            "/api/v1/crew/welfare-feedback",
            self.crew,
            {"message": "General suggestion"},
            201,
        )
        self.assertIsNone(self.db.query(WelfareFeedback).one().vessel_call_id)
        self.assertEqual(self.db.query(Incident).count(), 0)

    def test_other_person_assignment_is_refused(self):
        other, a = self.person(1)
        self.request(
            "POST",
            "/api/v1/crew/welfare-feedback",
            other,
            self.body(self.crew_assignment),
            404,
        )
        self.request(
            "POST", "/api/v1/crew/welfare-feedback", self.admin, self.body(a), 403
        )

    def test_survey_edit_does_not_duplicate_respondent(self):
        body = self.body(self.crew_assignment)
        for _ in range(2):
            self.request("POST", "/api/v1/crew/welfare-feedback", self.crew, body, 201)
        self.assertEqual(self.db.query(WelfareFeedback).count(), 1)

    def test_contradictory_barriers_and_unpaid_amount_are_refused(self):
        body = self.body(self.crew_assignment)
        body["difficulty"] = "NONE"
        self.request("POST", "/api/v1/crew/welfare-feedback", self.crew, body, 422)
        body["difficulty"] = "PREVENTED"
        body["amount_paid"] = 50
        body["currency"] = "INR"
        self.request("POST", "/api/v1/crew/welfare-feedback", self.crew, body, 422)

    def test_experience_outside_assignment_is_refused(self):
        body = self.body(self.crew_assignment)
        body["experience_at"] = (fixture.JAN - timedelta(days=1)).isoformat()
        self.request("POST", "/api/v1/crew/welfare-feedback", self.crew, body, 422)

    def test_five_people_unsuppress_without_identity_or_comment_disclosure(self):
        for n in range(5):
            user, a = (self.crew, self.crew_assignment) if n == 0 else self.person(n)
            self.request(
                "POST", "/api/v1/crew/welfare-feedback", user, self.body(a), 201
            )
        data = self.summary()
        self.assertEqual(data["respondents"], 5)
        self.assertEqual(data["obstacles"]["denominator"], 5)
        self.assertEqual(data["extra_charge_requested"]["numerator"], 5)
        self.assertNotIn("Synthetic unverified", str(data))
        self.assertNotIn("crew0@example.com", str(data))

    def test_admin_only_feedback_detail(self):
        self.request(
            "POST",
            "/api/v1/crew/welfare-feedback",
            self.crew,
            self.body(self.crew_assignment),
            201,
        )
        self.request(
            "GET",
            "/api/v1/superadmin/welfare-intelligence/feedback",
            self.admin,
            status=403,
        )
        self.assertEqual(
            self.request(
                "GET", "/api/v1/superadmin/welfare-intelligence/feedback", self.sa
            ).json()["total"],
            1,
        )

    def test_legacy_feedback_contract_and_invalid_message_validation(self):
        from app.api.v1 import routes_crew

        self.client.app.include_router(routes_crew.router, prefix="/api/v1/crew")
        path = "/api/v1/crew/feedback"
        result = self.request(
            "POST",
            path,
            self.crew,
            {"message": "Retain the original feedback response"},
        ).json()
        self.assertEqual(result, {"status": "success", "message": "Feedback received"})
        self.request("POST", path, self.crew, {"message": "   "}, 422)
        self.request("POST", path, self.crew, {"message": "X" * 5001}, 422)
        self.assertEqual(self.db.query(WelfareFeedback).count(), 1)
        self.assertEqual(self.db.query(Incident).count(), 0)
