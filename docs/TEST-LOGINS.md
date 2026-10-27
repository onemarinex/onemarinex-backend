# Test logins

Local development accounts for the three main surfaces.

> **These are weak, shared, publicly-readable credentials.** They belong on a
> developer machine and nowhere else. `scripts/seed_test_logins.py` refuses to
> run against a non-local `DATABASE_URL` for that reason. Read
> [Before you trust this on a deployed environment](#before-you-trust-this-on-a-deployed-environment)
> before assuming any of it applies to staging or production.

## Create them

```bash
PYTHONPATH=. ./.venv/bin/python scripts/seed_test_logins.py
```

Idempotent: it creates what is missing and leaves existing accounts alone. Add
`--reset` to also reset the password of an account that already exists, for when
someone has changed one and this page has drifted.

## The accounts

Password for all three: **`Test1234!`**

| Surface | Sign in at | Email | Role |
| --- | --- | --- | --- |
| Crew | `/get-started` | `sl-test-crew@heyports.dev` | `crew` |
| Agent dashboard | `/agent-login` | `testagent@heyports.dev` | `agent` |
| Superadmin dashboard | `/superadmin-login` | `superadmin@heyports.com` | `superadmin` |

The crew account gets a `CrewProfile` and the agent account an `AgentProfile`,
both required before their dashboards will load anything.

### Which endpoint each screen uses

Worth knowing when a login works in one place and not another — they are not the
same call:

| Surface | Endpoint |
| --- | --- |
| Crew | `POST /api/v1/auth/login-unified` — email and password only, role inferred |
| Agent, Superadmin | `POST /api/v1/auth/login` — takes an explicit `role` |

All three were verified against a freshly seeded local database: `200`, with
`role` returned as `crew`, `agent` and `superadmin` respectively.

## Other seeded accounts

`scripts/seed_shore_leave_dev.py` sets up the shore-leave cab booking flow and
creates two aggregator accounts, also with `Test1234!`:

| Email | Role | Notes |
| --- | --- | --- |
| `sl-test-agg@heyports.dev` | `aggregator` | Aggregator provider, driver Available |
| `sl-partner@heyports.dev` | `aggregator` | Partnered driver, Offline unless `ENABLE_FLEXIBLE=1` |

That script also points `sl-test-crew@heyports.dev` at Port of Visakhapatnam,
but it does **not** create that account — run `seed_test_logins.py` first.

## The other superadmin seeder

`app/db/seed_superadmin.py` creates the same `superadmin@heyports.com` account.
It used to hardcode **`admin123`** and check nothing about which database it was
pointed at. It now uses the same `Test1234!` locally, so the two scripts no
longer disagree, and it refuses a non-local `DATABASE_URL`:

```bash
PYTHONPATH=. python -m app.db.seed_superadmin
```

Run it as a module, not by path: `app/db/` contains a `types.py`, which shadows
the standard library's and breaks the import of SQLAlchemy. Running the file
directly has never worked.

On a database reached by setting `ALLOW_REMOTE_SEED=1`, it requires
`SUPERADMIN_PASSWORD` and refuses to use the local default.

If an existing account has a password neither script set, re-run
`seed_test_logins.py --reset` to make `Test1234!` authoritative.

## Before you trust this on a deployed environment

Everything above was verified against a **local** database
(`localhost:5432/onemarinex`). None of it was checked against staging or
production, and nothing here should be seeded into either.

Two things are worth checking directly on any deployed environment, because if
they are true they are live weaknesses rather than documentation gaps:

- **Whether `superadmin@heyports.com` exists there with `admin123`.** The
  seeder can no longer do this — it refuses a non-local database — but it could
  before, and the guard does not undo a run that already happened. The password
  was hardcoded in this repository's history, so treat it as published. Check
  for the account; if it exists, change the password, and check the sign-in logs
  for use of it.
- **Whether any `@heyports.dev` test account exists there.** These are meant to
  be local-only.

```sql
SELECT email, role FROM users
WHERE email IN ('superadmin@heyports.com', 'testagent@heyports.dev',
                'sl-test-crew@heyports.dev', 'sl-test-agg@heyports.dev',
                'sl-partner@heyports.dev');
```

## Accounts this page deliberately omits

The local database also holds `crew0…crew13@heyports.dev`, `sa-billtest@example.com`
and a handful of others. They were created by ad-hoc scripts that are not in this
repository, so their passwords are not recoverable from it — and they are not
documented here rather than guessed at. If you need one, point
`seed_test_logins.py` at it or reset it deliberately.