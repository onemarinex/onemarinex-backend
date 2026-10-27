"""The three company roles, and what Super Admin must see to approve each.

Owner answer to Q5, 26 October 2026:

    INITIAL ROLES: Registered/Legal Owner, ISM Manager, Commercial Manager.
    Other roles (technical/crew manager, charterer) require later explicit
    definition. Super Admin verifies company and approves every
    vessel/role/effective-period claim; companies cannot self-approve.

All three specification documents use the same three, so this is the taxonomy
rather than a placeholder. What is deferred is *additional* roles, which is why
`VesselRoleClaim.role` stays a plain string column: adding one here later needs
no migration, and the overlap constraint is keyed on whatever is stored.

Document 2 is emphatic that these are distinct and must never be conflated. One
company may hold several of them on one vessel, and different companies may
hold different ones simultaneously -- the exclusion constraint refuses an
overlap only within the *same* role.

Parent and subsidiary entities are separate legal persons here. A same-role
overlap between them is still an overlap and still needs Super Admin conflict
resolution, not an automatic merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class CompanyRole:
    code: str
    label: str
    evidence: str


REGISTERED_OWNER = CompanyRole(
    code="REGISTERED_OWNER",
    label="Registered / Legal Owner",
    evidence=(
        "Flag-state or vessel registry evidence, with dated ownership records."
    ),
)

ISM_MANAGER = CompanyRole(
    code="ISM_MANAGER",
    label="ISM Manager",
    evidence=(
        "DOC / SMC / ISM evidence, with dated appointment or handover evidence "
        "as applicable."
    ),
)

COMMERCIAL_MANAGER = CompanyRole(
    code="COMMERCIAL_MANAGER",
    label="Commercial Manager",
    evidence=(
        "Dated management agreement, owner authorization, or an independently "
        "verifiable appointment."
    ),
)

COMPANY_ROLES = (REGISTERED_OWNER, ISM_MANAGER, COMMERCIAL_MANAGER)
ROLE_CODES = tuple(role.code for role in COMPANY_ROLES)
_BY_CODE = {role.code: role for role in COMPANY_ROLES}

# Spellings seen in the specifications and likely from a submission form. Kept
# deliberately small: this resolves wording, not guesses. Anything outside it
# is rejected by name so the submitter is told what is accepted, rather than
# being silently filed under the wrong role.
_ALIASES = {
    "OWNER": REGISTERED_OWNER.code,
    "REGISTERED OWNER": REGISTERED_OWNER.code,
    "LEGAL OWNER": REGISTERED_OWNER.code,
    "REGISTERED/LEGAL OWNER": REGISTERED_OWNER.code,
    "REGISTERED / LEGAL OWNER": REGISTERED_OWNER.code,
    "ISM": ISM_MANAGER.code,
    "ISM MANAGER": ISM_MANAGER.code,
    "COMMERCIAL": COMMERCIAL_MANAGER.code,
    "COMMERCIAL MANAGER": COMMERCIAL_MANAGER.code,
}

# Named in the owner's answer as needing explicit definition before use. Held
# here so a claim for one is refused with the reason rather than with a generic
# "unknown role", which would read as a typo.
DEFERRED_ROLES = {
    "TECHNICAL MANAGER", "TECHNICAL_MANAGER",
    "CREW MANAGER", "CREW_MANAGER",
    "CHARTERER",
}


class UnknownCompanyRole(ValueError):
    """A role that is not one of the three, or is one that is not defined yet."""


def normalize_role(value: Optional[str]) -> Optional[str]:
    """The canonical code for a role, or None if it is not one of the three.

    Matching is on the canonical code, so every write path must go through
    this: a claim stored as "ISM" and a query for "ISM_MANAGER" would not meet.
    """
    if value is None:
        return None
    cleaned = " ".join(str(value).strip().upper().replace("_", " ").split())
    if not cleaned:
        return None
    direct = cleaned.replace(" ", "_")
    if direct in _BY_CODE:
        return direct
    return _ALIASES.get(cleaned)


def validate_role(value: Optional[str]) -> str:
    """The canonical code, or an error naming what is accepted."""
    code = normalize_role(value)
    if code is not None:
        return code
    cleaned = " ".join(str(value or "").strip().upper().replace("_", " ").split())
    if cleaned in DEFERRED_ROLES or cleaned.replace(" ", "_") in DEFERRED_ROLES:
        raise UnknownCompanyRole(
            f"{value!r} is not defined yet. The owner's answer to Q5 lists "
            "technical/crew manager and charterer as requiring later explicit "
            "definition, including their evidence standard."
        )
    raise UnknownCompanyRole(
        f"{value!r} is not a company role. Accepted: "
        + ", ".join(f"{role.label} ({role.code})" for role in COMPANY_ROLES)
    )


def evidence_for(value: str) -> str:
    """What Super Admin must see before approving a claim for this role."""
    return _BY_CODE[validate_role(value)].evidence
