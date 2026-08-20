"""Vessels a superadmin created that were never given to their agency.

The superadmin vessel form sends an agency name and no agent id. Until this was
fixed the handler fell back to the superadmin's own id whenever that name failed
to resolve to a profile, so the vessel was created, reported as created, and
belonged to a user who is not an agent — invisible on the agency's dashboard and
absent from its mapped vessels.

The same divergence arrives from the edit form, which sends an agency name and
no agent id: the name changed, the holder did not, and the vessel quietly stopped
matching the agency it claims.

So the question is not who holds a vessel but whether the holder is the agency
the vessel names. A vessel naming "Other" belongs with the superadmin by design
and is not a finding. This reports the ones whose agency_name and agent_id
disagree and, with --apply, hands each to the agency it names. A name matching no
agency, or more than one, is reported and left alone.

A vessel with no agent at all is never reassigned. Archiving is what nulls
agent_id, so those vessels have been deliberately taken out of operations and
still carry the agency name they left with; handing one back would half-revive it
— holding an agent again while its status still reads Archived. They are listed
separately, with their status, and nothing is proposed for them.

It also lists the agency names that would defeat an exact-match lookup — the
ones carrying leading or trailing whitespace, and any name registered twice —
since those are what made the lookup miss in the first place.

Dry run by default. Idempotent.

Usage (from onemarinex-backend/):
    PYTHONPATH=. python scripts/repair_vessel_agency_assignment.py
    PYTHONPATH=. python scripts/repair_vessel_agency_assignment.py --apply
"""
import argparse
import sys
from collections import defaultdict

import app.db.base  # noqa: F401 — registers every model on Base

from app.db.session import SessionLocal
from app.db.models.agent_profile import AgentProfile
from app.db.models.user import User
from app.db.models.vessel import Vessel

RULE = "=" * 78


def _key(name):
    return (name or "").strip().lower()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="write the reassignments (default is a dry run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        profiles = db.query(AgentProfile).all()
        by_name = defaultdict(list)
        for p in profiles:
            by_name[_key(p.agency_name)].append(p)

        print()
        print(RULE)
        print("Agency names that defeat an exact-match lookup")
        print()
        untidy = [p for p in profiles
                  if (p.agency_name or "") != (p.agency_name or "").strip()]
        duplicated = {n: ps for n, ps in by_name.items()
                      if n and len({p.user_id for p in ps}) > 1}
        if not untidy and not duplicated:
            print("  None. Every agency name is tidy and unique.")
        for p in untidy:
            print(f"  agency {p.id}: '{p.agency_name}' has surrounding whitespace")
        for name, ps in duplicated.items():
            print(f"  '{ps[0].agency_name}' is registered by "
                  f"{len({p.user_id for p in ps})} different agents: "
                  f"{sorted({p.user_id for p in ps})}")

        # "Other" names no agency, so resting with the superadmin is correct
        # and not a finding. Reporting those was 137 lines of noise that hid
        # the question worth asking.
        unassigned = {"", "other", "others", "none", "n/a", "other agency"}
        stranded, agentless = [], []
        for v in db.query(Vessel).all():
            if _key(v.agency_name) in unassigned:
                continue
            owners = by_name.get(_key(v.agency_name), [])
            distinct = {p.user_id for p in owners}
            if len(distinct) == 1 and v.agent_id in distinct:
                continue  # holder already matches the agency it names
            if v.agent_id is None:
                # Archiving nulls agent_id. Reassigning would half-revive the
                # vessel: an agent again, with its status still Archived.
                agentless.append(v)
                continue
            stranded.append(v)

        if agentless:
            print()
            print(RULE)
            print("Vessels with no agent — archived or unlinked, left untouched")
            print()
            for v in agentless:
                print(f"  vessel {v.id:<6} '{v.name}'  IMO {v.imo_number}")
                print(f"      names '{v.agency_name}'  ·  status {v.status}")
            print()
            print("  Nothing is proposed for these. If one should be back in")
            print("  operations, bring it back through the app so its status and")
            print("  its port call move together.")

        print()
        print(RULE)
        print("Vessels whose agency_name and agent_id disagree")
        print()
        if not stranded:
            print("  None.")
            print()
            print(RULE)
            print("Read-only. Nothing above has been written.")
            print()
            return 0

        planned, blocked = [], []
        for v in stranded:
            owners = by_name.get(_key(v.agency_name), [])
            distinct = {p.user_id for p in owners}
            print(f"  vessel {v.id:<6} '{v.name}'  IMO {v.imo_number}")
            print(f"      names '{v.agency_name}' but is held by user {v.agent_id}"
                  f"  ·  status {v.status}")
            if len(distinct) == 1:
                target = owners[0].user_id
                print(f"      -> agency {owners[0].id}, agent user {target}")
                planned.append((v, target))
            elif not distinct:
                print("      -> no agency of that name; left alone")
                blocked.append(v)
            else:
                print(f"      -> {len(distinct)} agencies of that name; "
                      f"a person must choose. Left alone")
                blocked.append(v)

        print()
        print(RULE)
        print(f"{len(planned)} to reassign, {len(blocked)} left for a human")
        print()
        if not args.apply:
            print("Dry run. Re-run with --apply to write.")
            print(RULE)
            print()
            return 0

        for vessel, target in planned:
            vessel.agent_id = target
        db.commit()
        print(f"Reassigned {len(planned)} vessel(s).")
        print(RULE)
        print()
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
