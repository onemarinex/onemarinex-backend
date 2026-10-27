"""Two agents confirming the same manifest at the same time.

_save_manifest_rows documents its concurrency contract: a row lock on the
vessel serialises confirm/upload, and the partial unique indexes on
crew_assignments are "the final boundary". Nothing exercised that with real
simultaneous transactions — the existing concurrency test covers a crew member
on two vessels, not two writers racing.

These tests use genuinely committed transactions on separate connections, so
they clean up after themselves rather than rolling back.
"""
import threading
import time
import unittest
import uuid

import app.db.base  # noqa: F401
from sqlalchemy import text

from app.services import crew_manifest as cm
from app.db.models.agent_profile import AgentProfile
from app.db.models.crew_assignment import CrewAssignment
from app.db.models.user import User
from app.db.models.vessel import Vessel
from app.db.models.vessel_call import VesselCall
from app.db.models.vessel_crew import VesselCrew
from app.db.session import SessionLocal, engine

STATEMENT_TIMEOUT_MS = 10_000


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class ConcurrentManifestConfirmTests(unittest.TestCase):
    def setUp(self):
        self.tag = uuid.uuid4().hex[:8].upper()
        self.passport = f"P{self.tag}"
        self.db = SessionLocal()

        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(agent)
        self.db.flush()
        self.db.add(AgentProfile(user_id=agent.id, agency_name=_uniq("Agency"), location="Port"))
        vessel = Vessel(
            agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier",
        )
        self.db.add(vessel)
        self.db.flush()
        call = VesselCall(vessel_id=vessel.id, vessel_name=vessel.name, port_name="Concurrency Port")
        self.db.add(call)
        self.db.commit()

        self.agent_id = agent.id
        self.vessel_id = vessel.id
        self.call_id = call.id

    def tearDown(self):
        # Committed rows, so remove them in foreign-key-safe order.
        self.db.rollback()
        for statement in (
            "DELETE FROM shore_passes WHERE vessel_call_id = :call",
            "DELETE FROM crew_assignments WHERE vessel_call_id = :call",
            "DELETE FROM vessel_crew WHERE vessel_id = :vessel",
            "DELETE FROM vessel_calls WHERE vessel_id = :vessel",
            "DELETE FROM vessels WHERE id = :vessel",
            "DELETE FROM agent_profiles WHERE user_id = :agent",
            "DELETE FROM users WHERE id = :agent",
        ):
            self.db.execute(
                text(statement),
                {"call": self.call_id, "vessel": self.vessel_id, "agent": self.agent_id},
            )
        self.db.commit()
        self.db.close()

    def _confirm_in_thread(self, barrier, results, rows):
        """Run one manifest confirm on its own connection, colliding on the barrier."""
        from app.api.v1.routes_vessels import _save_manifest_rows

        session = SessionLocal()
        try:
            session.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            vessel = session.query(Vessel).filter(Vessel.id == self.vessel_id).one()
            barrier.wait(timeout=15)
            saved = _save_manifest_rows(session, vessel, rows, "Concurrency Port")
            session.commit()
            results.append(("ok", saved))
        except Exception as exc:  # noqa: BLE001 - the failure mode is the assertion
            session.rollback()
            results.append(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            session.close()

    def _race(self, rows_a, rows_b):
        barrier = threading.Barrier(2)
        results = []
        threads = [
            threading.Thread(target=self._confirm_in_thread, args=(barrier, results, rows))
            for rows in (rows_a, rows_b)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "manifest confirm deadlocked or hung")
        return results

    def _row(self, name="Race Crew", rank="AB", nationality="INDIAN", passport=None):
        return cm.ParsedCrewRow(
            name=name, rank=rank, nationality=nationality,
            passport_number=passport or self.passport,
        )

    def _active_assignments(self):
        return self.db.query(CrewAssignment).filter(
            CrewAssignment.vessel_call_id == self.call_id,
            CrewAssignment.ended_at.is_(None),
        ).all()

    def test_same_crew_confirmed_twice_at_once_yields_one_assignment(self):
        results = self._race([self._row()], [self._row()])
        self.db.expire_all()

        assignments = self._active_assignments()
        manifest_rows = self.db.query(VesselCrew).filter(
            VesselCrew.vessel_id == self.vessel_id
        ).all()

        self.assertEqual(
            len(assignments), 1,
            f"expected one active assignment, got {len(assignments)}; thread results={results}",
        )
        self.assertEqual(
            len(manifest_rows), 1,
            f"expected one manifest row, got {len(manifest_rows)}; thread results={results}",
        )

    def test_neither_writer_is_left_believing_it_failed_after_writing(self):
        """A writer that reports an error must not have left rows behind."""
        results = self._race([self._row()], [self._row()])
        self.db.expire_all()

        successes = [r for r in results if r[0] == "ok"]
        self.assertTrue(successes, f"both writers failed: {results}")
        # Whatever each writer reported, the end state is exactly one member.
        self.assertEqual(len(self._active_assignments()), 1, results)

    def test_two_different_crew_members_both_land(self):
        other = f"Q{self.tag}"
        results = self._race(
            [self._row(name="Crew One")],
            [self._row(name="Crew Two", passport=other)],
        )
        self.db.expire_all()
        self.assertEqual(
            len(self._active_assignments()), 2,
            f"both distinct crew should be saved; results={results}",
        )

    def test_a_writer_delayed_mid_confirm_still_converges_on_one_member(self):
        """Hold one writer open for two seconds inside the critical section.

        This covers the realistic case of a slow confirm (large manifest, slow
        identity resolution) overlapping a second agent's retry.

        It deliberately does NOT claim the vessel row lock is what saves us:
        measured both ways, this passes with the lock removed too. Under
        PostgreSQL's default READ COMMITTED the delayed writer sees the other's
        committed rows when it wakes and upserts idempotently, so convergence
        here comes from that plus the two-phase structure. The lock's job is to
        keep the losing writer from erroring, and the partial unique indexes on
        crew_assignments remain the real boundary.
        """
        from app.api.v1 import routes_vessels

        original = routes_vessels._resolve_profile_or_queue_conflict
        first_in = threading.Event()
        picked = threading.Lock()
        held = {"done": False}

        def slow_first(*args, **kwargs):
            with picked:
                mine = not held["done"]
                held["done"] = True
            if mine:
                first_in.set()
                time.sleep(2.0)
            return original(*args, **kwargs)

        routes_vessels._resolve_profile_or_queue_conflict = slow_first
        try:
            results = self._race([self._row()], [self._row()])
        finally:
            routes_vessels._resolve_profile_or_queue_conflict = original

        self.assertTrue(first_in.is_set(), "the delay never ran; the overlap did not happen")
        self.db.expire_all()
        self.assertEqual(
            len(self._active_assignments()), 1,
            f"two overlapping writers produced duplicate crew; results={results}",
        )
        self.assertEqual(
            self.db.query(VesselCrew).filter(VesselCrew.vessel_id == self.vessel_id).count(),
            1, f"duplicate manifest rows; results={results}",
        )
        self.assertTrue(
            all(status == "ok" for status, _ in results),
            f"the lock should keep both writers from erroring; results={results}",
        )


if __name__ == "__main__":
    unittest.main()


class ConcurrentVesselCallCreationTests(unittest.TestCase):
    """Two writers opening the same vessel's call at once.

    active_vessel_call(create=True) selects an open call and inserts one if it
    finds none, with no lock and no IntegrityError handling. The partial unique
    index uq_vessel_calls_active_vessel keeps the data correct, so the question
    is what the losing writer gets back. Reached from create_vessel, the return
    call, crew eligibility sync, and _resolve_report_call — the last meaning a
    report load can create a call, so two concurrent report loads can collide.
    """

    def setUp(self):
        self.db = SessionLocal()
        agent = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(agent)
        self.db.flush()
        self.db.add(AgentProfile(user_id=agent.id, agency_name=_uniq("Agency"), location="Port"))
        vessel = Vessel(
            agent_id=agent.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(vessel)
        self.db.commit()
        self.agent_id = agent.id
        self.vessel_id = vessel.id

    def tearDown(self):
        self.db.rollback()
        for statement in (
            "DELETE FROM crew_assignments WHERE vessel_call_id IN "
            "(SELECT id FROM vessel_calls WHERE vessel_id = :vessel)",
            "DELETE FROM vessel_calls WHERE vessel_id = :vessel",
            "DELETE FROM vessels WHERE id = :vessel",
            "DELETE FROM agent_profiles WHERE user_id = :agent",
            "DELETE FROM users WHERE id = :agent",
        ):
            self.db.execute(text(statement), {"vessel": self.vessel_id, "agent": self.agent_id})
        self.db.commit()
        self.db.close()

    def _open_call_in_thread(self, barrier, results):
        from app.services.historical_context import active_vessel_call

        session = SessionLocal()
        try:
            session.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))
            vessel = session.query(Vessel).filter(Vessel.id == self.vessel_id).one()
            barrier.wait(timeout=15)
            call = active_vessel_call(session, vessel, create=True)
            session.commit()
            results.append(("ok", call.id if call else None))
        except Exception as exc:  # noqa: BLE001 - the failure mode is the finding
            session.rollback()
            results.append(("error", type(exc).__name__))
        finally:
            session.close()

    def test_both_writers_receive_the_one_call_instead_of_an_error(self):
        barrier = threading.Barrier(2)
        results = []
        threads = [
            threading.Thread(target=self._open_call_in_thread, args=(barrier, results))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            self.assertFalse(thread.is_alive(), "opening a vessel call deadlocked or hung")

        self.db.expire_all()
        calls = self.db.query(VesselCall).filter(VesselCall.vessel_id == self.vessel_id).all()
        # The index keeps the data right either way; that is not the question.
        self.assertEqual(len(calls), 1, f"duplicate open calls; results={results}")
        self.assertTrue(
            all(status == "ok" for status, _ in results),
            f"a writer got an error instead of the existing call; results={results}",
        )
        self.assertEqual(
            {value for _, value in results}, {calls[0].id},
            f"writers disagree about which call is open; results={results}",
        )
