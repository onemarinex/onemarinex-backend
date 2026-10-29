# Read-only company Demo reports

The Demo API uses the same six report types, section configuration, PDF engine and privacy reducers as Live. It does not insert reports, change company branding, send email or read operational records. Normal authentication and a current verified-company membership are required for every endpoint.

The router is `app.api.v1.routes_company_demo_reports.router`. Its application prefix is `/api/v1/shipping-company/report-centre`.

## API

- `GET /demo/options`: shared report catalogue and section labels; four numeric vessel IDs, three numeric port IDs and `countries: ["IN"]`. Returns the available fixture period and fixture version.
- `GET /demo/calls`: filters for `country_code`, `port_id`, `vessel_id`, `start`, `end` and `search`; paging uses `offset` and `limit`. Both period boundaries are required if either is supplied. IDs are numeric transport aliases for the existing fictional source references.
- `POST /demo/preview`: the same report/filter/configuration body as Live. Returns `{token, payload, expires_at}`. It prepares the payload from the server fixture; clients cannot supply report records.
- `POST /demo/render`: accepts `{token}` and returns PDF bytes. The signed token contains report parameters, a frozen generation/fixture anchor, the fixture version, actor and company IDs, and a thirty-minute expiry. It is not an access or refresh token. The current login and membership are checked again when rendering.

All responses use `Cache-Control: no-store`. Keep the report token in transient browser memory. Do not put it in a URL or local/session storage. Preview and download use the same signed snapshot and produce identical bytes.

## Fixture provenance

`company_demo_fixture.py` copies `heyports-frontend/src/components/ShippingCompany/Demo/demoData.ts`, `createDemoData` and `CALL_SETUP`. It preserves all original values for four vessels, three ports, twelve calls, 216 trips, 120 survey responses and 120 service reviews across three months.

The source values were compared with the actual TypeScript generator output, not hand-recreated expected totals. The comparison passed for every original call, trip, survey and review field at the fixed test anchor. Evidence is in `new work/Round-2-Delivery/report-pdf-evidence/demo-fixture-parity.json` in the parent workspace.

The following explicit fictional metadata was added for correct filtering and shared metric semantics:

- Numeric transport IDs for vessels, ports and calls.
- Stable `demo-person-{ordinal}` identities across months. Repeated monthly answers are not treated as different people.
- Generated booking, trip-start, survey-experience and review timestamps within the copied call interval, bounded by the frozen fixture anchor. These are generated fiction, not recovered real timestamps.

The original taxi `minutes` field is retained. Explicit fictional departure and port-return milestones, with stable passenger identities, now drive the same per-journey time-ashore definition as the company UI. These generated milestones do not prove vessel boarding. Facility reviews do not prove facility reach events or completed welfare service orders.

The existing CASE-001 has no vessel or occurrence timestamp. It is excluded from scoped incident counts. A transport delay is not classified as a safety incident by this adapter. No case count, resolution time or severity is invented.

The Demo company name is OneMarineX with a typographic fallback. No official logo has been invented or substituted. An approved PNG can be placed at `app/assets/onemarinex-logo.png`. The authenticated Demo branding endpoint, Company Profile and actual PDF renderer use that one validated asset. The official asset remains pending; no replacement was invented. Demo branding never writes to Live company profiles.

## Shared statistics

Survey summaries call the existing Live `statistics` reducer. Latest eligible answers are selected per synthetic person and per question. The same five-person privacy threshold applies.

Review summaries use `company_review_statistics.summarize_review_rows`. The Live query path in `welfare_analytics.review_summary` uses this same reducer. Its company, port, vessel, country, provider, review-type and exact-call query scopes remain unchanged. Complementary-cell suppression is preserved.

## Checks

`tests/test_company_demo_reports.py` is a non-DB suite. It does not initialize fixtures or truncate tables. Checks cover private options/call paging, normal login requirements, no writes during preview/render, deterministic PDFs, token type separation, actor/company isolation, tampering/expiry, arbitrary-payload rejection, per-person statistics, date filtering, exact-call filters and Live reducer delegation.

The parent task owns `tests/test_company_demo_reports_http.py` for real database-backed authorization and no-write checks. These pure tests do not replace those checks or the connected browser workflow.
