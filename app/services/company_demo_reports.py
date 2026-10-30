"""Isolated fictional report adapter. No application records or writes."""
from datetime import datetime, timezone
from types import SimpleNamespace
from app.services.company_demo_fixture import FIXTURE_VERSION, PORTS, VESSELS, SETUP, create_demo_data, month_start
from app.services.company_report_templates import report_catalogue, report_options, REPORT_TYPES, SECTIONS
from app.services.company_metrics import DICTIONARY_VERSION
from app.services.company_demo_branding import branding
from app.api.v1.routes_company_intelligence import PORTAL_METRIC_VERSION


def aware(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def validate_scope(params):
    start, end = aware(params["start"]), aware(params["end"])
    if start.tzinfo is None or end.tzinfo is None or not start < end or (end - start).total_seconds() > 1096 * 86400:
        raise ValueError("Choose a valid period of at most three years, with time zones.")
    if params.get("vessel_id") is not None and params["vessel_id"] not in {v["id"] for v in VESSELS}:
        raise ValueError("Choose a demonstration vessel.")
    if params.get("port_id") is not None and params["port_id"] not in {p["id"] for p in PORTS}:
        raise ValueError("Choose a demonstration port.")
    if params.get("country_code") not in (None, "", "IN"):
        raise ValueError("Choose a demonstration country.")
    return start, end


def selected_calls(data, params, anchor):
    start, end = validate_scope(params)
    rows = []
    for call in data["calls"]:
        if aware(call["started_at"]) >= min(end, anchor) or aware(call["ended_at"] or anchor) <= start:
            continue
        if params.get("port_id") is not None and call["port_id"] != params["port_id"]:
            continue
        if params.get("vessel_id") is not None and call["vessel_id"] != params["vessel_id"]:
            continue
        if params.get("call_id") is not None and call["id"] != params["call_id"]:
            continue
        rows.append(call)
    return rows


def options(anchor):
    return {
        "types": report_catalogue(), "sections": [{"code": k, "label": v} for k, v in SECTIONS.items()],
        "scope": {"ports": PORTS, "vessels": [{"id": v["id"], "name": v["name"], "imo_number": None} for v in VESSELS], "countries": ["IN"]},
        "period": {"start": month_start(anchor, -2).isoformat(), "end": month_start(anchor, 1).isoformat()},
        "fixture_version": FIXTURE_VERSION,
    }


def call_page(anchor, params, search="", offset=0, limit=25):
    calls = selected_calls(create_demo_data(anchor), params, anchor)
    term = search.strip().casefold()
    if term:
        calls = [c for c in calls if term in (c["vessel_name"] + " " + c["port_name"] + " " + c["reference"]).casefold()]
    calls.sort(key=lambda c: (c["started_at"], c["id"]), reverse=True)
    return {"items": calls[offset:offset+limit], "total": len(calls), "offset": offset, "limit": limit}


def prepare(params, anchor):
    """Prepare a fictional snapshot. Never accept caller-provided records."""
    start, end = validate_scope(params)
    kind = params["report_type"]
    configuration = report_options(kind, params.get("configuration"))
    if kind == "CALL" and params.get("call_id") is None:
        raise ValueError("Select the exact demonstration port call.")
    if kind != "CALL" and params.get("call_id") is not None:
        raise ValueError("An exact call is available only for Vessel & Port Call reports.")
    data = create_demo_data(anchor)
    calls = selected_calls(data, params, anchor)
    if params.get("call_id") is not None and not calls:
        raise ValueError("Choose a demonstration call inside the selected scope.")
    ids = {call["id"] for call in calls}
    booked = [t for t in data["trips"] if t["call_id"] in ids and start <= aware(t["created_at"]) < end]
    started = [t for t in data["trips"] if t["call_id"] in ids and t["trip_started_at"] and start <= aware(t["trip_started_at"]) < end]
    surveys = [s for s in data["surveys"] if s["call_id"] in ids and start <= aware(s["experience_at"]) < end]
    reviews = [r for r in data["reviews"] if r["call_id"] in ids and start <= aware(r["created_at"]) < end]
    participation = sum(t["seats"] for t in started)
    activity_calls = len({t["call_id"] for t in started})
    call_rows = []
    for call in calls:
        c = dict(call)
        c["metrics"] = {
            "total_booked_rides": sum(t["call_id"] == call["id"] for t in booked),
            "crew_participation": sum(t["seats"] for t in started if t["call_id"] == call["id"]),
        }
        call_rows.append(c)
    comparison = {}
    for call in call_rows:
        vessel = kind == "FLEET"
        key = call["vessel_id"] if vessel else call["port_id"]
        row = comparison.setdefault(key, {"name": call["vessel_name"] if vessel else call["port_name"], "recorded_calls": 0, "booked_rides": 0, "started_passenger_seats": 0})
        row["recorded_calls"] += 1
        for field, metric in (("booked_rides", "total_booked_rides"), ("started_passenger_seats", "crew_participation")):
            value = call["metrics"][metric]
            row[field] = None if row[field] is None or value is None else row[field] + value
    ports = []
    for port in PORTS:
        own = [c for c in call_rows if c["port_id"] == port["id"]]
        if own:
            own_ids = {c["id"] for c in own}
            survey = survey_statistics([s for s in surveys if s["call_id"] in own_ids])
            unable = (survey.get("unable_to_go_ashore") or {}).get("percent")
            finished = [t for t in started if t["call_id"] in own_ids and t.get("ashore_returned_at") and start <= aware(t["ashore_returned_at"]) < end]
            minutes = [(aware(t["ashore_returned_at"])-aware(t["ashore_departed_at"])).total_seconds()/60 for t in finished]
            ports.append({**port, "recorded_calls": len(own),
                "booked_rides": sum(c["metrics"]["total_booked_rides"] for c in own),
                "started_passenger_seats": sum(c["metrics"]["crew_participation"] for c in own),
                "access_percent": None if unable is None else 100-unable,
                "satisfaction_percent": (survey.get("satisfaction") or {}).get("percent"),
                "obstacle_percent": (survey.get("obstacles") or {}).get("percent"),
                "respondents": survey["respondents"], "facilities_visited": None,
                "average_time_ashore_minutes": sum(minutes)/len(minutes) if minutes else None,
                "calls_with_shore_leave": len({t["call_id"] for t in started if t["call_id"] in own_ids}),
                "coverage": "Fictional recorded activity"})
    result = {
        "report_type": kind, "title": REPORT_TYPES[kind], "configuration": configuration,
        "company_name": "OneMarineX", "branding": branding(),
        "data_mode": "DEMO", "generated_at": anchor.isoformat(),
        "period": {"start": start.isoformat(), "end": end.isoformat()}, "filters": params,
        "fixture_version": FIXTURE_VERSION, "metric_version": PORTAL_METRIC_VERSION,
        "source_dictionary_version": DICTIONARY_VERSION,
        "calls": call_rows,
        "summary": {"recorded_vessels": len({c["vessel_id"] for c in calls}), "recorded_ports": len(ports), "recorded_calls": len(calls), "booked_rides": len(booked), "started_passenger_seats": participation},
        "operations": {"recorded_calls": len(calls), "crew_participation": participation, "calls_with_shore_leave": activity_calls, "calls_with_shore_leave_share": activity_calls * 100 / len(calls) if activity_calls is not None and calls else None, "average_time_ashore_minutes": None, "duration_sample_people": None, "facilities_visited": None, "welfare_service_bookings": None, "duration_coverage": "Time ashore is unavailable. The fixture has taxi journey durations, not authoritative departure-from-vessel and boarding times.", "service_coverage": "The fixture has reviews of facilities, but no authoritative facility reach or welfare service-order records."},
        "comparison": {"dimension": "vessel" if kind == "FLEET" else "port", "rows": sorted(comparison.values(), key=lambda r: r["name"])},
        "ports": ports,
        "port_comparison": ports,
        "vessels": [{"name": v["name"], "imo_number": None, "role_label": ", ".join(v["roles"]), "period_label": "Fictional relationship; exact dates not provided"} for v in VESSELS if any(c["vessel_id"] == v["id"] for c in calls)],
        "safety": {"incident_count": None, "unresolved_count": None, "resolved_count": None, "average_resolution_hours": None, "cases": [], "coverage": "The existing fictional CASE-001 has no vessel or occurrence timestamp. It is excluded from period and vessel counts; a transport delay is not assumed to be a safety incident."},
        "coverage": {"recorded_calls": len(calls), "global_coverage_claim": False, "temporal_granularity": "explicitly generated fictional event timestamps"},
        "methodology": "Fictional, read-only records copied from the company demonstration fixture. No live records are included. Stable synthetic person identities and event timestamps were explicitly added for filtering and shared statistics; these are generated fiction, not recovered real data. Passenger seats can include repeated passengers. Taxi journey minutes do not establish time ashore.",
        "executive_summary": f"This fictional scope contains {len(calls)} recorded calls, {len({c['vessel_id'] for c in calls})} vessels and {len(ports)} ports. No actual operational decisions should use these figures.",
    }
    completed = [trip for trip in started if trip.get("ashore_returned_at") and start <= aware(trip["ashore_returned_at"]) < end]
    durations = [(aware(trip["ashore_returned_at"])-aware(trip["ashore_departed_at"])).total_seconds()/60 for trip in completed]
    average = sum(durations)/len(durations) if durations else None
    result["operations"].update(distinct_crew=len({person for trip in started for person in trip["person_keys"]}), completed_journeys=len(completed),
        average_time_ashore_minutes=average, average_journey_time_ashore_minutes=average,
        duration_sample_people=len({person for trip in completed for person in trip["person_keys"]}),
        duration_coverage="Mean per completed illustrative departure-to-port-return journey. Explicit generated milestones are separate from taxi completion and do not confirm vessel boarding.")
    result["survey"] = survey_statistics(surveys)
    from app.services.company_review_statistics import summarize_review_rows
    result["service_reviews"] = summarize_review_rows([
        SimpleNamespace(review_type=r["type"], crew_id=r["person_key"], rating=r["rating"])
        for r in reviews
    ], suppress=True)
    months = [m for m in data["months"] if datetime.fromisoformat(m+"-01T00:00:00+00:00") < end and
              month_start(datetime.fromisoformat(m+"-01T00:00:00+00:00"), 1) > start]
    result["charts"] = [
        {"section": "executive_summary", "kind": "line", "title": "Recorded call activity", "description": "Fictional calls intersecting the selected scope, grouped by arrival month.", "items": [{"label": m, "value": sum(c["month"] == m for c in calls)} for m in months]},
        {"section": "shore_leave", "kind": "line", "title": "Crew booking trend", "description": "Fictional passenger-seat activity. Repeat passengers can count again.", "items": [{"label": m, "value": sum(t["seats"] for t in started if t["month"] == m)} for m in months]},
        {"section": "crew_experience", "kind": "line", "title": "Satisfaction trend", "unit": "%", "description": "Latest answer per synthetic person within each month; ratings of 4 or 5 out of 5. Shared minimum-respondent and complementary-cell checks apply. These public fictional records do not establish Live disclosure eligibility.", "items": [survey_trend_item(m, surveys) for m in months]},
    ]
    result["approved_feedback"] = [{"context": "Fictional review - " + r["provider"], "text": r["comment"]} for r in reviews[:20]]
    return result


def survey_statistics(rows):
    """Adapt only the server-owned fictional survey fixture to shared statistics.

    Exact fictional context is retained, including the stable source ID/person,
    experience time and recorded vessel/port. These already-public demonstration
    rows have no hidden private source population. Declare their publication set
    explicitly instead of pretending they passed Live tenant/history screening.
    The shared reducer still enforces distinct per-question minima and binary/
    categorical complements. No Live records, SQL session or writes are used.
    """
    from app.api.v1.routes_welfare import statistics
    from app.services.welfare_disclosure import QUESTIONS
    codes = {"Port access restriction": "gate_restriction", "Transport unavailable": "transport_unavailable", "Agent delay": "agent_pass_delayed", "Port processing delay": "immigration_delay", "Transport cost": "transport_expensive"}
    ports_by_vessel = {vessel["id"]: setup["port_id"] for vessel, setup in zip(VESSELS, SETUP)}
    records = [SimpleNamespace(
        id=row["id"], user_id=row["person_key"], updated_at=aware(row["experience_at"]),
        experience_at=aware(row["experience_at"]), vessel_id=row["vessel_id"],
        port_id=ports_by_vessel[row["vessel_id"]], vessel_call_id=row["call_id"],
        message=row["comment"],
        answers={"went_ashore": "YES" if row["went_ashore"] else "NO", "difficulty": "PREVENTED" if not row["went_ashore"] else "DELAYED" if row["barriers"] else "NONE", "barriers": [codes[b] for b in row["barriers"]], "extra_charge": {"Paid": "PAID", "Requested, not paid": "REQUESTED_NOT_PAID", "Not requested": "NOT_REQUESTED"}[row["extra_charge"]], "satisfaction": row["satisfaction"]},
    ) for row in rows]
    public_ids = {record.id for record in records}
    publication = {"population": public_ids}
    publication.update({"question:"+key: public_ids for key in QUESTIONS})
    publication.update({"rate:"+key: public_ids for key in ("difficulty", "extra_charge", "went_ashore", "satisfaction")})
    return statistics(records, suppress=True, publication_ids=publication)


def survey_trend_item(month, rows):
    stat = survey_statistics([r for r in rows if r["month"] == month])
    share = stat.get("satisfaction") or {}
    return {"label": month, "value": share.get("percent"), "suppressed": stat.get("suppressed") or share.get("suppressed", False), "sample_size": share.get("denominator")}
