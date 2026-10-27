"""Capability spike for the company vessel-role tenure invariant (audit B04).

Document 2 requires that the same role on the same vessel cannot be held over
overlapping periods, enforced in the database rather than by an ORM check, so
two simultaneous approvals cannot both succeed.

Nothing in the application implements this yet and the role taxonomy and date
semantics are still open questions (Q5, Q7). This builds the candidate
constraint on a temporary table, proves it behaves as the specification needs
-- including under real concurrency -- and drops it. It creates no migration
and touches no application table, and becomes the acceptance test for the real
thing once the taxonomy is agreed.
"""
import threading
import unittest

import app.db.base  # noqa: F401
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from app.db.session import SessionLocal, engine

TABLE = "spike_vessel_role_tenure"

DDL = f"""
CREATE TABLE {TABLE} (
    id          serial PRIMARY KEY,
    vessel_id   integer NOT NULL,
    role        text    NOT NULL,
    company_id  integer NOT NULL,
    period      daterange NOT NULL,
    status      text    NOT NULL DEFAULT 'approved',
    -- Half-open periods, so a handover on the boundary date is not an overlap.
    -- Scoped to approved rows: a pending claim must not block a live one.
    CONSTRAINT {TABLE}_no_overlap EXCLUDE USING gist (
        vessel_id WITH =, role WITH =, period WITH &&
    ) WHERE (status = 'approved')
);
"""


class VesselRoleTenureExclusionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.available = True
        with engine.begin() as conn:
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
            except (ProgrammingError, DBAPIError) as exc:
                # Worth failing loudly rather than skipping: if a deployment
                # cannot install btree_gist, the B04 design needs rethinking.
                cls.available = False
                cls.reason = str(exc).splitlines()[0]
        if cls.available:
            with engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
                conn.execute(text(DDL))

    @classmethod
    def tearDownClass(cls):
        if cls.available:
            with engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))

    def setUp(self):
        if not self.available:
            self.fail(f"btree_gist unavailable, so the B04 design cannot work here: {self.reason}")
        self.db = SessionLocal()
        self.db.execute(text(f"DELETE FROM {TABLE}"))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def _claim(self, session, *, vessel=1, role="owner", company=1,
               period="[2026-01-01,2026-06-01)", status="approved"):
        session.execute(
            text(
                f"INSERT INTO {TABLE} (vessel_id, role, company_id, period, status) "
                "VALUES (:v, :r, :c, CAST(:period AS daterange), :s)"
            ),
            {"v": vessel, "r": role, "c": company, "period": period, "s": status},
        )

    def test_two_companies_cannot_hold_one_role_over_overlapping_dates(self):
        self._claim(self.db, company=1, period="[2026-01-01,2026-06-01)")
        self.db.commit()
        # The violation surfaces on the INSERT itself, not at commit time.
        with self.assertRaises(IntegrityError):
            self._claim(self.db, company=2, period="[2026-03-01,2026-09-01)")

    def test_a_handover_on_the_boundary_date_is_allowed(self):
        """[Jan, Mar) then [Mar, Jun) must both fit -- this is why Q7 matters."""
        self._claim(self.db, company=1, period="[2026-01-01,2026-03-01)")
        self._claim(self.db, company=2, period="[2026-03-01,2026-06-01)")
        self.db.commit()
        self.assertEqual(self._count(), 2)

    def test_different_roles_may_overlap(self):
        self._claim(self.db, role="owner", company=1)
        self._claim(self.db, role="commercial_manager", company=2)
        self.db.commit()
        self.assertEqual(self._count(), 2)

    def test_different_vessels_may_overlap(self):
        self._claim(self.db, vessel=1, company=1)
        self._claim(self.db, vessel=2, company=2)
        self.db.commit()
        self.assertEqual(self._count(), 2)

    def test_a_pending_claim_does_not_block_an_approved_one(self):
        self._claim(self.db, company=2, status="pending")
        self._claim(self.db, company=1, status="approved")
        self.db.commit()
        self.assertEqual(self._count(), 2)

    def test_two_pending_claims_may_overlap_until_one_is_approved(self):
        self._claim(self.db, company=1, status="pending")
        self._claim(self.db, company=2, status="pending")
        self.db.commit()
        self.assertEqual(self._count(), 2)

    def test_simultaneous_approvals_cannot_both_succeed(self):
        """The reason this belongs in the database and not in the ORM."""
        barrier = threading.Barrier(2)
        results = []

        def approve(company, period):
            session = SessionLocal()
            try:
                session.execute(text("SET statement_timeout = 10000"))
                session.execute(text(f"SELECT count(*) FROM {TABLE}"))  # both see an empty table
                barrier.wait(timeout=15)
                self._claim(session, company=company, period=period)
                session.commit()
                results.append(("ok", company))
            except Exception as exc:  # noqa: BLE001 - the outcome is the assertion
                session.rollback()
                results.append(("rejected", type(exc).__name__))
            finally:
                session.close()

        threads = [
            threading.Thread(target=approve, args=(1, "[2026-01-01,2026-06-01)")),
            threading.Thread(target=approve, args=(2, "[2026-03-01,2026-09-01)")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "approval deadlocked or hung")

        accepted = [r for r in results if r[0] == "ok"]
        self.assertEqual(len(accepted), 1, f"exactly one approval may win; results={results}")
        self.assertEqual(self._count(), 1, f"results={results}")

    def _count(self):
        return self.db.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()


if __name__ == "__main__":
    unittest.main()
