# Downgrade and restore

**Verified:** 25 October 2026, against PostgreSQL 16.14, at source revision
`a15d498` with Alembic graph head `t1u2v3w4x5y6`.

## The short version

**This repository has no schema rollback. Restoring from a backup is the only
way to undo a schema change.**

That is not a criticism of the migrations — most are deliberately additive, and
several refuse to downgrade on purpose so audit records survive an application
rollback. But it means the usual mental model ("deploy went wrong, run
`alembic downgrade`") does not apply here, and acting on that model is worse
than doing nothing, for the reason in section 3.

Three separate facts combine to produce this, each reproducible below.

## 1. The migration chain cannot build a database

The first migration — `0befe5cfff10`, the one with `down_revision = None` —
begins by altering `rfq_quotes`, `rfqs` and `users`. It never creates them. It
was authored against a database that already existed, so the chain starts in
the middle of its own history.

Reproduce:

```bash
createdb scratch
DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/scratch \
  .venv/bin/alembic upgrade head
```

Result: `UndefinedTable: relation "rfq_quotes" does not exist`, raised on the
first revision. Because the whole step is one transaction, it rolls back and
leaves **zero tables and no `alembic_version` row**.

The practical consequence: the schema's source of truth is
`Base.metadata.create_all` over the SQLAlchemy models, not the migration
history. You cannot reconstruct production's schema from the migrations in
order to compare the two, and you cannot rehearse a migration on a database
built the way production was built.

### Building a disposable database

This is the method that works, and the one to use for rehearsals and for the
concurrency tests:

```bash
export DATABASE_URL=postgresql+psycopg2://user:pass@localhost:5432/scratch
.venv/bin/python -c "
from app.db.base import Base
from app.db.session import engine
import app.main          # imports every model module
Base.metadata.create_all(bind=engine)"
.venv/bin/alembic stamp head
```

Do not start the application to create a schema. Startup runs `create_all`,
schema guards, data-cleanup helpers and baseline stamping, so it both writes
data and conceals which of those produced the result.

## 2. Downgrade only reaches five revisions, and changes nothing

From head, `alembic downgrade` runs successfully for exactly five revisions and
then stops at an explicit refusal:

| From | To | Effect |
|---|---|---|
| `t1u2v3w4x5y6` | `s0t1u2v3w4x5` | stamp only |
| `s0t1u2v3w4x5` | `r9s0t1u2v3w4` | stamp only |
| `r9s0t1u2v3w4` | `o6p7q8r9s0t1` | stamp only |
| `o6p7q8r9s0t1` | `n5o6p7q8r9s0` | stamp only |
| `n5o6p7q8r9s0` | `m4n5o6p7q8r9` | stamp only |
| `m4n5o6p7q8r9` | `l3m4n5o6p7q8` | **refused** |

The refusal is deliberate, from
`m4n5o6p7q8r9_booking_crew_assignment.py:71`:

```text
RuntimeError: Release 1 historical context cannot be safely downgraded
```

The transaction rolls back, so the stamp stays at `m4n5o6p7q8r9`. Two other
migrations refuse in their own `downgrade()` — `l3m4n5o6p7q8` and
`j1k2l3m4n5o6` — and both sit below this floor, so they are unreachable
anyway. (Several further migrations raise `RuntimeError` from `upgrade()` or
from helpers rather than from `downgrade()`; those are unrelated to rollback.)

13 of the 57 migrations have a no-op downgrade, and — the point that matters —
**all five reachable ones are among them**. That is why the table above reads
"stamp only" the whole way down.

## 3. Why running a downgrade is worse than doing nothing

Measured: downgrading five revisions from head left the table count at 71,
`report_snapshots` still present, and `crew_profiles.selected_vessel_id` still
present, while `alembic_version` read `m4n5o6p7q8r9`.

The database now misreports itself. It claims a revision that predates columns
and tables it still holds. Every later judgement that trusts the stamp —
a release preflight, a schema check, an operator deciding what is deployed —
is then working from a false premise, and nothing was gained, because no schema
was reverted.

So: **do not run `alembic downgrade` against a deployed database.** It buys
nothing and costs you the one cheap signal you had.

### If someone already has

Recoverable, and verified: replaying those five revisions is idempotent.

```bash
.venv/bin/alembic upgrade head
```

Confirmed on a disposable database — five revisions replayed, stamp back at
`t1u2v3w4x5y6`, 71 tables, and the full test suite (591 tests) passing against
the resulting schema. Verify the stamp afterwards, and do not assume this holds
for migrations added after this document's date without re-testing.

## 4. What to do instead

**To undo a release:** roll the *application* back and leave the schema in
place. Every reachable migration is additive, so an older application runs
against the newer schema. This is what `DEPLOYMENT.md` means by "roll the
application back while leaving this additive schema intact".

**To undo a schema change:** restore the database from a backup. There is no
other mechanism. This means:

- Take a backup immediately before any migration, and **confirm it restores**
  into a separate database before starting. An untested backup is not a
  rollback plan.
- Know the restore duration before you need it, because that is your true
  worst-case recovery time.
- Writes accepted after the backup and before the restore are lost. For
  anything beyond a short window, plan how to reconcile them.

**Unverified:** backup configuration, retention, restore timing and key custody
on the managed deployment were not inspected — no environment access. Treat
every claim above as applying to the migration graph and to local PostgreSQL,
not as evidence that your production backups work.

## 5. Before the next large migration

The passport-encryption and shipping-company work will add tables and change
columns. Given the above:

1. **Keep every migration additive and reversible by leaving data in place.**
   No dropped columns, no destructive rewrites in the same release that
   introduces a new read path.
2. **Dual-write and backfill as separate deployable steps**, so a failure at
   any point leaves a schema that both the old and new application can run on.

   `u2v3w4x5y6z7` is the worked example, and the reason the rule is written
   down. It originally did both: added `crew_assignments.public_ref` *and*
   backfilled every existing row. Nothing was destroyed, but a deploy would
   have rewritten a live table, which the owner's standing instruction forbids.
   It is now schema only, and `scripts/backfill_assignment_public_ref.py` is
   the row-writing half — dry run by default, `--limit` for a first slice,
   resumable, and a no-op on a second run. Stopping it is uneventful precisely
   because the column is optional to the application.
3. **Do not plan on `downgrade`.** Write the restore procedure instead, and
   rehearse it.
4. **Never pin the graph head in a gate.** This was written as "decide who
   owns the release baseline", on the assumption that someone had to rule on
   which head was approved. That was wrong: the approved head is a fact in the
   repository, not a policy. `scripts/preflight_assignment_scoped_operations.py`
   hardcoded `EXPECTED_HEAD = "s0t1u2v3w4x5"` and its own test asserted that
   literal, so the gate rejected a healthy database at the current head while
   the test went on passing. It now reads the head from the Alembic graph, and
   pins only the revision whose structures it checks — which is history, and
   does not move. Pin a revision you are asserting *about*; never pin the head.
5. **Consider giving the chain a genesis.** A single squashed baseline
   migration that creates the current schema would make rehearsal, comparison
   and recovery possible for everything built afterwards. It is a sizeable
   piece of work and is not a prerequisite for shipping additively, but every
   release until it exists inherits the limitations in this document.

## Reproducing all of this

The measurements above come from a disposable database built as in section 1.
No production or staging system was contacted, and the application database was
never connected to.
