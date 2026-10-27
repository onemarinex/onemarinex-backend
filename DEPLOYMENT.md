# Deployment: how schema changes reach production

## The short version

Two mechanisms, deliberately overlapping:

1. **Alembic migrations** — canonical. Applied by `python -m app.db.migrate`.
2. **`ensure_*` guards in `app/main.py`** — a safety net that runs on every boot
   and applies the same DDL with `ADD COLUMN IF NOT EXISTS`.

The guards exist because this app has historically deployed with no pre-deploy
hook, so migrations never ran. Every recent schema change therefore shipped
twice: once as a migration, once as a guard. If you add a migration that alters
an existing table, **add a matching guard**, or the change will not reach
production until someone runs the migration by hand.

New *tables* need no guard: `Base.metadata.create_all()` runs at startup and
creates any table that is missing. It never `ALTER`s one that exists, which is
why columns need the guards.

## Running migrations

```bash
python -m app.db.migrate
```

**Not** `alembic upgrade head`. This project's history cannot be replayed from
base — the earliest revision alters `rfqs` / `rfq_quotes`, tables from a retired
product, and dies with `UndefinedTable` against an empty database. `app.db.migrate`
handles the baseline-then-delta path and takes a Postgres advisory lock so two
instances starting together serialise instead of racing on DDL.

## Release 0: repair the duplicate migration graph

Production was stamped at `i0k1l2m3n4o5`, but two newer migrations reused the
already-deployed revision IDs `a2c3d4e5f6g7` and `b3d4e5f6g7h8`. Release 0
reissues only those newer changes as one linear, additive chain:

```text
i0k1l2m3n4o5
  -> j1k2l3m4n5o6  cab_bookings.vessel_id
  -> k2l3m4n5o6p7  agent_profiles.agency_rules
```

After deploying the Release 0 code, run these commands in the backend console:

```bash
PYTHONPATH=. python scripts/preflight_release_zero.py
python -m app.db.migrate
python -m alembic current
python -m alembic heads
```

Do not run the migration if preflight reports orphaned
`cab_bookings.vessel_id` values. Reconcile those bookings first. A successful
deployment ends with both `current` and `heads` reporting only:

```text
k2l3m4n5o6p7 (head)
```

This release repairs the migration foundation only. Historical vessel-call and
crew-assignment tables, event backfills, archival lifecycle changes, and report
snapshots belong in subsequent releases.

## Release 1: immutable vessel and crew history

Release 1 starts only from the verified Release 0 head and adds a linear chain:

```text
k2l3m4n5o6p7
  -> l3m4n5o6p7q8  vessel calls, crew assignments, event ownership, safe deletes
  -> m4n5o6p7q8r9  booking-to-crew-assignment link
```

The migrations are additive. They preserve SOS, Incident, and booking rows when
a crew profile or vessel is removed, and backfill context only from evidence
stored on the event: linked booking first, then a unique stored vessel. They do
not assign unresolved or ambiguous events through the crew member's current
manifest.

After deploying the Release 1 backend code, run in the backend console:

```bash
python -m alembic current
python -m alembic heads
PYTHONPATH=. python scripts/preflight_release_one.py
python -m app.db.migrate
echo "migration_exit=$?"
python -m alembic current
python -m alembic heads
PYTHONPATH=. python scripts/preflight_release_one.py
```

Before migration, `current` must be `k2l3m4n5o6p7 (head)` and `heads` must show
only `m4n5o6p7q8r9 (head)`. Do not continue if preflight prints `BLOCKED`.
After migration, both `current` and `heads` must show only:

```text
m4n5o6p7q8r9 (head)
```

The post-migration preflight prints resolved and unresolved context counts.
Unresolved historical rows are intentionally retained for manual reconciliation
and are not exposed to an agent by current-crew inference. Never "repair" them
by assigning the crew member's present vessel. Release 1 has no destructive
downgrade; roll the application back while leaving this additive schema intact.

See [docs/MIGRATION-ROLLBACK.md](docs/MIGRATION-ROLLBACK.md) for why that is the
only rollback available: the chain cannot build a database from empty, downgrade
reaches only five revisions and changes no schema, and restoring from a backup
is therefore the sole way to undo a schema change.

