# Unmanaged vessels — the case the specifications do not mention

A Super Admin can create a vessel whose agency is **"Other"**: no partner agency
runs it. This shipped in September 2026 and is live.

**Documents 1, 2 and 3 contain zero references to it.** That was checked, not
assumed. Every one of them describes a world where a crew member belongs to an
agency and every event has an agency to attribute it to, so anyone building from
those documents alone will write code that is wrong here — and wrong quietly,
because an unmanaged vessel produces no error, just an empty result that looks
like a crew member their agent has not added yet.

This page exists so that stops happening. It records what the owner decided
(Q15, 26 October 2026) and what the code actually does, verified against it.

## What makes a vessel unmanaged

`_unmanaged_vessel_call` in `app/api/v1/routes_crew.py` is the single place that
decides. A vessel is unmanaged when its agency name is not a partnered agency —
either literally `"Other"`, or absent.

The distinction it protects matters more than it looks. A **partnered** vessel
with no assignment is a crew member their agent has not added yet: a genuine
wait, and still refused. An **unmanaged** vessel will never have an assignment,
because there is no agent to create one. Treating the two the same is what left
cab booking locked behind *"your agent has not uploaded the crew list"* on a
ship with no agent to do it.

## What the owner decided

> **Q15 — OWNER DECISION.** Preserve live unmanaged-vessel direct booking, with
> no partner agent, no manifest, no agent eligibility card and no false
> "Awaiting your agent" prompt. Integrate these calls into common vessel-call,
> event-time, security and historical rules, with legitimate nullable
> `agency_id`. A validated self-selected unmanaged participation context may be
> created without pretending it is agency-verified crew identity. Preserve cab
> booking for active calls; do not invent a call for departed vessels. Include
> eligible unmanaged events in company aggregate reporting when independently
> attributable to a verified vessel and tenure.

Note what this is **not**: it is not an exemption. The owner did not say to
fence these vessels off from the new model. They are to be carried *inside* the
common rules, with a null agency being a legitimate value rather than a defect.
Code that treats `agency_id IS NULL` as corrupt data is reading this backwards.

## What the code does today

Verified on 26 October 2026, not recalled.

| Requirement | State | Where |
|---|---|---|
| No partner agent, manifest or eligibility card | Met — an unmanaged vessel has none and never will | `_unmanaged_vessel_call` |
| No false "Awaiting your agent" prompt | Met — the endpoint returns `agency_managed`, and the frontend shows "Ask your shipping agent to add you" only when it is true | `routes_crew.py` → `useCrewAssignments.ts:152` |
| Legitimate nullable `agency_id` | Met — `VesselCall.agency_id` is `nullable=True` with `ON DELETE SET NULL` | `app/db/models/vessel_call.py` |
| Cab booking preserved for active calls | Met | `routes_crew.py` |
| No invented call for a departed vessel | Met — the lookup passes `create=False`, so a departed vessel yields no call rather than a fresh one | `routes_crew.py:223` |
| Unmanaged events in company aggregate reporting | **Not built** | Company aggregate reporting does not exist yet |

The last row is the only part of Q15 still outstanding, and it cannot be built
before the company domain is. When it is built, unmanaged calls belong in the
aggregates whenever the vessel and tenure are independently verifiable — they
are not to be filtered out for lacking an agency.

## If you are changing identity, assignment or attribution code

Three questions worth asking before you write a query:

1. **Does this assume every crew member has an assignment?** On an unmanaged
   vessel there is none, by design and permanently.
2. **Does this treat a null `agency_id` as missing data?** Here it is the
   correct value, not an unreconciled legacy row. The two look identical in the
   column and are distinguished by the vessel, not the call.
3. **Does an empty result tell the user their agent has not acted?** Check
   `agency_managed` before saying so, or you will name someone who does not
   exist.

## Related

- `app/api/v1/routes_crew.py` — `_unmanaged_vessel_call`, and the docstring that
  explains the partnered/unmanaged distinction at the point it is made.
- The audit's R04 — legacy rows with a null `agency_id` are a *different*
  problem with the same shape in the column. Do not fix one by assuming the
  other.
