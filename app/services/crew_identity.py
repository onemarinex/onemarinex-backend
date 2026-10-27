"""Identity rules for adding a person to a vessel call.

Passport numbers in historical data differ in case and whitespace, while an
HPID may differ because older HPIDs included the person's port and nationality.
Neither difference should create another manifest row.  Conversely, a passport
shared by several profiles is not safe evidence for choosing one account.

These helpers deliberately refuse ambiguous matches.  They never merge crew
profiles or guess which account owns a passport; that requires an audited
identity-reconciliation decision.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.db.models.crew_profile import CrewProfile
from app.db.models.crew_identity_conflict import CrewIdentityConflictRecord
from app.services.crew_reference import COUNTRY_ALIASES, normalize_nationality


class CrewIdentityConflict(ValueError):
    """The supplied identity points at conflicting existing records."""


def normalize_passport_number(value: Optional[str]) -> Optional[str]:
    """Return the canonical comparison/storage form for a passport number.

    Spaces are presentation noise in passport identifiers, including spaces
    introduced by OCR.  Punctuation is retained because removing it could make
    two genuinely different legacy identifiers compare equal.
    """

    if value is None:
        return None
    normalized = "".join(str(value).strip().upper().split())
    return normalized or None


def normalized_passport_expression(column):
    """SQL equivalent of :func:`normalize_passport_number` for existing rows."""

    # Existing production values contain ordinary spaces.  New writes are
    # canonical, so this expression is primarily a compatibility bridge.
    return func.replace(func.upper(func.trim(column)), " ", "")



# --- HPID shape -------------------------------------------------------------
#
# The HPID is HP-{passport}-{NAT}-{PORT}, and it is **not** permanent. A crew
# member is issued a new one each time they come to a port, so the PORT
# fragment is meant to differ between visits. Two HPIDs that differ only in the
# port are two visits by one person, not a duplicate to be merged.
#
# What is *not* meant to differ is the rendering. The nationality fragment came
# from slicing whatever was stored -- "IN" gave IN, "INDIAN" gave IND -- so the
# same person at the same port produced two different HPIDs depending on how
# their nationality happened to be recorded. That is the instability worth
# removing, and it is all this canonicalisation touches.

HPID_PREFIX = "HP"

# Fragment -> ISO code, built from the aliases the rest of the codebase uses.
# Needed because the fragment is the alias's first three characters and cannot
# be trimmed back: FILIPINO gives FIL for PH, UKRAINIAN gives UKR for UA.
#
# A fragment produced by more than one country is dropped rather than guessed.
# IND is the one that matters: INDIAN and INDONESIAN both give it, so an HPID
# carrying IND cannot be resolved to IN or ID. Those rows stay un-collapsed and
# are reported by scripts/audit_hpid_consistency.py -- it is a flaw in the
# identifier's design, and inventing an answer would silently merge two
# nationalities.
def _build_nationality_fragments():
    candidates = {}
    for alias, code in COUNTRY_ALIASES.items():
        candidates.setdefault(alias[:3].upper(), set()).add(code)
    fragments = {
        fragment: next(iter(codes))
        for fragment, codes in candidates.items()
        if len(codes) == 1
    }
    # An ISO code is always itself, even where it collides with a name prefix.
    for code in COUNTRY_ALIASES.values():
        fragments[code.upper()] = code
    return fragments


def _ambiguous_fragments():
    candidates = {}
    for alias, code in COUNTRY_ALIASES.items():
        candidates.setdefault(alias[:3].upper(), set()).add(code)
    return frozenset(f for f, codes in candidates.items() if len(codes) > 1)


# GRE, IND, NOR, REP, SOU, UNI — each produced by two or more countries.
AMBIGUOUS_NATIONALITY_FRAGMENTS = _ambiguous_fragments()

_NATIONALITY_FRAGMENTS = _build_nationality_fragments()


def canonical_nationality_fragment(fragment: Optional[str]) -> str:
    """One spelling for a nationality fragment: IND and IN both give IN."""
    if not fragment:
        return ""
    text = str(fragment).strip().upper()
    return _NATIONALITY_FRAGMENTS.get(text, text)


def canonical_hpid(value: Optional[str]) -> Optional[str]:
    """The comparable form of one HPID, keeping the port it was issued at.

    `HP-R1522877-IND-VIS` and `HP-R1522877-IN-VIS` are one visit by one person
    and reduce to the same value. `HP-R1522877-IN-KRI` is a different visit and
    deliberately does not. Returns None for anything that is not an HPID -- the
    eight crew_profiles rows carrying identifiers like `ITEM46-0-005C` come from
    a retired product and must not be coerced into looking like one.
    """
    if value is None:
        return None
    parts = [p for p in str(value).strip().upper().split("-") if p != ""]
    if len(parts) < 2 or parts[0] != HPID_PREFIX:
        return None
    if len(parts) >= 4:
        passport = normalize_passport_number("-".join(parts[1:-2]))
        nationality = canonical_nationality_fragment(parts[-2])
        port = parts[-1]
    else:
        # Malformed: no nationality or port to canonicalise. Kept comparable
        # rather than discarded, and reported by scripts/audit_hpid_consistency.
        passport = normalize_passport_number("-".join(parts[1:]))
        nationality = port = ""
    if not passport:
        return None
    return "-".join(part for part in (HPID_PREFIX, passport, nationality, port) if part)


def hpids_match(left: Optional[str], right: Optional[str]) -> bool:
    """Whether two HPIDs are the same issuance, whatever their rendering.

    Not whether they are the same person: a person has one HPID per port visit,
    and comparing across visits is the passport's job, not this one's.
    """
    canonical_left = canonical_hpid(left)
    return canonical_left is not None and canonical_left == canonical_hpid(right)


def hpid_is_conforming(value: Optional[str]) -> bool:
    """Whether a stored HPID has the full HP-{passport}-{NAT}-{PORT} shape."""
    if value is None:
        return False
    parts = [p for p in str(value).strip().upper().split("-") if p != ""]
    return len(parts) >= 4 and parts[0] == HPID_PREFIX


# Values that were typed to get past the field rather than to identify anyone.
# Production holds accounts under "U" and "NOT_PROVIDED", and because the HPID
# is derived from the passport, those became permanent identities: HP-U-IN-VIS,
# HP-NOT_PROVIDED-IN-MUM. Worse, they collide — three different people share
# "U" — which is why manifest matching cannot trust a passport on its own.
PLACEHOLDER_PASSPORTS = frozenset({
    "NOTPROVIDED", "NOT_PROVIDED", "NOTAVAILABLE", "NOTGIVEN",
    "NA", "N/A", "NIL", "NONE", "NULL", "UNKNOWN", "TEST", "TESTING",
    "DUMMY", "SAMPLE", "PENDING", "TBD", "TBA", "-", "--", "---",
    "0", "00", "000", "0000", "00000", "000000",
})

# Real passport numbers run to six characters or more; the shortest national
# formats are six. Anything shorter is a keystroke, not an identifier.
MINIMUM_PASSPORT_LENGTH = 5


def validate_passport_number(value: Optional[str]) -> str:
    """The canonical form of a passport that could plausibly identify someone.

    Registration performed no check at all, so these values arrived through
    sign-up and then propagated into HPIDs. Rejecting them at the door is what
    makes a uniqueness rule reachable: the existing duplicates are entirely
    placeholder values, not people who registered twice.
    """
    passport = normalize_passport_number(value)
    if not passport:
        raise CrewIdentityConflict("A passport number is required")

    stripped = "".join(character for character in passport if character.isalnum())
    if passport in PLACEHOLDER_PASSPORTS or stripped in PLACEHOLDER_PASSPORTS:
        raise CrewIdentityConflict(
            "Enter the passport number shown on your passport"
        )
    if len(stripped) < MINIMUM_PASSPORT_LENGTH:
        raise CrewIdentityConflict(
            "That passport number is too short to be valid"
        )
    if not any(character.isdigit() for character in stripped):
        raise CrewIdentityConflict(
            "A passport number contains at least one digit"
        )
    return passport


def passport_already_registered(
    db: Session,
    passport: str,
    *,
    exclude_profile_id: Optional[int] = None,
) -> Optional[CrewProfile]:
    """The account already holding this passport, if there is one.

    One passport is one person, so a second account under it is either the same
    person registering twice — who should recover the first — or a passport
    typed wrongly. Neither is resolved by creating the account.
    """
    query = db.query(CrewProfile).filter(
        normalized_passport_expression(CrewProfile.passport_number)
        == normalize_passport_number(passport)
    )
    if exclude_profile_id is not None:
        query = query.filter(CrewProfile.id != exclude_profile_id)
    return query.order_by(CrewProfile.id).first()


def resolve_verified_crew_profile(
    db: Session,
    *,
    passport_number: str,
    nationality: str,
    crew_name: Optional[str] = None,
    generated_hpid: Optional[str] = None,
) -> Optional[CrewProfile]:
    """Resolve one verified account or return ``None`` for a pending member.

    A unique normalized passport plus matching nationality can recover an
    account whose old HPID used a different port spelling.  More than one
    passport match, disagreement between HPID and passport, or conflicting
    nationality is returned as a reconciliation conflict instead of being
    silently linked.
    """

    passport = normalize_passport_number(passport_number)
    if not passport:
        raise CrewIdentityConflict("A passport number is required")

    passport_matches = (
        db.query(CrewProfile)
        .filter(normalized_passport_expression(CrewProfile.passport_number) == passport)
        .order_by(CrewProfile.id)
        .limit(3)
        .all()
    )
    if len(passport_matches) > 1:
        raise CrewIdentityConflict(
            "This passport matches multiple crew accounts and requires "
            "Superadmin identity reconciliation"
        )

    hpid_match = None
    if generated_hpid:
        hpid_matches = (
            db.query(CrewProfile)
            .filter(
                func.upper(func.trim(CrewProfile.hpid))
                == generated_hpid.strip().upper()
            )
            .order_by(CrewProfile.id)
            .limit(2)
            .all()
        )
        if len(hpid_matches) > 1:  # Defensive: hpid is expected to be unique.
            raise CrewIdentityConflict(
                "This HPID matches multiple crew accounts and requires "
                "Superadmin identity reconciliation"
            )
        hpid_match = hpid_matches[0] if hpid_matches else None

    passport_match = passport_matches[0] if passport_matches else None
    if hpid_match and passport_match and hpid_match.id != passport_match.id:
        raise CrewIdentityConflict(
            "The passport and HPID identify different crew accounts; "
            "Superadmin identity reconciliation is required"
        )

    profile = passport_match or hpid_match
    if profile is None:
        return None

    stored_passport = normalize_passport_number(profile.passport_number)
    if stored_passport and stored_passport != passport:
        raise CrewIdentityConflict(
            "The matched crew account has a different passport; "
            "Superadmin identity reconciliation is required"
        )

    stored_nationality = normalize_nationality(profile.nationality, strict=False)
    proposed_nationality = normalize_nationality(nationality, strict=False)
    if stored_nationality and stored_nationality != proposed_nationality:
        raise CrewIdentityConflict(
            "The passport matches an account with a different nationality; "
            "Superadmin identity reconciliation is required"
        )
    # The name is deliberately not compared.
    #
    # A passport number identifies a person; a name is how one was typed. Crew
    # register themselves as "MARIMUTHU", agents type "MARIMUTHU S", and the two
    # stopped being the same person — so a matching passport queued a Superadmin
    # reconciliation and the crew member got no shore pass over a spelling. The
    # passport, the nationality and the HPID still have to agree.
    #
    # crew_name is still accepted so callers need not change and so it keeps
    # reaching the conflict record, where a human reconciling one can still see
    # which name was submitted.
    return profile


def normalized_person_name(value: Optional[str]) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def identity_candidate_profile_ids(db: Session, passport_number: str) -> list[int]:
    passport = normalize_passport_number(passport_number)
    if not passport:
        return []
    return [
        row[0]
        for row in db.query(CrewProfile.id)
        .filter(normalized_passport_expression(CrewProfile.passport_number) == passport)
        .order_by(CrewProfile.id)
        .all()
    ]


def identity_fingerprint(proposed_identity: dict) -> str:
    """Stable identity evidence key used to scope a human decision.

    Rank is deliberately excluded: rank changes throughout a seafarer's
    career.  Name, nationality and passport must all describe the same retry;
    a decision for one person must never authorize a different person who
    later presents the same reused passport.
    """

    payload = {
        "name": normalized_person_name(proposed_identity.get("name")),
        "nationality": normalize_nationality(
            proposed_identity.get("nationality"), strict=False
        ),
        "passport_number": normalize_passport_number(
            proposed_identity.get("passport_number")
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def resolved_identity_decision(
    db: Session,
    *,
    operation: str,
    vessel_id: int,
    passport_number: str,
    proposed_identity: dict,
) -> tuple[bool, Optional[CrewProfile]]:
    """Return an audited Superadmin decision for a retry, if one exists."""

    passport = normalize_passport_number(passport_number)
    row = (
        db.query(CrewIdentityConflictRecord)
        .filter(
            CrewIdentityConflictRecord.operation == operation,
            CrewIdentityConflictRecord.vessel_id == vessel_id,
            CrewIdentityConflictRecord.passport_key == passport,
            CrewIdentityConflictRecord.identity_fingerprint
            == identity_fingerprint(proposed_identity),
            CrewIdentityConflictRecord.status == "RESOLVED",
        )
        .order_by(CrewIdentityConflictRecord.resolved_at.desc(), CrewIdentityConflictRecord.id.desc())
        .first()
    )
    if row is None:
        return False, None
    if row.resolution_action == "LEAVE_PENDING":
        return True, None
    if row.resolution_action == "SELECT_PROFILE" and row.selected_profile_id:
        return True, db.query(CrewProfile).filter(
            CrewProfile.id == row.selected_profile_id
        ).first()
    return False, None


def persist_identity_conflict(
    db: Session,
    *,
    operation: str,
    vessel_id: int,
    passport_number: str,
    proposed_identity: dict,
    message: str,
) -> CrewIdentityConflictRecord:
    """Persist or refresh one open queue item, without touching identities."""

    passport = normalize_passport_number(passport_number)
    fingerprint = identity_fingerprint(proposed_identity)
    # Serialize concurrent reports of this exact ambiguity so retries update
    # one durable queue item instead of racing two OPEN records.
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"crew-identity:{vessel_id}:{passport}:{fingerprint}"},
        )
    existing = (
        db.query(CrewIdentityConflictRecord)
        .filter(
            CrewIdentityConflictRecord.vessel_id == vessel_id,
            CrewIdentityConflictRecord.passport_key == passport,
            CrewIdentityConflictRecord.identity_fingerprint == fingerprint,
            CrewIdentityConflictRecord.status == "OPEN",
        )
        .order_by(CrewIdentityConflictRecord.id.desc())
        .first()
    )
    candidates = identity_candidate_profile_ids(db, passport)
    if existing:
        existing.operation = operation
        existing.proposed_identity = proposed_identity
        existing.candidate_profile_ids = candidates
        existing.conflict_message = message
        existing.version += 1
        row = existing
    else:
        row = CrewIdentityConflictRecord(
            operation=operation,
            vessel_id=vessel_id,
            passport_key=passport,
            identity_fingerprint=fingerprint,
            proposed_identity=proposed_identity,
            candidate_profile_ids=candidates,
            conflict_message=message,
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    return row