## Release 2: immutable report artifacts

Release 2 starts only from the verified Release 1 head and adds one revision:

```text
m4n5o6p7q8r9
  -> n5o6p7q8r9s0  immutable, checksummed report snapshots
```

Every Incident, SOS, and Shore Leave PDF download first writes the exact
server-generated report payload to `report_snapshots`. Later changes to a crew
profile, vessel assignment, timeline, or report calculation cannot rewrite an
existing artifact. Generating the report again creates a new version instead
of overwriting the earlier one. Agent reads remain agency-scoped; superadmins
can retrieve snapshots that require reconciliation.

After deploying the Release 2 backend code, run in the backend console:

```bash
python -m alembic current
python -m alembic heads
PYTHONPATH=. python scripts/preflight_release_two.py
python -m app.db.migrate
echo "migration_exit=$?"
python -m alembic current
python -m alembic heads
PYTHONPATH=. python scripts/preflight_release_two.py
```

Before migration, `current` must be `m4n5o6p7q8r9 (head)` and `heads` must show
only `n5o6p7q8r9s0 (head)`. Do not continue if preflight prints `BLOCKED`.
After migration, both `current` and `heads` must show only:

```text
n5o6p7q8r9s0 (head)
```

This release is additive and does not rewrite existing operational records.
It deliberately has no destructive downgrade, because generated report
artifacts are audit records and must survive an application rollback.

## Release 3: vessel lifecycle and reviewed historical reconciliation

Release 3 starts only from the verified Release 2 head and adds one revision:

```text
n5o6p7q8r9s0
  -> o6p7q8r9s0t1  append-only historical-context reconciliation audit
```

Vessel status is derived using backend server time: Active until 24 hours
before ETD, Departing during the final 24 hours, and Departed at or after ETD.
Archived remains an explicit removal action. Departed and archived vessel calls
remain available in the agent's historical-vessel view.

The superadmin reconciliation queue contains only unresolved or mismatched SOS
and Incident records. A reviewer must select a historical vessel call and cite
evidence. The service never proposes or infers the crew member's current
vessel, and every accepted decision is preserved in an append-only audit row.

After deploying the Release 3 backend code, run in the backend console:

```bash
python -m alembic current
python -m alembic heads
PYTHONPATH=. python scripts/preflight_release_three.py
PYTHONPATH=. python scripts/sync_vessel_lifecycle.py
python -m app.db.migrate
echo "migration_exit=$?"
python -m alembic current
python -m alembic heads
PYTHONPATH=. python scripts/preflight_release_three.py
PYTHONPATH=. python scripts/sync_vessel_lifecycle.py
```

Before migration, `current` must be `n5o6p7q8r9s0 (head)` and `heads` must show
only `o6p7q8r9s0t1 (head)`. The lifecycle command is a dry run unless `--apply`
is supplied. Review its proposed status and call transitions, then apply them:

```bash
PYTHONPATH=. python scripts/sync_vessel_lifecycle.py --apply
PYTHONPATH=. python scripts/preflight_release_three.py
```

After migration, both `current` and `heads` must show only:

```text
o6p7q8r9s0t1 (head)
```

Unresolved records are expected and remain visible only to superadmin for
manual reconciliation. Do not bulk-assign them from current crew identity.
Release 3 is additive; its downgrade deliberately preserves reconciliation
audit rows.

## After Release 3: four revisions with no release section

The numbered sections above stop at `o6p7q8r9s0t1`. Four revisions have landed
since, in this order:

```text
o6p7q8r9s0t1
  -> r9s0t1u2v3w4  assignment-scoped operations and request idempotency
  -> s0t1u2v3w4x5  persist SOS note authorship for safe agent edits
  -> t1u2v3w4x5y6  remember which vessel a crew member selected
  -> u2v3w4x5y6z7  give every crew assignment an opaque public reference
```

So the current graph head is:

```text
u2v3w4x5y6z7 (head)
```

They were not grouped into a release and have no staged rollout of their own.
Deploy them as one step from the verified Release 3 head, or from any later
revision in the list. None of the four contains a `drop_column`, `drop_table` or
`drop_constraint`, and each guards its own work with a schema inspection, so a
partial run can be repeated. Only `u2v3w4x5y6z7` has been rehearsed end to end
(see below); the other three are additive by inspection, not by measurement.

