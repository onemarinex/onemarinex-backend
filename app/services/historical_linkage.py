"""Attach a seafarer to the voyages they sailed before they had an account.

The read-only audit left this open: the registration sync "searches current
manifest rows and materializes an active-call assignment. It does not by
itself prove that a crew account created after departure can be attached to
every correct historical assignment."

It does not, and the reason is structural rather than a missing branch.
`sync_crew_manifest_helper` walks `VesselCrew`, and **`VesselCrew` is a current
roster, not a history**: every row for a vessel is deleted when it departs
(`routes_vessels.py`, the departure path, which says so and is right to --
a finished call's roster lives in its own `crew_assignments` rows). From there
`assignment_for_manifest` goes through `active_vessel_call`, which filters
`VesselCall.ended_at IS NULL`.

So a seafarer who registers a month after signing off searches a roster that
no longer lists them, for a call that is no longer open. Their assignments
exist, carry their passport and nationality, and keep `crew_profile_id = NULL`
for ever. Their own history is invisible to them.

This closes that, and only that:

  - it **attaches**, never creates. No assignment, vessel call or manifest row
    is made here, so no voyage is invented for anyone;
  - it writes `crew_profile_id` **only where it is NULL**. An assignment that
    already belongs to somebody is never touched, and nothing else on the row
    is modified;
  - it uses the owner's own matching rule -- normalised passport **and**
    nationality, never the legacy HPID -- which is what Q2 settled and what
    `crew_profile_for_manifest` already applies to live manifests. A past
    voyage is held to the same standard as a current one, not a weaker one;
  - where the passport matches but the identity is **ambiguous**, it attaches
    nothing and raises an identity conflict instead, so a Super Admin decides
    on evidence. That queue and its screen already exist.

One assignment per person per call still holds: an assignment is skipped if
the profile already holds one on that call.
"""

from __future__ import annotations

import logging
from typing import List

from sqlalchemy.orm import Session

from app.db.models.crew_assignment import CrewAssignment
from app.db.models.crew_profile import CrewProfile
from app.services.crew_identity import (
    normalize_passport_number,
    normalized_passport_expression,
    persist_identity_conflict,
)
from app.services.historical_context import normalize_nationality

logger = logging.getLogger("heyports.historical_linkage")

# A passport that matches this many unlinked historical assignments with
# disagreeing identities is not evidence of anything. Raise it rather than
# guess, the same way live manifest matching refuses a multi-candidate match.
OPERATION = "HISTORICAL_LINKAGE"


def _nationality_agrees(profile: CrewProfile, assignment: CrewAssignment) -> bool:
    """Both sides normalised, with an unrecorded nationality counting as agreement.

    An assignment written before nationality was captured carries none. That
    is an absence of evidence, not evidence of a different person, and the
    passport still had to match to get here.
    """
    assignment_nationality = normalize_nationality(assignment.nationality)
    if not assignment_nationality:
        return True
    return assignment_nationality == normalize_nationality(profile.nationality)


def candidate_assignments(db: Session, profile: CrewProfile) -> List[CrewAssignment]:
    """Unlinked assignments whose passport matches this profile's."""
    passport = normalize_passport_number(profile.passport_number)
    if not passport:
        return []
    return (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.crew_profile_id.is_(None),
            normalized_passport_expression(CrewAssignment.passport_number) == passport,
        )
        .order_by(CrewAssignment.id)
        .all()
    )


