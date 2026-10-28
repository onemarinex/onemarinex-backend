# Shipping company portal: v13 / v9 implementation

Date: 28 October 2026.
Scope: the independent work from Cumulative Review Master v13 and Complete UI Functionality Master v9.

This document extends `company-operations.md`. The approved-claim access boundary is unchanged. The automatic-acceptance decision remains open.

## Implemented flows

| Area | Behavior |
|---|---|
| Company onboarding | Super Admin saves and resumes a four-step draft. Completion requires company identity, representative authority and verification evidence. It verifies the company and sends its initial administrator a private, expiring invitation. Mail failure rolls back completion. Completion can be retried without creating another company. |
| Company administration | Team Members, Invitations, Access Activity, and Company Profile & Branding. Existing last-administrator protection remains. Invitation resend rotates the old token. Logos are decoded, resized and re-encoded as PNG, with metadata removed. Legal identity stays read-only for the company. |
| Fleet administration | Search and status filtering use the company-scoped registry. CSV/XLSX uploads have size and row limits and produce row findings. Formula cells are refused. Date corrections, end dates and withdrawal require a separately audited Super Admin decision. Corrections retain the original claim. Import and change requests grant no access. |
| Overview and port calls | Five overview metrics. Authorized active and recorded ports, with search, paging and current port figures. Exact call headers use the selected call's agent, ETA, ETD, status and time zone. Call detail has four operational metrics and a paged activity feed. Transport and confirmed vessel movement are separate record types. |
| Safety escalation | Only cases explicitly escalated by Super Admin to the recipient company appear. Ordinary company safety endpoints and old report exports no longer publish un-escalated incident/SOS counters. |
| Restricted disclosure | Requests start from a call or an escalated case. Tracking is separate from administration. Super Admin selects requested fields and individual notes, timeline items or service reviews, narrows the event window, applies literal redaction terms and previews the company view. Approval rejects a changed preview. |
| Live approved information | Reads and PDF exports recheck membership, source ownership, tenure, the escalation, approved fields and expiry. Edited approved comments/notes require another review. Ordinary system case updates can appear with generic labels; their original personal text does not flow through. |
| Crew welfare | General feedback and an optional structured survey use separate storage. Non-bookers can respond to their own call. Answers can distinguish inability to go ashore, optional non-participation reasons, agent-requested money, actual payment and satisfaction. No new survey submission creates an incident. |
| Welfare intelligence | Five company tabs and four Super Admin tabs. Shared calendar periods, country/port/vessel scope, monthly trends, port comparisons, structured responses, service rating distributions and original Super Admin source reviews. Provider/type filters apply to service reviews. Country metadata requires a reviewed Super Admin entry. |
| Reports | Fleet Performance, Port Performance, Vessel / Port Call and Crew Welfare templates. Preview is live. Generation stores the payload, metric version, branding, source scope and SHA-256. History opens the stored payload. PDF/CSV exports use that payload and recheck the original authorized periods. A shortened or revoked relationship can block an old report. |
| Navigation | Overview, Your Fleet, Vessels & Port Calls, Crew Welfare, Safety Escalations, Reports, Data Access Requests and Company Administration. Old movement/shore-leave URLs remain for compatibility. Company chrome stays fixed; the workspace scrolls. Navigation uses native links. |

## Access contracts

- A declared or imported IMO does not establish a verified relationship. Existing independent approval remains required.
- Company reads require an active membership in a verified company.
- Staff and branding mutations require the company administrator role.
- Company roles retain their existing shared oversight access. A disclosure request does not approve itself.
- Super Admin alone verifies companies, decides claims and fleet corrections, escalates cases and approves disclosures.
- Case and call grants are distinct. A case grant cannot release unrelated call reviews.
- A field approval cannot add a field that was not requested.
- Selected items must belong to the approved source. Review text is tied to the approved content hash.
- The requested event window and access lifetime are separate. Access lasts at most seven days.
- Redaction terms use explicit, case-sensitive literal matching. The preview is the reviewer's check of the released text. This is not automatic personal-data detection.
- Personal fields do not expand automatically. Approved scalar personal values are retained; edited source notes/reviews are hidden until reviewed again.
- Revocation/expiry blocks the next API read or export. The browser also hides expired information and refreshes the live grant view.
- Company, privileged review and survey responses use `Cache-Control: private, no-store`.
- View, export, review and administration events are recorded in the company audit trail.

## Metric contracts and source limits

The new report contract is `2026.10.28.portal.1`. The payload also retains the legacy source dictionary version. Existing legacy endpoint definitions stay in `company-metric-contract-2026-10-27.md`.

