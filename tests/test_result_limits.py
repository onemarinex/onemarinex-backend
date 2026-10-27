"""No single response may try to materialise an entire table.

The audit: "no shared pagination/rate-limit policy ... several list paths end
in unbounded `.all()`." This is the ceiling half, and the tests are mostly
about what it must NOT do — a safety limit that silently returns a prefix is
worse than the unbounded query it replaced, because the reader draws
conclusions from a list that looks complete.

Runs inside a transaction that is always rolled back.
"""

import unittest
import uuid

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import Response
from sqlalchemy.orm import Session

from app.api.v1 import routes_superadmin
from app.db.models.crew_profile import CrewProfile
from app.db.models.user import User
from app.db.session import engine
from app.services import result_limits


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class CeilingTests(unittest.TestCase):
    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)
        self.admin = type("Admin", (), {"role": "superadmin", "id": 1})()

        self.made = []
        for _ in range(6):
            user = User(email=_uniq("crew") + "@example.com", hashed_password="x", role="crew")
            self.db.add(user)
            self.db.flush()
            profile = CrewProfile(user_id=user.id, full_name="Crew", rank="able_seaman",
                                  nationality="IN", hpid=_uniq("HP"))
            self.db.add(profile)
            self.db.flush()
            self.made.append(profile.id)

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def track_crew(self, limit=None):
        response = Response()
        rows = routes_superadmin.track_crew(
            response=response, limit=limit, db=self.db, current_user=self.admin)
        return rows, response.headers

    def test_a_normal_read_returns_everything_and_says_it_is_complete(self):
        rows, headers = self.track_crew()
        self.assertGreaterEqual(len(rows), 6)
        self.assertEqual(headers["X-Result-Truncated"], "false")
        self.assertEqual(headers["X-Result-Count"], str(len(rows)))

    def test_a_truncated_read_says_so(self):
        rows, headers = self.track_crew(limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(headers["X-Result-Truncated"], "true")
        self.assertEqual(headers["X-Result-Limit"], "3")

    def test_the_ceiling_is_above_anything_the_system_holds(self):
        """It is a runaway stop, not a limit anyone meets in normal use."""
        self.assertGreaterEqual(result_limits.DEFAULT_MAX_ROWS, 2000)

    def test_a_caller_cannot_raise_the_ceiling_without_bound(self):
        self.assertEqual(
            result_limits.resolve_limit(10 ** 9), result_limits.HARD_MAX_ROWS)

    def test_a_nonsense_limit_falls_back_to_the_default(self):
        for value in (None, 0, -1):
            self.assertEqual(
                result_limits.resolve_limit(value), result_limits.DEFAULT_MAX_ROWS)

    def test_the_response_is_still_a_bare_list(self):
        """The shape is unchanged, so no existing caller needs updating."""
        rows, _ = self.track_crew()
        self.assertIsInstance(rows, list)

    def test_exactly_the_ceiling_is_not_reported_as_truncated(self):
        """Off by one here turns a complete answer into a false warning."""
        query = self.db.query(CrewProfile).filter(CrewProfile.id.in_(self.made[:4]))
        rows, truncated, ceiling = result_limits.capped(query, limit=4)
        self.assertEqual(len(rows), 4)
        self.assertFalse(truncated)
        self.assertEqual(ceiling, 4)

    def test_one_over_the_ceiling_is_reported_as_truncated(self):
        query = self.db.query(CrewProfile).filter(CrewProfile.id.in_(self.made[:5]))
        rows, truncated, _ = result_limits.capped(query, limit=4)
        self.assertEqual(len(rows), 4)
        self.assertTrue(truncated)


class EveryGrowingHistoryIsCappedTests(unittest.TestCase):
    """The lists that grow with time, rather than with current state.

    A caller-scoped list is not automatically safe: one driver's whole ride
    history, one crew member's whole bill history and a Super Admin's view of
    every notification the platform has ever sent all grow without limit,
    however well scoped they are.

    Read from the source rather than exercised, because what is being checked
    is that a route has not been *left out* -- which a behavioural test on the
    routes that were included cannot see.
    """

    CAPPED_ROUTES = (
        ("app/api/v1/routes_drivers.py", "get_driver_rides"),
        ("app/api/v1/routes_notifications.py", "list_notifications_admin"),
        ("app/api/v1/routes_expenses.py", "list_bills"),
        ("app/api/v1/routes_incidents.py", "get_incidents"),
        ("app/api/v1/routes_superadmin.py", "track_crew"),
        ("app/api/v1/routes_superadmin.py", "track_incidents"),
        ("app/api/v1/routes_superadmin.py", "track_drivers"),
        ("app/api/v1/routes_superadmin.py", "track_service_requests"),
    )
    # get_incident_monitoring is not here on purpose. It returns two lists and
    # caps only one -- the other is summed into the open-incident counts and
    # must stay complete -- so the "no bare .all()" rule below does not
    # describe it. DeliberatelyNotCappedTests checks both of its halves.

    def _body(self, relative, function):
        import pathlib

        source = pathlib.Path(relative).read_text()
        start = source.index(f"def {function}(")
        end = source.find("\n@router", start)
        return source[start:end if end != -1 else len(source)]

    def test_no_branch_of_a_capped_route_still_ends_in_a_bare_all(self):
        """Every terminal query, not just one of them.

        The first version of this asked whether `capped_response` appeared in
        the body, which a route with several role branches passes while one of
        them quietly returns everything. `capped_response` performs the
        `.all()` itself, so a remaining one is a branch that escaped.
        """
        for relative, function in self.CAPPED_ROUTES:
            body = self._body(relative, function)
            with self.subTest(route=function):
                self.assertIn("capped_response", body)
                self.assertNotIn(
                    ".all()", body,
                    f"{function} still has a branch ending in .all(), so that "
                    "branch returns everything it matches.",
                )

    def test_a_capped_list_drops_the_oldest_rows_not_the_newest(self):
        """A ceiling keeps the rows it reaches first.

        On an oldest-first ordering that means keeping the oldest and dropping
        the newest, which is the opposite of what a truncated history should
        do. Capping `company_review`'s audit events, which are ordered by id
        ascending, would hide the most recent decisions.
        """
        for relative, function in self.CAPPED_ROUTES:
            body = self._body(relative, function)
            with self.subTest(route=function):
                self.assertIn(
                    ".desc()", body,
                    f"{function} is capped but not ordered newest-first, so "
                    "the ceiling would drop the most recent rows.",
                )

    def test_a_capped_list_is_ordered_so_truncation_is_deterministic(self):
        """Without an ORDER BY, a ceiling returns an arbitrary slice.

        `get_incidents` had no ordering at all, and "the first 2000 rows
        Postgres happened to return" is not an answer anyone can read.
        """
        for relative, function in self.CAPPED_ROUTES:
            with self.subTest(route=function):
                self.assertIn("order_by", self._body(relative, function))


class DeliberatelyNotCappedTests(unittest.TestCase):
    """Three lists that grow with time and still must not have a ceiling.

    Each was on the shortlist of candidates and each was excluded for a
    different reason, so the exclusions are recorded rather than left to look
    like oversights.
    """

    @staticmethod
    def _body(relative, function):
        import pathlib

        source = pathlib.Path(relative).read_text()
        start = source.index(f"def {function}(")
        end = source.find("\n@router", start)
        return source[start:end if end != -1 else len(source)]

    def test_a_list_that_is_also_counted_keeps_every_row(self):
        """`active_incidents` is returned *and* summed into the open counts.

        A ceiling would under-report how many incidents are open, which is a
        safety number read as fact. It is bounded by operational reality
        anyway: a row leaves the set when it is resolved.
        """
        body = self._body("app/api/v1/routes_incidents.py", "get_incident_monitoring")
        active = body[body.index("active_incidents ="):body.index("resolved_incidents =")]
        self.assertNotIn("capped_response", active)
        self.assertIn("active_crew = sum(", body)
        # The resolved list beside it, which nothing counts, is capped.
        self.assertIn("capped_response", body[body.index("resolved_incidents ="):])

    def test_a_merged_feed_is_not_capped_by_a_single_header(self):
        """Two streams sorted together in Python cannot report one truncation."""
        body = self._body("app/api/v1/routes_incidents.py", "agent_incident_list")
        self.assertIn("_agent_sos_records", body)
        self.assertNotIn("capped_response", body)

    def test_a_report_is_not_silently_shortened(self):
        """A report that quietly omits rows is worse than one that is slow."""
        body = self._body("app/api/v1/routes_incidents.py", "agent_safety_report_records")
        self.assertNotIn("capped_response", body)


class NotAppliedToInternalWorkTests(unittest.TestCase):
    """A cleanup loop that processed the first N rows would be a data bug.

    The departure path deletes every manifest row for a vessel. If a ceiling
    were ever applied there it would leave rows behind, silently, and the
    vessel would carry a stale roster into its next call.
    """

    def test_the_departure_cleanup_is_not_capped(self):
        import pathlib

        source = pathlib.Path("app/api/v1/routes_vessels.py").read_text()
        start = source.index("departed_manifest = db.query(VesselCrew)")
        self.assertNotIn("capped", source[start:start + 400])


if __name__ == "__main__":
    unittest.main()


class HeadersReachTheBrowserTests(unittest.TestCase):
    """Setting a header is not the same as the browser letting anyone read it.

    The frontend is served from a different origin than this API, and a
    browser exposes only a short safelist of response headers to JavaScript
    unless the server names the others in Access-Control-Expose-Headers.
    `allow_headers` does not cover this -- that is the request side.

    Without it the ceiling headers arrive and are invisible to the code that
    has to act on them, so a list truncated at the ceiling renders as a
    complete one. A Playwright test found this by stubbing the response and
    finding the notice never appeared.
    """

    def test_the_ceiling_headers_are_exposed_cross_origin(self):
        from fastapi.middleware.cors import CORSMiddleware

        import app.main as main

        cors = [mw for mw in main.app.user_middleware if mw.cls is CORSMiddleware]
        self.assertEqual(len(cors), 1, "expected exactly one CORS middleware")
        exposed = {name.lower() for name in (cors[0].kwargs.get("expose_headers") or [])}
        for header in ("x-result-count", "x-result-limit", "x-result-truncated"):
            with self.subTest(header=header):
                self.assertIn(header, exposed)
