"""Deciding a vendor's verification, which nothing could do.

A vendor creates a profile (`pending`), uploads a trade licence, GST or VAT
certificate, bank details and ISO certifications (`under_review`), and then
nothing happens, ever. `verification_status` is written in exactly two places
and read in one — a status endpoint the vendor polls. No route sets `verified`
or `rejected`, no route returns the documents, and no screen shows them.

So the application collects four identity and banking documents per vendor and
provides no way to look at them or act on them. The verification it asks for
cannot be performed on the evidence it gathers, and a vendor sits at
`under_review` indefinitely with no way to tell that nobody is coming.

This is the decision half, deliberately shaped like `company_verification`:

  - the transitions are explicit, so a rejected vendor cannot be quietly
    flipped to verified without going back through review;
  - verifying requires an evidence type and reference, because "what did you
    look at" is the whole point of a verification record;
  - rejecting requires a reason, and that reason is what the vendor is shown.
    A refusal nobody can act on is the dead end this is fixing, in a new form;
  - nothing here reads or returns a document. Fetching those is the route's
    job, through `storage.resolve`, so the signed URL is minted at the moment
    a reviewer asks rather than stored anywhere.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("heyports.vendor_verification")

PENDING = "pending"
UNDER_REVIEW = "under_review"
VERIFIED = "verified"
REJECTED = "rejected"

DOCUMENT_FIELDS = (
    ("trade_license_url", "Trade licence"),
    ("gst_vat_url", "GST / VAT certificate"),
    ("bank_details_url", "Bank details"),
    ("iso_certifications_url", "ISO certifications"),
)

ALLOWED_TRANSITIONS = {
    PENDING: frozenset({UNDER_REVIEW, REJECTED}),
    UNDER_REVIEW: frozenset({VERIFIED, REJECTED}),
    # A decision can be revisited, but only by going back through review, so
    # there is always a fresh look at the evidence behind the current state.
    VERIFIED: frozenset({UNDER_REVIEW}),
    REJECTED: frozenset({UNDER_REVIEW}),
}


class VendorDecisionError(Exception):
    """A decision that the current state does not allow."""


def _transition(profile, target: str) -> None:
    current = (profile.verification_status or PENDING).strip().lower()
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise VendorDecisionError(
            f"A vendor that is {current} cannot become {target}. "
            f"From {current} the available decisions are: "
            f"{', '.join(sorted(allowed)) or 'none'}."
        )
    profile.verification_status = target


def documents_present(profile) -> list:
    """Which of the four documents this vendor has actually uploaded."""
    return [
        {"field": field, "label": label, "stored_ref": getattr(profile, field)}
        for field, label in DOCUMENT_FIELDS
        if getattr(profile, field)
    ]


def _tell_the_vendor(profile, message: str) -> None:
    """Append to the notes the vendor themselves can read.

    `GET /vendor/verification-status` returns this field to the vendor, so it
    carries the decision and the reason for it and nothing else. Who decided,
    and what evidence they checked, is an internal record and must not arrive
    here by accident.
    """
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    entry = f"[{when}] {message}".strip()
    profile.verification_notes = (
        f"{profile.verification_notes}\n{entry}" if profile.verification_notes else entry
    )


def _record_internally(profile, *, actor_id: int, evidence: str) -> None:
    """The reviewer and the evidence, which the vendor does not see.

    A log line rather than a row, because there is no audit table for vendor
    verification and adding one is a schema change and an owner decision. It
    is enough to answer who verified a vendor and on what, which is the
    question an audit asks.
    """
    logger.info(
        "vendor.verification vendor=%s status=%s actor=%s evidence=%s",
        profile.id, profile.verification_status, actor_id, evidence or "(none)",
    )


def verify(profile, *, actor_id: int, evidence_type: str, evidence_reference: Optional[str],
           reason: str):
    """The only path to `verified`."""
    evidence_type = (evidence_type or "").strip()
    if not evidence_type:
        raise VendorDecisionError(
            "Verifying a vendor needs the type of evidence that was checked — "
            "a verification with no record of what was looked at is not one."
        )
    if not documents_present(profile):
        raise VendorDecisionError(
            "This vendor has uploaded no documents, so there is nothing to verify "
            "them against."
        )
    _transition(profile, VERIFIED)
    reference = f" ({evidence_reference.strip()})" if (evidence_reference or "").strip() else ""
    _tell_the_vendor(profile, f"Verified. {reason}".strip())
    _record_internally(profile, actor_id=actor_id, evidence=f"{evidence_type}{reference}")
    return profile


def reject(profile, *, actor_id: int, reason: str):
    """Refuse, with a reason the vendor is shown."""
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise VendorDecisionError(
            "Rejecting a vendor needs a reason they can act on, of at least 10 "
            "characters. Leaving them at a bare refusal is the dead end this "
            "screen exists to remove."
        )
    _transition(profile, REJECTED)
    _tell_the_vendor(profile, f"Rejected: {reason}")
    _record_internally(profile, actor_id=actor_id, evidence="")
    return profile


def reopen(profile, *, actor_id: int, reason: str):
    """Send a decided vendor back for another look."""
    _transition(profile, UNDER_REVIEW)
    _tell_the_vendor(profile, (reason or "").strip() or "Reopened for review.")
    _record_internally(profile, actor_id=actor_id, evidence="")
    return profile