The gate for the first of them covers the whole group, because its checks are
the data invariants the rest rely on:

```bash
PYTHONPATH=. python scripts/preflight_assignment_scoped_operations.py
```

It reads the head from the Alembic graph rather than naming one, so it does not
need editing when the next migration lands; it prints
`assignment_scoped_applied` so you can see the schema assertions actually ran.
Do not continue if it prints `BLOCKED`.

`u2v3w4x5y6z7` is **schema only**: it adds a nullable column and its unique
index, reads no row and writes no row. Deploying it does not rewrite the
database. Its `downgrade()` is deliberately a no-op — dropping the column would
destroy references that may already have been handed to drivers and cab
providers. Roll the application back and leave the schema in place, as with
Releases 1-3.

Filling that column is a separate, deliberate step, and the application does not
need it done: an assignment with a null reference behaves exactly as it did
before, which is why this can wait, run in slices, or stop halfway.

```bash
# what it would do, writing nothing
PYTHONPATH=. python scripts/backfill_assignment_public_ref.py

# a bounded first slice, to watch it on real data
PYTHONPATH=. python scripts/backfill_assignment_public_ref.py --apply --limit 500

# the rest, resuming from wherever it stopped
PYTHONPATH=. python scripts/backfill_assignment_public_ref.py --apply
```

It writes `public_ref` and only where it is NULL, so a row that already has one
is never re-issued. Each batch commits before the next begins, so interrupting
it leaves finished rows written and the rest NULL. Rehearsed against 1,200 rows:
a checksum over every other column was identical before and after.

## Before the first run, check the stamp

```sql
SELECT version_num FROM alembic_version;
```

| Result | What it means | What to do |
|---|---|---|
| table missing / empty | never stamped | **Stamp at the revision matching your current schema first**, then upgrade. Running `app.db.migrate` while unstamped will `create_all()` + stamp head and apply **no deltas**, leaving new columns missing on a database marked up to date. |
| the current head | already up to date | nothing |
| an older revision | deltas pending | upgrade, but expect migrations whose work a guard already did. All revisions from `w1c2d3e4f5g6` onward are idempotent; older ones may not be. |

To stamp without running DDL:

```bash
alembic stamp <revision>
```

## Adding a pre-deploy job (DigitalOcean App Platform)

A **Pre-Deploy Job** runs after build and before the new version takes traffic.
If it fails, the deployment is aborted and the previous version keeps serving —
which is exactly the behaviour you want from a migration step.

Dashboard: **App → Create Component → Job**, Job Type **Pre-Deploy**, same repo
and branch as the web service, run command `python -m app.db.migrate`. It must
receive the **same `DATABASE_URL`** as the web component.

As spec YAML:

```yaml
jobs:
  - name: migrate
    kind: PRE_DEPLOY
    run_command: python -m app.db.migrate
    environment_slug: python
    instance_count: 1
    instance_size_slug: apps-s-1vcpu-0.5gb
    github:
      repo: onemarinex/onemarinex-backend
      branch: main
    envs:
      - key: DATABASE_URL
        scope: RUN_TIME
        value: ${db.DATABASE_URL}
```

> **Do not apply this fragment on its own.** `doctl apps update --spec` replaces
> the entire app spec, so a file containing only this block would delete the web
> service. Export the current spec first, merge this in, then apply:
>
> ```bash
> doctl apps spec get <app-id> > app.yaml
> # merge the jobs: block into app.yaml
> doctl apps update <app-id> --spec app.yaml
> ```

## Migrations must be backward compatible

A pre-deploy job runs while the **old** code is still serving. Until the new
version takes over, the previous release is querying the migrated schema. So a
migration may add nullable columns, add tables, and add enum values; it must not
drop or rename anything, or add a `NOT NULL` column without a default, until the
release *after* the one that stopped using it.

## Note on `procfile`

The file is committed lowercase, so buildpacks looking for `Procfile` ignore it
and the start command comes from the platform dashboard instead. Renaming it
would silently change how the app starts if the dashboard command differs —
check that the dashboard says
`uvicorn app.main:app --host 0.0.0.0 --port $PORT` before renaming.