1. Company role periods use half-open UTC intervals. Each source event uses its own timestamp. A call overlapping a handover does not grant the entire call's history.
2. Crew ashore and participation use tracked passenger seats. Repeat trips can count a person again. The UI states this unit. Transport completion is not confirmed vessel boarding.
3. Active-ride report snapshots use recorded start/completion milestones. A later completed status does not erase an earlier active-ride snapshot.
4. Survey percentages use distinct respondents who answered that question. Missing answers are excluded. Company survey/review groups below five people are withheld. Rating distributions also avoid releasing a small cell by subtraction.
5. Star ratings remain service ratings. They are not the overall shore-leave satisfaction survey.
6. Completed pickup-to-port intervals can support time-ashore averages. Overlaps are merged. Incomplete/unlinked intervals are excluded. Coverage is shown.
7. No approved link between marine procurement orders and crew welfare services exists. Welfare-service booking totals therefore remain unavailable. Reached facilities are shown separately and are not relabelled as purchases.
8. Existing port records have no country field. Reviewed geography uses a new separate table. Unmapped ports stay in the “Country not recorded” scope. No production country values were guessed or backfilled.
9. ETA/ETD are estimates. They are not relabelled as actual arrival/departure. UTC is an explicit fallback when the port time zone is absent.
10. Unknown canonical IMOs are row errors in the retained claim import flow. They must enter the canonical vessel registry through the existing authorized process before a role claim can be submitted.
11. Legacy feedback stays unchanged in its original storage. Super Admin has a separate, filtered read path. It is not silently included in structured survey percentages.
12. No AI provider or background classification job is enabled. Structured categories and numeric metrics work without AI. Optional AI comment analysis remains an extension.

## Principal route families

All paths below start with `/api/v1/`.

| Audience | Route family |
|---|---|
| Company member | `shipping-company/intelligence/*` |
| Company administrator / member reads | `shipping-company/administration/*` |
| Company member / administrator fleet mutations | `shipping-company/fleet/registry`, `import-preview`, `claims/{id}/changes`, `changes` |
| Super Admin | `superadmin/company-onboarding/*`, `superadmin/fleet-changes/*` |
| Company member | `shipping-company/safety-escalations/*`, `disclosure-requests/*` |
| Super Admin | `superadmin/safety-escalations/*`, `disclosure-requests/*` |
| Crew | `crew/welfare-feedback`, `crew/welfare-feedback/options`, `crew/welfare-feedback/calls` |
| Company member | `shipping-company/welfare-intelligence/*` |
| Super Admin | `superadmin/welfare-intelligence/*` |
| Company member | `shipping-company/report-centre/*` |

Request/source identifiers are authorization inputs, not authority by themselves. The server resolves and checks the exact source on each protected operation.

## Schema and application rollout

The Alembic head is `j6e7f8a9b0c1`. Five new revisions follow `e1f2a3b4c5d6`:

- `f2a3b4c5d6e7`: company profiles and onboarding drafts.
- `g3b4c5d6e7f8`: separate welfare feedback.
- `h4c5d6e7f8a9`: recipient safety escalations, disclosure requests and report snapshots.
- `i5d6e7f8a9b0`: reviewed port geography.
- `j6e7f8a9b0c1`: reviewed fleet change requests.

These revisions create eight additive tables and indexes. They do not update, move or delete historical rows. Repeated upgrades are idempotent. Downgrades retain the tables and data for application rollback.

This repository also creates missing tables from registered metadata at application startup. Do not mistake an Alembic stamp alone for proof of the deployed schema. Follow the environment's existing schema process and check the actual tables and constraints. Deploy the backend before the new frontend. The backend requirements now include ReportLab and an explicit Pillow constraint.

Set the correct `APP_PUBLIC_BASE_URL` and configure SMTP for initial/staff invitations. Without configured mail, onboarding completion and invitation creation/resend return a delivery error. Never use the test fixture/outbox assembly as a deployment entry point.

No production database, staging target or deployment was used for this implementation. Target schema, running revisions, mail delivery and authenticated release acceptance still need operational verification on an identified private target.

## Verification

Use the isolation instructions in `company-operations.md`. Database tests and the browser fixture must run in separate phases because the HTTP fixtures reset synthetic tables.

New tests cover administration, onboarding, event-time call scope, activity paging, structured survey privacy, country/provider filters, granular disclosure, preview/source races, expiry/revocation, frozen reports, shortened tenure and additive migrations. The connected browser fixture now supports all new route families, synthetic survey identities and a synthetic escalated case. Email stays in memory and external networking is blocked.

The fixture is loopback-only: PostgreSQL `55957`, API `18080`, frontend `15173`. No public staging URL is created. Final counts and merge records are recorded in the task completion report after verification.
