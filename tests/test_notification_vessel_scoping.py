"""An agent's notifications belong to one of their own vessels, and to them.

Previously an agent could type any vessel name into a free-text field — or leave
it blank and reach the whole port — and the history, edit and delete checks all
matched on `port_name == assigned_port OR created_by == me`, so every agency
berthed at the same port could see and modify each other's notifications.

Runs against the configured database inside a transaction that is always rolled
back, so it leaves no rows behind.
"""

import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db.base  # noqa: F401 — registers every model on Base
from fastapi import HTTPException, Response
from sqlalchemy.orm import Session

from app.api.v1.routes_notifications import (
    NotificationCreateIn,
    NotificationUpdateIn,
    create_notification,
    delete_notification,
    get_unread_count,
    list_notifications_admin,
    list_notifications_for_crew,
    mark_notification_read,
    update_notification,
)
from app.db.models.user import User
from app.db.models.crew_profile import CrewProfile
from app.db.models.notification import Notification
from app.services.historical_context import assignment_for_manifest
from app.db.models.vessel import Vessel
from app.db.models.vessel_crew import VesselCrew
from app.db.session import engine


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class NotificationVesselScopingTests(unittest.TestCase):
    PORT = "port_test_harbour"

    def setUp(self):
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

        self.agent_a, self.vessel_a = self.make_agent()
        self.agent_b, self.vessel_b = self.make_agent()

    def tearDown(self):
        self.db.close()
        self.trans.rollback()
        self.connection.close()

    def make_agent(self):
        """An agent with one vessel, both berthed at the same port."""
        user = User(email=_uniq("agent") + "@example.com", hashed_password="x", role="agent")
        self.db.add(user)
        self.db.flush()
        vessel = Vessel(
            agent_id=user.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(vessel)
        self.db.flush()
        agent = SimpleNamespace(
            id=user.id, role="agent",
            agent_profile=SimpleNamespace(assigned_port=self.PORT),
        )
        return agent, vessel

    def send(self, agent, vessel_name):
        return create_notification(
            body=NotificationCreateIn(
                title="Gate closing", message="Return by 18:00", vessel=vessel_name
            ),
            db=self.db,
            current_user=agent,
        )

    def test_notification_is_tagged_with_the_chosen_vessel(self):
        n = self.send(self.agent_a, self.vessel_a.name)

        self.assertEqual(n.vessel, self.vessel_a.name)
        self.assertEqual(n.port_name, self.PORT)

    def test_agent_cannot_target_another_agencys_vessel(self):
        with self.assertRaises(HTTPException) as ctx:
            self.send(self.agent_a, self.vessel_b.name)

        self.assertEqual(ctx.exception.status_code, 403)

    def test_agent_must_choose_a_vessel(self):
        """Blank used to mean 'everyone at this port'."""
        with self.assertRaises(HTTPException) as ctx:
            self.send(self.agent_a, "")

        self.assertEqual(ctx.exception.status_code, 400)

    def test_vessel_can_be_given_by_imo_and_is_stored_as_the_name(self):
        # The crew feed matches on the vessel name, so that is what must be saved.
        n = self.send(self.agent_a, self.vessel_a.imo_number)

        self.assertEqual(n.vessel, self.vessel_a.name)

    def test_vessel_name_matching_is_case_insensitive(self):
        n = self.send(self.agent_a, self.vessel_a.name.upper())

        self.assertEqual(n.vessel, self.vessel_a.name)

    def test_history_shows_only_the_agents_own_notifications(self):
        self.send(self.agent_a, self.vessel_a.name)
        self.send(self.agent_b, self.vessel_b.name)
        self.send(self.agent_b, self.vessel_b.name)

        mine = list_notifications_admin(response=Response(), db=self.db, current_user=self.agent_a)

        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].vessel, self.vessel_a.name)

    def test_agent_cannot_edit_another_agents_notification(self):
        theirs = self.send(self.agent_b, self.vessel_b.name)

        with self.assertRaises(HTTPException) as ctx:
            update_notification(
                notification_id=theirs.id,
                body=NotificationUpdateIn(title="Hijacked"),
                db=self.db,
                current_user=self.agent_a,
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_agent_cannot_delete_another_agents_notification(self):
        theirs = self.send(self.agent_b, self.vessel_b.name)

        with self.assertRaises(HTTPException) as ctx:
            delete_notification(
                notification_id=theirs.id, db=self.db, current_user=self.agent_a
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_agent_cannot_retarget_a_notification_to_another_agencys_vessel(self):
        mine = self.send(self.agent_a, self.vessel_a.name)

        with self.assertRaises(HTTPException) as ctx:
            update_notification(
                notification_id=mine.id,
                body=NotificationUpdateIn(vessel=self.vessel_b.name),
                db=self.db,
                current_user=self.agent_a,
            )

        self.assertEqual(ctx.exception.status_code, 403)

    def test_all_my_vessels_snapshots_only_the_creators_owned_vessels(self):
        second = Vessel(
            agent_id=self.agent_a.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Tanker", status="Active",
        )
        self.db.add(second)
        self.db.flush()

        result = create_notification(
            body=NotificationCreateIn(
                title="Weather", message="Heavy weather expected",
                audience_type="all_agent_vessels",
            ),
            db=self.db, current_user=self.agent_a,
        )

        self.assertEqual(result.audience_type, "all_agent_vessels")
        self.assertEqual(set(result.target_vessel_ids), {self.vessel_a.id, second.id})
        self.assertNotIn(self.vessel_b.id, result.target_vessel_ids)

    def test_all_my_vessels_reaches_own_crew_but_not_other_agency(self):
        crew_user = User(
            email=_uniq("crew") + "@example.com", hashed_password="x", role="crew"
        )
        self.db.add(crew_user)
        self.db.flush()
        hpid = _uniq("HP")
        # Passport and nationality on both sides: that pair is what links a
        # manifest row to a profile (HPID spec §4.1), and the link is made when
        # the call seeds its assignments, not by the profile= argument below.
        passport = _uniq("P")
        profile = CrewProfile(
                user_id=crew_user.id, full_name="Crew", rank="able_seaman",
                nationality="IN", hpid=hpid, current_port=self.PORT,
                vessel=self.vessel_a.name, passport_number=passport,
            )
        manifest = VesselCrew(
                vessel_id=self.vessel_a.id, name="Crew", rank="able_seaman", hp_id=hpid,
                passport_number=passport, nationality="IN",
            )
        self.db.add_all([profile, manifest])
        self.db.flush()
        assignment_for_manifest(self.db, self.vessel_a, manifest, profile=profile)
        create_notification(
            body=NotificationCreateIn(
                title="Own fleet", message="For our fleet",
                audience_type="all_agent_vessels",
            ),
            db=self.db, current_user=self.agent_a,
        )
        create_notification(
            body=NotificationCreateIn(
                title="Other fleet", message="Must not leak",
                audience_type="all_agent_vessels",
            ),
            db=self.db, current_user=self.agent_b,
        )

        crew = SimpleNamespace(id=crew_user.id, role="crew")
        visible = list_notifications_for_crew(db=self.db, current_user=crew)

        self.assertEqual([item.title for item in visible], ["Own fleet"])

    def test_authorization_happens_before_notification_limit(self):
        crew_user = User(
            email=_uniq("crew") + "@example.com", hashed_password="x", role="crew"
        )
        self.db.add(crew_user)
        self.db.flush()
        profile = CrewProfile(
            user_id=crew_user.id,
            full_name="Crew",
            rank="able_seaman",
            nationality="IN",
            hpid=_uniq("HP"),
            current_port=self.PORT,
            vessel=self.vessel_a.name,
            passport_number=_uniq("P"),
        )
        manifest = VesselCrew(
            vessel_id=self.vessel_a.id,
            name="Crew",
            rank="able_seaman",
            hp_id=profile.hpid,
            passport_number=profile.passport_number,
            nationality=profile.nationality,
        )
        self.db.add_all([profile, manifest])
        self.db.flush()
        assignment_for_manifest(self.db, self.vessel_a, manifest, profile=profile)

        self.db.add(Notification(
            title="Relevant older notice",
            message="Must remain visible",
            audience_type="single_vessel",
            target_vessel_ids=[self.vessel_a.id],
        ))
        self.db.flush()
        self.db.add_all([
            Notification(
                title=f"Other vessel {index}",
                message="Not for this crew",
                audience_type="single_vessel",
                target_vessel_ids=[self.vessel_b.id],
            )
            for index in range(501)
        ])
        self.db.flush()

        visible = list_notifications_for_crew(
            db=self.db,
            current_user=SimpleNamespace(id=crew_user.id, role="crew"),
        )

        self.assertEqual([item.title for item in visible], ["Relevant older notice"])

    def test_unread_and_read_authorization_include_visible_rows_older_than_feed_limit(self):
        crew_user = User(
            email=_uniq("crew") + "@example.com", hashed_password="x", role="crew"
        )
        self.db.add(crew_user)
        self.db.flush()
        profile = CrewProfile(
            user_id=crew_user.id, full_name="Crew", rank="able_seaman",
            nationality="IN", hpid=_uniq("HP"), current_port=self.PORT,
            vessel=self.vessel_a.name, passport_number=_uniq("P"),
        )
        manifest = VesselCrew(
            vessel_id=self.vessel_a.id, name="Crew", rank="able_seaman",
            hp_id=profile.hpid,
            passport_number=profile.passport_number,
            nationality=profile.nationality,
        )
        self.db.add_all([profile, manifest])
        self.db.flush()
        assignment_for_manifest(self.db, self.vessel_a, manifest, profile=profile)
        notices = [
            Notification(
                title=f"Relevant {index}", message="For this crew",
                audience_type="single_vessel", target_vessel_ids=[self.vessel_a.id],
            )
            for index in range(501)
        ]
        self.db.add_all(notices)
        self.db.flush()
        crew = SimpleNamespace(id=crew_user.id, role="crew")

        self.assertEqual(get_unread_count(db=self.db, current_user=crew), {"count": 501})
        self.assertEqual(
            mark_notification_read(
                notification_id=notices[0].id, db=self.db, current_user=crew,
            ),
            {"status": "ok"},
        )
        self.assertEqual(get_unread_count(db=self.db, current_user=crew), {"count": 500})

    def test_crew_who_changed_ship_stop_receiving_the_old_ships_notices(self):
        """The reported leak: notices reaching vessels they were not sent to.

        Recipients were resolved by matching HPID and passport against the whole
        manifest table with no vessel filter. A crew member who joins a second
        ship is on both manifests, so they stayed a recipient for the ship they
        had left — and a notice addressed to one vessel arrived on another.
        """
        # A second ship under the same agent, so both notices are legitimate.
        new_ship = Vessel(
            agent_id=self.agent_a.id, name=_uniq("MV"), imo_number=_uniq("IMO"),
            vessel_type="Bulk Carrier", status="Active",
        )
        self.db.add(new_ship)
        self.db.flush()

        crew_user = User(
            email=_uniq("crew") + "@example.com", hashed_password="x", role="crew"
        )
        self.db.add(crew_user)
        self.db.flush()
        hpid = _uniq("HP")
        passport = _uniq("P").replace("-", "")[:12].upper()
        profile = CrewProfile(
            user_id=crew_user.id, full_name="Crew", rank="able_seaman",
            nationality="IN", hpid=hpid, passport_number=passport,
            current_port=self.PORT,
            # They have transferred to the new ship.
            vessel=new_ship.name,
        )
        self.db.add(profile)
        # The same person is still listed on both manifests.
        assignments = []
        for vessel in (self.vessel_a, new_ship):
            manifest = VesselCrew(
                vessel_id=vessel.id, name="Crew", rank="able_seaman",
                hp_id=hpid, passport_number=passport, nationality="IN",
            )
            self.db.add(manifest)
            self.db.flush()
            assignments.append(
                assignment_for_manifest(self.db, vessel, manifest, profile=profile)
            )
        self.db.flush()
        assignments[0].ended_at = datetime.now(timezone.utc) - timedelta(days=1)
        self.db.flush()

        self.send(self.agent_a, self.vessel_a.name)
        self.send(self.agent_a, new_ship.name)

        crew = SimpleNamespace(id=crew_user.id, role="crew")
        visible = list_notifications_for_crew(db=self.db, current_user=crew)

        self.assertEqual([item.vessel for item in visible], [new_ship.name])


if __name__ == "__main__":
    unittest.main()
