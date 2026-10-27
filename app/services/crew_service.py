import uuid


def generate_hpid(passport_number: str, nationality: str, port: str) -> str:
    """
    Generate HPID using pattern: HP-{passport_number}-{NAT}-{PORT}
    Examples: HP-P1234567-IN-VIS, HP-P7654321-PH-DUB

    An HPID is issued per port visit, not once per person, so the PORT fragment
    is meant to differ between visits. What was not meant to differ is the
    *rendering*: both fragments came from raw slicing, so the same person at the
    same port produced different HPIDs depending on how the source happened to
    be written -- "IN" and "INDIAN" gave IN and IND, "port_visakhapatnam" and
    "Port of Visakhapatnam" gave VIS and POR.

    They now go through the normalisers the rest of the codebase uses, so one
    visit yields one HPID. Existing rows keep their old rendering, which is why
    matching compares canonical_hpid() rather than the literal string.

    New values also avoid the ambiguity in the old ones: nationality is
    normalised to its ISO code before slicing, so INDIAN gives IN rather than
    IND -- a fragment India and Indonesia share and which cannot be resolved
    afterwards.
    """
    from app.services.crew_reference import normalize_nationality
    from app.services.port_identity import canonical_port_key

    # Use upper case passport number
    id_part = str(passport_number).strip().upper() if passport_number else "XXXX"

    # The country's canonical code, not the first three characters of whatever
    # was typed. Falls back to the old slice when the value is unrecognised, so
    # an unusual nationality still produces an identifier rather than failing.
    normalized_nationality = normalize_nationality(nationality) if nationality else None
    nat_part = normalized_nationality or (
        str(nationality)[:3].upper() if nationality else "GEN"
    )

    # The canonical port key drops "port", "of" and punctuation, so every
    # spelling of one port gives the same three characters.
    port_key = canonical_port_key(port)
    port_part = port_key[:3].upper() if port_key else "GEN"

    return f"HP-{id_part}-{nat_part}-{port_part}"


def generate_unique_hpid(db, passport_number, nationality, port, unique_fallback=None, exclude_profile_id=None) -> str:
    """
    Same HP-{...}-{NAT}-{PORT} pattern as generate_hpid(), but guaranteed
    unique against CrewProfile.hpid (a unique-constrained column).

    generate_hpid() alone falls back to the literal "XXXX" whenever
    passport_number is missing, which is IDENTICAL for every crew member
    sharing the same nationality and port — any two such crew members
    collide and the second one's save fails with a UniqueViolation.
    Disambiguate with unique_fallback (e.g. the crew's user_id, which is
    stable per crew member) whenever passport_number is missing, and fall
    back to a random suffix as a last resort if a collision still exists.
    """
    from app.db.models.crew_profile import CrewProfile

    candidate = generate_hpid(passport_number, nationality, port)
    if not passport_number or not str(passport_number).strip():
        disambiguator = unique_fallback if unique_fallback is not None else uuid.uuid4().hex[:8]
        candidate = f"{candidate}-{disambiguator}"

    query = db.query(CrewProfile).filter(CrewProfile.hpid == candidate)
    if exclude_profile_id is not None:
        query = query.filter(CrewProfile.id != exclude_profile_id)
    if query.first() is not None:
        candidate = f"{candidate}-{uuid.uuid4().hex[:4].upper()}"
    return candidate


def ensure_stable_hpid(db, profile, *, port=None) -> str:
    """Issue an HPID once and never derive a replacement from mutable profile data.

    Passport, nationality, vessel and port can legitimately change. They may be
    used as readable ingredients when an identifier is first issued, but cannot
    be allowed to change an identifier that is already referenced by incidents,
    manifests, shore passes and historical reports.
    """
    if profile.hpid:
        return profile.hpid
    profile.hpid = generate_unique_hpid(
        db,
        profile.passport_number,
        profile.nationality,
        port or profile.current_port or "port_general",
        unique_fallback=profile.user_id,
        exclude_profile_id=profile.id,
    )
    return profile.hpid
