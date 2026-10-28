# Company metric contract — 27 October 2026

Definition version: `2026.10.27.1`. Prepared engineering contract; owner G6 sign-off remains unrecorded.

Operational totals use threshold 0. Sensitive incident/SOS metrics use five distinct registered crew profiles, rather than five repeated events from one person. Missing identity is conservative. Planned transport never confirms vessel movement.

## verified_vessels — Verified vessels

- **section:** overview
- **numerator:** distinct canonical vessel_id across this company's approved, non-revoked claims
- **denominator:** Not a rate
- **unique_key:** vessel_id
- **event_time:** n/a — a state, not an event
- **eligible_status:** VesselRoleClaim.status = APPROVED and revoked_at is null
- **missing_data:** a claim with no canonical vessel is excluded and counted as unresolved
- **availability:** ready
- **caveat:** Distinct by vessel, not by claim. A company holding Owner and ISM on one ship is one vessel, not two -- section 22 names this double count directly.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## qualifying_calls — Qualifying port calls

- **section:** vessels_and_port_calls
- **numerator:** vessel_calls whose window intersects an approved role window
- **denominator:** Not a rate
- **unique_key:** vessel_call_id
- **event_time:** port_call
- **eligible_status:** the call intersects [effective_start, effective_end)
- **missing_data:** a call with no started_at is excluded and reported as unresolved
- **availability:** labelled
- **caveat:** A call may be only partly inside tenure. Listing it does not entitle the company to the events inside it -- section 7 is explicit that each event is attributed by its own timestamp.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## eligible_crew — Crew marked eligible

- **section:** crew_shore_leave
- **numerator:** crew_assignments with shore_pass_eligible true on a qualifying call
- **denominator:** crew_assignments on that call
- **unique_key:** crew_assignment_id
- **event_time:** port_call
- **eligible_status:** CrewAssignment.shore_pass_eligible is true
- **missing_data:** eligibility is a current flag, not a historical snapshot. A figure for a past call reflects the flag now, not then.
- **availability:** labelled
- **caveat:** Section 22 asks 'eligibility at what time?' and there is no answer in the schema: the flag carries no history. Label the figure as current state, never as what was true during the call.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## passes_approved — Agent-confirmed shore-leave eligible (operational approval proxy)

- **section:** crew_shore_leave
- **numerator:** call assignments marked eligible by the agent
- **denominator:** crew marked eligible on that call
- **unique_key:** crew_assignment_id
- **event_time:** port_call
- **eligible_status:** shore_pass_eligible is true
- **missing_data:** Eligibility is current state; there is no historical change snapshot
- **availability:** labelled
- **caveat:** Agent eligibility is the owner's operational approval proxy, not a physical immigration-issued pass. Historical figures reflect current state.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## actually_ashore — Crew who went ashore

- **section:** crew_shore_leave
- **numerator:** shore_passes with out_time recorded on a qualifying call
- **denominator:** passes approved on that call
- **unique_key:** shore_pass_id
- **event_time:** shore_pass_exit
- **eligible_status:** out_time is not null
- **missing_data:** a missing in_time is not a crew member who stayed ashore; it is a return nobody wrote down. Report returns separately as unknown.
- **availability:** labelled
- **caveat:** Written by a person at the gate, so this is a recording of an exit rather than a measurement of one. No manifest inference.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## booking_activity — Cab bookings placed

- **section:** crew_movements
- **numerator:** cab_bookings created inside tenure for a qualifying call
- **denominator:** Not a rate
- **unique_key:** booking_id
- **event_time:** cab_booking_placed
- **eligible_status:** the booking resolves to a canonical vessel and call
- **missing_data:** a booking with no resolved call is excluded, not guessed
- **availability:** ready
- **caveat:** A booking is not a trip, not a crew member ashore, and not a welfare outcome. Section 22 lists all three.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## trips_started — Trips started

- **section:** crew_movements
- **numerator:** cab_bookings with trip_started_at inside tenure
- **denominator:** bookings placed
- **unique_key:** booking_id
- **event_time:** trip_started
- **eligible_status:** trip_started_at is not null
- **missing_data:** null means not started, or started and not recorded; counted as not started and labelled
- **availability:** ready
- **caveat:** A different event time from the booking, so it can fall in a different tenure window. Never attribute trip facts to the booking's timestamp.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## trips_completed — Trips completed

- **section:** crew_movements
- **numerator:** cab_bookings with trip_completed_at inside tenure
- **denominator:** trips started
- **unique_key:** booking_id
- **event_time:** trip_completed
- **eligible_status:** trip_completed_at is not null
- **missing_data:** as above
- **availability:** ready
- **caveat:** A completed trip is not proof that anyone re-boarded.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## sign_on_off — Confirmed sign-on and sign-off events

- **section:** crew_movements
- **numerator:** explicit audited crew movement events
- **denominator:** Not a rate
- **unique_key:** assignment_id + movement type
- **event_time:** crew_sign_on_off
- **eligible_status:** explicit agent confirmation timestamp exists
- **missing_data:** Unconfirmed or planned transport is excluded
- **availability:** labelled
- **caveat:** Agent-confirmed movement events only. Booked or completed transport never confirms boarding.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## sos_raised — SOS raised

- **section:** safety_and_incident_oversight
- **numerator:** crew_sos_requests created inside tenure for a qualifying call
- **denominator:** Not a rate
- **unique_key:** sos_id
- **event_time:** sos_raised
- **eligible_status:** the request resolves to a canonical vessel and call
- **missing_data:** an unresolved request is excluded and reported as unresolved
- **availability:** ready
- **caveat:** Aggregate only. No precise location and no contact detail: section 10 says this section carries no raw incident or SOS.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 5
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## incidents_reported — Incidents

