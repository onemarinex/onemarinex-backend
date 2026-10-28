> Update, 28 October 2026: [The v13/v9 extension](company-portal-v13-v9.md) describes the revised portal, disclosure, welfare, report and schema contracts. The text below records the earlier operational release.

# Company operations completion contract — 27 October 2026

This release implements the selected gaps in the consolidated completion review. It does not certify a deployment or silently repair old production data.

## Agency transfers

Agents use `/agent/crew-transfer`. Persistent call assignments are the passenger source; planned incoming crew can be added without creating a HeyPorts account. Preparing a passenger does not confirm boarding. Agents explicitly confirm actual sign-on/off independently.

`POST /api/v1/agency-transfers` offers one request to one active provider at the port of the agency-owned open call. It accepts assignment IDs, SIGN_ON/SIGN_OFF, pickup/destination, aware travel time, requirements and an optional client retry key. Reusing a retry key with the same payload returns the original request; changing the payload returns 409. The browser supplies a retry key.

The selected provider uses `/aggregator/agency-transfers`: accept/decline, then assign an available driver from its own fleet. Other providers receive neither the request nor its history. Accepted assignment reserves the driver. Cancellation, replacement and completion release that reservation. Agency reassignment/withdrawal requires a reason, revokes the former driver link and is prohibited once transport starts. The agency audit records actor, state changes and reasons.

Driver links use `/agency-transfer-trip#<token>`. The page removes the fragment and calls a constant API path with `X-Transfer-Token`. Only a SHA-256 digest is stored; links expire 24 hours after assignment and rotate on reassignment. The driver receives only route/time/count/status information. Completing transport never writes vessel sign-on/off. Billing and settlement remain between the agency, shipping company and provider; no marketplace broadcast or HeyPorts charging is introduced.

## Company staff

Verified company administrators manage roles and access at `/shipping-company/settings`. ADMIN manages staff; ANALYST and VIEWER retain the owner-agreed eight oversight sections without staff administration privileges. Disabled membership is checked on every company API request even while a JWT remains valid. A serialized parent-company lock prevents concurrent removal of the last active administrator.

Invitations require configured email, expire after 48 hours, are single-use and revocable, and store only token digests. Mail configuration or delivery failure returns 503 rather than claiming success. Existing invited accounts must authenticate as that email; invitation acceptance cannot reset an existing password. New recipients prove email-token possession before creating their own account. Active membership in another company is rejected. Every membership/invitation decision is audited.

Deployment configuration: use the existing SMTP configuration in `app/services/email.py` and set `APP_PUBLIC_BASE_URL` to the actual private frontend URL. This change does not make a deployment private by itself; existing network/access controls must remain enforced. Fixture tests capture outbound email locally; actual mail delivery requires environment verification.

## Historical attribution and access

`event_origin.py` classifies booking, incident and SOS rows as CONFIRMED_AGENCY, CONFIRMED_UNMANAGED, UNRESOLVED or CONFLICTED. Matching immutable call/vessel/agency evidence is required for company aggregates; a null agency is confirmed unmanaged only with explicit provenance or an independently stored Other-agency call snapshot.

Known immutable agency-stamped operational history remains accessible to that agency when its old call link is missing. Null agency rows never inherit access from the current vessel owner or today's crew roster. Explicit unresolved/conflicted rows are quarantined. Current-call screens require the actual call link; a matching time window alone is insufficient.

Super Admin can review booking/incident/SOS attribution using expected context, independent evidence and a recorded reason. The decision is audited and applied only to that selected record. Late registration proposes separate historical identity reviews for separate voyages; it does not automatically attach departed assignments or treat missing nationality as agreement.

`scripts/audit_context_origins.py` provides read-only aggregate state counts without personal records. It has been exercised only on the disposable fixture. Production counts and individual historical reconciliation decisions remain operational work; no production rows were backfilled, migrated, deleted or moved here.

## Collection pagination

