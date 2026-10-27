"""Issue an opaque public reference to crew assignments that have none.

Migration u2v3w4x5y6z7 adds `crew_assignments.public_ref` and its unique index
and stops there, deliberately: deploying a schema change must not rewrite rows
in an existing database. This is the row-writing half, separated so it is run
on purpose, watched while it runs, and stopped without consequence.

What it writes: `public_ref`, and only where it is currently NULL. It reads no
other column and changes no other column. A row that already has a reference is
never touched, because a reference may already have been handed to a driver or
a cab provider and re-issuing it would break their link.

Stopping is safe. Every batch is committed before the next one starts, so an
interrupted run leaves the rows it finished and the rest still NULL — which is
exactly the state the application already handles, since a null reference is
what every assignment had before this feature existed. Re-running continues
from where it stopped.

Dry run by default. Idempotent.

Usage (from onemarinex-backend/):
    PYTHONPATH=. python scripts/backfill_assignment_public_ref.py
    PYTHONPATH=. python scripts/backfill_assignment_public_ref.py --apply
    PYTHONPATH=. python scripts/backfill_assignment_public_ref.py --apply --limit 500
    PYTHONPATH=. python scripts/backfill_assignment_public_ref.py --apply --batch 200
"""
import argparse
import sys

import app.db.base  # noqa: F401 — registers every model on Base

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.services.assignment_ref import generate_assignment_ref

# Attempts at a free value for one row before giving up. At 128 bits a single
# clash is already implausible; five in a row means something is wrong with the
# generator, not with luck, and the run should stop rather than spin.
_ATTEMPTS = 5


def _counts(db):
    row = db.execute(text(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE public_ref IS NULL) AS missing "
        "FROM crew_assignments"
    )).one()
    return row.total, row.missing


def _issue(db, assignment_id):
    """Give one row a reference. Returns False if the row was taken already.

    The unique index is the authority, not the pre-check: two runs against the
    same database would both see a free value and only one would win. A loser
    retries with a new value.
    """
    for _ in range(_ATTEMPTS):
        candidate = generate_assignment_ref()
        try:
            with db.begin_nested():
                updated = db.execute(text(
                    "UPDATE crew_assignments SET public_ref = :ref "
                    "WHERE id = :id AND public_ref IS NULL"
                ), {"ref": candidate, "id": assignment_id}).rowcount
        except IntegrityError:
            continue          # the value was taken between generating and writing
        return updated == 1   # 0 means another run reached this row first
    raise RuntimeError(
        f"Could not find an unused reference for assignment {assignment_id} "
        f"in {_ATTEMPTS} attempts"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="write the references (default is a dry run)")
    parser.add_argument("--batch", type=int, default=500,
                        help="rows per commit (default 500)")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many rows; omit for all of them")
    args = parser.parse_args(argv)

    if args.batch < 1:
        parser.error("--batch must be at least 1")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")

    db = SessionLocal()
    try:
        total, missing = _counts(db)
        print(f"crew_assignments: {total}")
        print(f"without a reference: {missing}")

        if missing == 0:
            print("\nNothing to do.")
            return 0

        if not args.apply:
            planned = missing if args.limit is None else min(missing, args.limit)
            print(f"\nDry run. Would write {planned} row(s), "
                  f"{args.batch} per commit, touching no other column.")
            print("Re-run with --apply to write.")
            return 0

        written = 0
        skipped = 0
        while True:
            remaining = None if args.limit is None else args.limit - written
            if remaining is not None and remaining <= 0:
                break
            size = args.batch if remaining is None else min(args.batch, remaining)

            rows = db.execute(text(
                "SELECT id FROM crew_assignments "
                "WHERE public_ref IS NULL ORDER BY id LIMIT :limit"
            ), {"limit": size}).fetchall()
            if not rows:
                break

            for (assignment_id,) in rows:
                if _issue(db, assignment_id):
                    written += 1
                else:
                    skipped += 1
            db.commit()
            print(f"  committed — {written} written"
                  + (f", {skipped} already taken" if skipped else ""))

        _, still_missing = _counts(db)
        print(f"\nWrote {written} reference(s). "
              f"{still_missing} assignment(s) still without one.")
        if still_missing and args.limit is not None:
            print("That is the --limit stopping, not a failure. "
                  "Re-run to continue.")
        return 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