def link_historical_assignments(db: Session, profile: CrewProfile) -> dict:
    """Attach `profile` to the assignments that are unambiguously theirs.

    Returns a summary rather than raising: registration must not fail because
    a historical record is untidy. The caller commits.
    """
    passport = normalize_passport_number(profile.passport_number)
    summary = {"linked": [], "skipped_occupied_call": [], "conflicts": []}
    if not passport:
        return summary

    candidates = candidate_assignments(db, profile)
    disagreeing = [a for a in candidates if not _nationality_agrees(profile, a)]
    agreeing = [a for a in candidates if _nationality_agrees(profile, a)]

    for assignment in agreeing:
        # One person, one open assignment per call -- the rule the live path
        # enforces too. Linking here would create a second.
        held = (
            db.query(CrewAssignment.id)
            .filter(
                CrewAssignment.vessel_call_id == assignment.vessel_call_id,
                CrewAssignment.crew_profile_id == profile.id,
                CrewAssignment.id != assignment.id,
            )
            .first()
        )
        if held is not None:
            summary["skipped_occupied_call"].append(assignment.id)
            continue
        assignment.crew_profile_id = profile.id
        summary["linked"].append(assignment.id)

    if disagreeing:
        # The passport matches and the nationality does not. That is exactly
        # the case the HPID spec says must not be resolved by guessing, so it
        # goes to the queue a person reads.
        summary["conflicts"] = _raise_conflicts(db, profile, disagreeing)

    db.flush()
    logger.info(
        "historical linkage for profile %s: linked=%d conflicts=%d skipped=%d",
        profile.id, len(summary["linked"]), len(summary["conflicts"]),
        len(summary["skipped_occupied_call"]),
    )
    return summary


def _raise_conflicts(db: Session, profile: CrewProfile, assignments) -> List[int]:
    raised = []
    seen_vessels = set()
    for assignment in assignments:
        vessel_id = _vessel_id_for(db, assignment)
        if vessel_id is None or vessel_id in seen_vessels:
            continue
        seen_vessels.add(vessel_id)
        record = persist_identity_conflict(
            db,
            operation=OPERATION,
            vessel_id=vessel_id,
            passport_number=profile.passport_number,
            proposed_identity={
                "full_name": profile.full_name,
                "nationality": profile.nationality,
                "passport_number": profile.passport_number,
                "crew_profile_id": profile.id,
                "assignment_id": assignment.id,
                "assignment_name": assignment.crew_name,
                "assignment_nationality": assignment.nationality,
            },
            message=(
                "A seafarer registered after this voyage ended and their passport "
                f"matches assignment {assignment.id}, but the nationality recorded "
                f"on that assignment ({assignment.nationality or 'none'}) does not "
                f"match the one on their account ({profile.nationality or 'none'}). "
                "The assignment has not been attached to anyone."
            ),
        )
        raised.append(record.id)
    return raised


def _vessel_id_for(db: Session, assignment: CrewAssignment):
    from app.db.models.vessel_call import VesselCall

    return (
        db.query(VesselCall.vessel_id)
        .filter(VesselCall.id == assignment.vessel_call_id)
        .scalar()
    )


def apply_resolved_conflict(db: Session, record) -> List[int]:
    """Attach the assignment a Super Admin just decided on.

    `resolve_identity_conflict` records the decision -- status, chosen profile,
    evidence, audit row -- and deliberately does not act on it; other
    operations consult `resolved_identity_decision()` the next time they run.
    A historical assignment has no next time: nothing re-reads it, so a
    resolution with no consequence is a decision that silently does nothing.

    Narrow on purpose. Only HISTORICAL_LINKAGE records, only SELECT_PROFILE,
    only the one assignment the record names, and only while it is still
    unclaimed. Returns the assignment ids attached.
    """
    if record.operation != OPERATION or record.resolution_action != "SELECT_PROFILE":
        return []
    profile_id = record.selected_profile_id
    if profile_id is None:
        return []

    assignment_id = (record.proposed_identity or {}).get("assignment_id")
    if assignment_id is None:
        return []

    assignment = (
        db.query(CrewAssignment)
        .filter(
            CrewAssignment.id == assignment_id,
            CrewAssignment.crew_profile_id.is_(None),
        )
        .first()
    )
    if assignment is None:
        # Already claimed, by this person or another. The reviewer's decision
        # does not override an existing link; that is a correction, which is a
        # different operation with its own evidence.
        return []

    held = (
        db.query(CrewAssignment.id)
        .filter(
            CrewAssignment.vessel_call_id == assignment.vessel_call_id,
            CrewAssignment.crew_profile_id == profile_id,
            CrewAssignment.id != assignment.id,
        )
        .first()
    )
    if held is not None:
        return []

    assignment.crew_profile_id = profile_id
    db.flush()
    logger.info(
        "historical linkage conflict %s resolved: assignment %s attached to profile %s",
        record.id, assignment.id, profile_id,
    )
    return [assignment.id]