Real HTTP display collections default to 100 rows. Existing array/envelope shapes remain stable; callers navigate with `collection`, `collection_offset` and `collection_limit`. `X-Collection-Pages` exposes per-collection label, offset, limit, returned count, has_more and next_offset. The generic UI provides Previous/Next. Query ordering includes a stable key before SQL LIMIT/OFFSET. Legacy `X-Result-*` headers remain available.

The browser preserves opted-in filters, tabs and expanded fleet/report views only across paging reloads, using a short-lived pathname/account-bound session snapshot. Form submissions and server rows are not replayed. Changing a filter resets the selected page offset. Offset pagination is deterministic for an unchanged dataset; inserts/deletions during browsing can shift pages.

Incident/SOS feeds page one SQL UNION of keys before enrichment. Report record selection is paged; individual report PDFs remain per selected record. Claim conflict and identity audit previews are limited to 20 and explicitly advertise total/truncation; authorized child endpoints provide subsequent pages. Full authorization sets, totals, identity candidate inputs, enrichment lookups and mutation loops remain complete. Current-state catalog inputs need not all be paginated. `collection-pagination-inventory.json` maps the reviewed display-query work; it is not an assertion that every ORM `.all()` must be capped.

## Metrics and acceptance

The versioned contract is `company-metric-contract-2026-10-27.md`. UTC instants are attributed separately to approved half-open tenures; display timezone is Asia/Kolkata. Mid-call handovers show intersecting calls and partial coverage. Claim gaps are never bridged when calculating current ashore estimates.

Engineering acceptance covers dictionary completeness, boundaries, gaps, revocation, suppression units, unknown tracking, report lineage, HTTP authorization and connected cross-dashboard updates. Owner G6 approval of the displayed definitions/denominators is a human acceptance step and has not been self-certified. Welfare remains unavailable pending agreed categories; it is not inferred from taxi activity.

## Reproducing isolated verification

Use a disposable PostgreSQL 16 container bound only to `127.0.0.1:55957`, database `company_ops_fixture`, synthetic user `fixture` and password `fixture-only-pass`. Never point fixture setup at a shared database. The browser fixture and HTTP tests explicitly reject other database names/hosts; the network isolation module also pins the port.

From backend root, set:

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH=tests/fixtures/company_ops:tests:.
export DATABASE_URL=postgresql+psycopg2://fixture:fixture-only-pass@127.0.0.1:55957/company_ops_fixture
export WHATSAPP_ENABLED=false
export SMTP_HOST=''
.venv/bin/python tests/fixtures/company_ops/init_fixture.py
.venv/bin/python -m pytest -q
```

The isolation module disables dotenv loading and denies external network/database targets. The initializer creates synthetic schema and the real exclusion constraint. The HTTP fixture resets all synthetic tables between tests; do not run it concurrently with the browser API fixture.

After pytest ends, set `COMPANY_OPS_BROWSER_FIXTURE=1` and `APP_PUBLIC_BASE_URL=http://127.0.0.1:15173`, then run `.venv/bin/python -m uvicorn company_operations_browser_fixture:app --host 127.0.0.1 --port 18080`. This is a test-only minimal API assembly using real routers/auth/database, with an in-memory mail outbox; it is never mounted by `app.main`.

From frontend root, `COMPANY_OPS_CONNECTED=1 CI=1 VITE_API_BASE_URL=http://127.0.0.1:18080 npx playwright test tests/company-operations-connected.spec.ts --config playwright.company-operations.config.ts --retries=0` runs the connected flow. Without COMPANY_OPS_CONNECTED the guarded test is skipped. Run the normal suite with the same config and no connected flag for mocked UI regressions.

## Deployment and rollback

Deploy the backend before the frontend. The new Alembic head is `e1f2a3b4c5d6`, following `d0e1f2a3b4c5`; its frozen idempotent DDL creates four additive tables and indexes only. It does not update existing rows. Repeated upgrade and application rollback were tested against synthetic fixture data; downgrade deliberately retains operational/audit tables. Application rollback uses the previous application revision while retaining data. Verify the actual migration head, SMTP, private routing, logs and authenticated roles on the authorized target before release acceptance.