- **section:** safety_and_incident_oversight
- **numerator:** incidents whose occurrence falls inside tenure
- **denominator:** Not a rate
- **unique_key:** incident_id
- **event_time:** incident_occurred, falling back to incident_reported
- **eligible_status:** the incident resolves to a canonical vessel and call
- **missing_data:** a row with no stated occurrence is attributed by its report time and must be labelled; see company_event_dictionary.incident_occurrence
- **availability:** labelled
- **caveat:** `incidents.occurred_at` was added on 26 Oct 2026 and is null on every earlier row. Those are attributed by report time, which across a handover can be the wrong company -- label, do not hide.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 5
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## incidents_resolved — Incidents resolved

- **section:** safety_and_incident_oversight
- **numerator:** incidents with resolved_at inside tenure
- **denominator:** incidents reported
- **unique_key:** incident_id
- **event_time:** incidents.resolved_at
- **eligible_status:** resolved_at is not null
- **missing_data:** null means unresolved or not recorded as resolved
- **availability:** labelled
- **caveat:** Resolution is its own event with its own time. An incident raised under one company and resolved under the next belongs, for this metric, to whoever held the vessel when it was resolved -- so the two counts need not reconcile, and a ratio of them is misleading across a handover.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 5
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## welfare_engagement — Welfare engagement

- **section:** welfare_intelligence
- **numerator:** completed services in agreed welfare categories
- **denominator:** Not a rate
- **unique_key:** booking_id
- **event_time:** trip_completed
- **eligible_status:** the service category is on the agreed welfare list
- **missing_data:** an uncategorised service is excluded, never assumed welfare
- **availability:** unavailable
- **caveat:** The categories are not agreed. Section 22 forbids mental-health scores and any claimed wellbeing outcome inferred from rides: a crew member taking a cab to a restaurant is a cab ride, not evidence of welfare. Needs an agreed category list first.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 5
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## sign_ons — Confirmed sign-ons

- **section:** crew_movements
- **numerator:** assignments explicitly confirmed signed on
- **denominator:** Not a rate
- **unique_key:** crew_assignment_id
- **event_time:** crew_signed_on
- **eligible_status:** signed_on_at is recorded
- **missing_data:** No confirmation is excluded
- **availability:** labelled
- **caveat:** Agent-confirmed boarding, not transport completion; counted at the confirmation timestamp.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## sign_offs — Confirmed sign-offs

- **section:** crew_movements
- **numerator:** assignments explicitly confirmed signed off
- **denominator:** Not a rate
- **unique_key:** crew_assignment_id
- **event_time:** crew_signed_off
- **eligible_status:** signed_off_at is recorded
- **missing_data:** No confirmation is excluded
- **availability:** labelled
- **caveat:** Agent-confirmed departure, not transport completion; counted at the confirmation timestamp.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## booked_seats — Booked passenger seats

- **section:** crew_movements
- **numerator:** sum of booking num_passengers
- **denominator:** Not a rate
- **unique_key:** booking_id
- **event_time:** cab_booking_placed
- **eligible_status:** resolved booking inside tenure
- **missing_data:** Unknown origin excluded
- **availability:** ready
- **caveat:** Selected seats, not unique crew. Each repeat booking contributes again.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## returned_seats — Returned-to-vessel passenger seats (operational proxy)

- **section:** crew_movements
- **numerator:** selected seats on completed trips
- **denominator:** Not a rate
- **unique_key:** booking_id
- **event_time:** trip_completed
- **eligible_status:** completed trip inside tenure
- **missing_data:** No recorded completion excluded
- **availability:** labelled
- **caveat:** Completed-trip proxy requested by the owner; not independently verified physical boarding.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## estimated_ashore_seats — Current ashore passenger seats (operational estimate)

- **section:** crew_shore_leave
- **numerator:** selected booking seats with pickup reached and no port-end reached
- **denominator:** Not a rate
- **unique_key:** booking_id
- **event_time:** pickup_reached
- **eligible_status:** pickup reached within a continuous approved interval covering the snapshot
- **missing_data:** Missing or conflicting stop tracking is reported separately
- **availability:** labelled
- **caveat:** Seat estimate, not unique crew or physical boarding. Counts pickup until port-end reach. Completion is a separate return proxy; conflicting tracking is reported alongside.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## ashore_uncertain_seats — Seats with incomplete or conflicting tracking

- **section:** crew_shore_leave
- **numerator:** seats on started/completed journeys without usable stop evidence, plus completion/return-stop disagreements
- **denominator:** Not a rate
- **unique_key:** booking_id
- **event_time:** pickup_reached
- **eligible_status:** authorized booking or pickup inside the current continuous interval
- **missing_data:** An unrecorded pickup is unknown, never inferred from driver arrival
- **availability:** labelled
- **caveat:** Coverage indicator; may overlap the estimate when a completed trip has pickup evidence but no port-end reach. No missing tracking is presented as a confirmed exit or return.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

## pending_transfers — Planned transfers (not confirmed movement)

- **section:** crew_movements
- **numerator:** open agency transfer requests
- **denominator:** Not a rate
- **unique_key:** agency_transfer_id
- **event_time:** transfer_planned
- **eligible_status:** not completed, cancelled or declined; planned travel inside tenure
- **missing_data:** Requests with conflicting call agency are excluded
- **availability:** labelled
- **caveat:** Planned transport only; neither a request nor completed transport confirms vessel sign-on/off. Attribution uses scheduled travel time.
- **aggregation_grain:** per vessel, per day
- **suppression_threshold:** 0
- **timezone:** Asia/Kolkata
- **source_version:** 2026.10.27.1

