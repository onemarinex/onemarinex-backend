# Database steps after merging this release

**Short answer: there is no manual migration step.** Alembic does not run on
this deployment and never has — the `ensure_*` guards in `app/main.py` are the
real migration mechanism, and they run on boot. Merging to `main` deploys, the
app starts, the guards apply the DDL, and the release is schema-complete.

What follows is the part that is *not* automatic: one check before you deploy,
three verifications after, and one script that is deliberately never run by a
deploy.

---

## What happens on its own, in this order

`app/main.py:on_startup`:

1. `Base.metadata.create_all()` — creates **missing tables**. This is how the
   four company-domain tables, the audit-event table and the two
   submission/request tables arrive. It never ALTERs an existing table.
2. Twenty-one `ensure_*` guards — the **ALTERs**. Each takes its own
   transaction with a `lock_timeout` of 5s and catches its own exception.
3. `ensure_alembic_baseline()` — stamps head **if the database is unstamped**.
   It applies no DDL. The stamp is a record, not a cause.
4. `verify_critical_schema()` — **raises** if a guarded column is missing.
   DigitalOcean aborts a deployment whose process exits, so the previous
   version keeps serving. This is the one guard that fails loudly.

Seven migrations ship with this release (`u2v3w4x5y6z7` … `b8c9d0e1f2a3`) and
are written to match the guards exactly, so a database that gets a column from
either route ends up identical.

---

## 1. Before you deploy — check three columns

`verify_critical_schema` gained three pre-existing NOT NULL columns this
release. They were exempt and should not have been: if one is absent, every
INSERT into its table is *already* failing.

```sql
SELECT 'aggregator_profiles.provider_type'    AS column, to_regclass('aggregator_profiles') IS NOT NULL AS table_exists,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='aggregator_profiles' AND column_name='provider_type') AS present
UNION ALL SELECT 'ports.canonical_key', true,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='ports' AND column_name='canonical_key')
UNION ALL SELECT 'vendors.commission_percentage', true,
       EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='vendors' AND column_name='commission_percentage');
```

All three `present` → deploy. Any `false` → **this release will refuse to
start** where the previous one started and failed on first write. That is the
intended behaviour, but it will abort the deploy, so know it before rolling
out rather than during.

---

## 2. After you deploy — verify the overlap constraint

This is the one that can fail silently and matters most.

`ensure_company_domain_schema` runs `CREATE EXTENSION IF NOT EXISTS
btree_gist` and then adds an `EXCLUDE USING gist` constraint to
`vessel_role_claims`. **It catches its own exception and logs it**, exactly
like the other guards — so if the application's database user cannot create an
extension on managed Postgres, the constraint is simply absent and the app
starts normally.

That constraint is not a nicety. It is the *only* thing preventing two
companies holding the same role on the same vessel over overlapping dates —
the invariant TEN-06 tests and the one the claim-approval flow relies on the
database to enforce.

```sql
SELECT extname FROM pg_extension WHERE extname = 'btree_gist';
SELECT conname FROM pg_constraint WHERE conname = 'uq_vessel_role_claims_no_overlap';
```

Both must return a row. If `btree_gist` is missing, create it as the admin
user (`doadmin` on DigitalOcean managed Postgres — it is on the allowed
extension list) and restart the app so the guard runs again.

## 3. After you deploy — verify the guarded columns and the stamp

```sql
SELECT table_name, column_name
FROM information_schema.columns
WHERE (table_name, column_name) IN (
  ('crew_assignments','public_ref'), ('crew_assignments','source'),
  ('crew_assignments','signed_on_at'), ('crew_assignments','signed_off_at'),
  ('driver_magic_links','expires_at'), ('driver_magic_links','revoked_at'),
  ('incidents','occurred_at'),
  ('cab_bookings','otp_failed_attempts'), ('cab_bookings','otp_last_attempt_at'),
  ('cab_bookings','otp_locked_until')
) ORDER BY 1, 2;
```

Ten rows expected. `verify_critical_schema` already refuses to start without
most of these, so this is confirmation rather than a gate.

```sql
SELECT * FROM alembic_version;
SELECT is_nullable FROM information_schema.columns
WHERE table_name = 'shore_passes' AND column_name = 'crew_profile_id';  -- expect YES
```

**The `alembic_version` value is still unknown** and has been an open question
since 26 October. It does not block this release — the guards inspect the
schema rather than trusting the stamp — but it decides what a future
PRE_DEPLOY job would do on its first run.

---

## 4. The one step that writes to existing rows

```bash
python scripts/backfill_assignment_public_ref.py            # dry run, writes nothing
python scripts/backfill_assignment_public_ref.py --apply --limit 300 --batch 100
```

`crew_assignments.public_ref` is nullable and every surface treats a null as
"no reference", so **the release is correct without this**. Existing
assignments simply have no opaque reference until it runs.

This is deliberately not part of any deploy. It is the only thing in this
release that writes to rows that already exist, and under the standing
instruction about production data it is your call, not the deployment's.
It is resumable, idempotent, and a rehearsal against 1,200 rows showed a
byte-identical checksum over every other column.

---

## 5. Not database, but same deploy

| Setting | Why |
|---|---|
| `ACCESS_TOKEN_EXPIRE_MINUTES=20160` | Production has 30. The frontend now renews on a 401 so this no longer causes an outage, but the value is still wrong |
| `PRIVATE_UPLOAD_DIR` | New. Private uploads land here instead of the public `/uploads` mount. **On the default local backend this is the app's ephemeral disk, so private files are lost on every redeploy.** Set `STORAGE_BACKEND=spaces` for anything that must survive |
| `MANIFEST_PDF_PROCESSOR` | Optional. `off` stops crew-list PDFs being sent to Anthropic; CSV and Excel keep working |

---

## 6. Known gaps, recorded rather than fixed

**The migration chain cannot build a database from scratch.** Measured on
26 October, not assumed: the ORM declares **78 tables and migrations create
28**. Eighteen tables — `users`, `vessels`, `ports`, `cab_bookings`,
`crew_profiles` among them — are ALTERed by migrations that never create
them. `alembic upgrade head` against an empty database fails on the fourth
revision with `relation "rfq_quotes" does not exist`.

That is not a defect to fix so much as a fact to plan around. The migrations
are incremental patches over a `create_all()` schema; they have never been a
schema definition, because `create_all` has always run first.

**It changes how staging must be built.** Gate G1 asks for "migrations tested
up and down in a disposable environment", and that cannot be done as written —
there is nothing to migrate up from zero. Staging has to be built the way
production is, which is also the more honest rehearsal: building it any other
way would exercise a mechanism the production deploy does not use.

The production path *was* rehearsed from empty on 26 October and works:
`create_all()` then the guards then the baseline stamp produced **79 tables**,
every guarded column, `shore_passes.crew_profile_id` nullable, the
`vessel_role_claims` overlap constraint, and a stamp at head.

Three changes that had a guard and no migration at all —
`crew_assignments.source`, the two movement timestamps and the shore-pass
nullability — now have one (`c9d0e1f2a3b4`), and
`tests/test_guard_migration_parity.py` fails if a future guard ships without
one.

**Rollback is restore-from-backup.** Every migration in this release has a
deliberately empty `downgrade()`: dropping these columns would destroy the one
record of a lockout, an occurrence time or an issued reference.
`docs/MIGRATION-ROLLBACK.md` has the detail.
