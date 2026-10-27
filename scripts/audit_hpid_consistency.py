"""What shape are the HPIDs actually in, and who would be mismatched.

The HPID is HP-{passport}-{NAT}-{PORT} and is issued per port visit, not once
per person, so the PORT fragment is *meant* to differ between visits. What was
not meant to differ is the rendering: both trailing fragments came from raw
slicing, so one person at one port produced different HPIDs depending on how the
source was written:

    nationality   "IN" -> IN       "INDIAN" -> IND
    port          "port_visakhapatnam" -> VIS    "Port of Visakhapatnam" -> POR

Matching now compares canonical_hpid(), which collapses the rendering and keeps
the port, and generate_hpid() emits normalised fragments going forward. Neither
rewrites an existing row, which is what this reports.

It also reports the fragments that cannot be resolved at all. IND is produced by
both INDIAN and INDONESIAN, so an HPID carrying it names one of two countries
and there is no way to tell which. That is a flaw in the identifier, not
something to guess past.

Read-only. Dry run is the only mode; it writes nothing and takes no arguments.

Usage (from onemarinex-backend/):
    PYTHONPATH=. python scripts/audit_hpid_consistency.py
"""
import collections
import sys

import app.db.base  # noqa: F401 — registers every model on Base

from sqlalchemy import text

from app.db.session import SessionLocal
from app.services.crew_identity import (
    AMBIGUOUS_NATIONALITY_FRAGMENTS,
    PLACEHOLDER_PASSPORTS,
    canonical_hpid,
    hpid_is_conforming,
)

SOURCES = [
    ("crew_profiles", "hpid", "id"),
    ("vessel_crew", "hp_id", "id"),
    ("crew_assignments", "hpid", "id"),
]

# Substrings that are placeholder *text*, never part of a real passport. The
# PLACEHOLDER_PASSPORTS set is compared exactly against the passport segment
# instead: it contains "0" and "-", which appear in almost every identifier and
# flagged all of them when matched as substrings.
PLACEHOLDER_SUBSTRINGS = ("XXXX", "BUGBASH", "NOT_PROVIDED", "NOTPROVIDED", "TEST")


def _is_placeholder(canonical: str) -> bool:
    """Whether the passport inside an HPID is a value someone typed to move on."""
    passport = canonical.split("-", 1)[1] if "-" in canonical else canonical
    if passport in PLACEHOLDER_PASSPORTS:
        return True
    return any(marker in passport for marker in PLACEHOLDER_SUBSTRINGS)


def _rows(db, table, column, key):
    return db.execute(text(
        f"SELECT {key} AS key, {column} AS value FROM {table} "
        f"WHERE {column} IS NOT NULL"
    )).mappings().all()


def main():
    db = SessionLocal()
    findings = collections.Counter()
    try:
        for table, column, key in SOURCES:
            rows = _rows(db, table, column, key)
            print(f"\n=== {table}.{column} — {len(rows)} non-null row(s) ===")

            not_an_hpid, malformed, placeholder = [], [], []
            canonical_to_rows = collections.defaultdict(list)

            for row in rows:
                value = row["value"]
                canonical = canonical_hpid(value)
                if canonical is None:
                    not_an_hpid.append((row["key"], value))
                    continue
                if not hpid_is_conforming(value):
                    malformed.append((row["key"], value))
                if _is_placeholder(canonical):
                    placeholder.append((row["key"], value))
                canonical_to_rows[canonical].append((row["key"], value))

            def report(label, items, limit=8):
                print(f"  {label}: {len(items)}")
                findings[label] += len(items)
                for key_value, value in items[:limit]:
                    print(f"      id={key_value}  {value}")
                if len(items) > limit:
                    print(f"      ... and {len(items) - limit} more")

            report("not an HPID at all", not_an_hpid)
            report("missing the nationality/port fragments", malformed)
            report("placeholder text inside the identifier", placeholder)

            # One issuance recorded under two spellings.
            split = {
                canonical: entries
                for canonical, entries in canonical_to_rows.items()
                if len({str(v).upper() for _, v in entries}) > 1
            }
            print(f"  one issuance under more than one spelling: {len(split)}")
            findings["one issuance under more than one spelling"] += len(split)
            for canonical, entries in list(split.items())[:8]:
                spellings = sorted({str(v).upper() for _, v in entries})
                print(f"      {canonical} <- {', '.join(spellings)}")

            ambiguous = [
                (row["key"], row["value"]) for row in rows
                if str(row["value"]).strip().upper().split("-")[-2:-1]
                and str(row["value"]).strip().upper().split("-")[-2]
                in AMBIGUOUS_NATIONALITY_FRAGMENTS
            ]
            report("nationality fragment names two countries", ambiguous)

        print("\n=== totals ===")
        for label, count in findings.most_common():
            print(f"  {label}: {count}")
        if not sum(findings.values()):
            print("  Nothing to report.")
        print(
            "\nNothing was changed. Matching already collapses the renderings it\n"
            "safely can, so most of these are a tidiness and retirement question\n"
            "rather than a correctness one. The exception is the ambiguous\n"
            "nationality fragment: those rows name one of two countries and no\n"
            "amount of code can tell which. Rewriting any of it is a production\n"
            "data change and needs its own decision."
        )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
