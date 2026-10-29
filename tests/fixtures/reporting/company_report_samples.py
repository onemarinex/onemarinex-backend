"""Fictional, standalone renderer fixtures. No real account or database access."""
from datetime import datetime, timedelta, timezone


def payload(kind="EXECUTIVE", vessels=20, ports=2, annual=False):
    start = datetime(2026, 1 if annual else 10, 1, tzinfo=timezone.utc)
    end = datetime(2027, 1, 1, tzinfo=timezone.utc) if annual else datetime(2026, 11, 1, tzinfo=timezone.utc)
    days = (end - start).days
    calls = []
    for i in range(vessels * 2):
        calls.append({
            "id": i + 1, "vessel_id": i % vessels + 1, "port_id": i % ports + 1,
            "vessel_name": f"MV Aster {i % vessels + 1:04d}", "port_name": f"Harbour {i % ports + 1:03d}",
            "started_at": (start + timedelta(days=i % days)).isoformat(),
            "eta": (start + timedelta(days=i % days)).isoformat(),
            "status": "ACTIVE" if i % 5 == 0 else "CLOSED",
            "metrics": {"total_booked_rides": i % 9 + 1, "crew_participation": (i % 9 + 1) * 3},
        })
    port_rows = [{"name": f"Harbour {i + 1:03d}", "recorded_calls": sum(c["port_id"] == i + 1 for c in calls), "booked_rides": sum(c["metrics"]["total_booked_rides"] for c in calls if c["port_id"] == i + 1), "started_passenger_seats": sum(c["metrics"]["crew_participation"] for c in calls if c["port_id"] == i + 1)} for i in range(ports)]
    vessel_rows = [{"name": f"MV Aster {i + 1:04d}", "recorded_calls": 2, "booked_rides": sum(c["metrics"]["total_booked_rides"] for c in calls if c["vessel_id"] == i + 1), "started_passenger_seats": sum(c["metrics"]["crew_participation"] for c in calls if c["vessel_id"] == i + 1)} for i in range(vessels)]
    bucket = lambda call: datetime.fromisoformat(call["started_at"]).month - 1 if annual else min((datetime.fromisoformat(call["started_at"]).day - 1) // 7, 3)
    labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] if annual else ["Week 1", "Week 2", "Week 3", "Week 4+"]
    call_trend = [{"label": label, "value": sum(bucket(c) == i for c in calls)} for i, label in enumerate(labels)]
    crew_trend = [{"label": label, "value": sum(c["metrics"]["crew_participation"] for c in calls if bucket(c) == i)} for i, label in enumerate(labels)]
    share = lambda n: {"numerator": n, "denominator": 100, "percent": n, "suppressed": False}
    return {
        "report_type": kind, "company_name": "Aster Maritime - Fictional Company",
        "branding": {"name": "Aster Maritime", "logo": None}, "data_mode": "DEMO",
        "period": {"start": start.isoformat(), "end": end.isoformat()}, "generated_at": "2026-10-29T09:30:00+00:00",
        "summary": {"recorded_vessels": vessels, "recorded_ports": min(ports, len(calls)), "recorded_calls": len(calls), "booked_rides": sum(c["metrics"]["total_booked_rides"] for c in calls), "started_passenger_seats": sum(c["metrics"]["crew_participation"] for c in calls)},
        "operations": {"crew_participation": sum(c["metrics"]["crew_participation"] for c in calls), "calls_with_shore_leave": len(calls), "calls_with_shore_leave_share": 100, "average_time_ashore_minutes": 186, "duration_sample_people": 100, "facilities_visited": 84, "duration_coverage": "Complete, linked pickup-to-port intervals. Overlaps are merged. Unfinished journeys are excluded. Fictional sample: 100 people.", "service_coverage": "Facility reach events are recorded. Service-category orders are unavailable."},
        "survey": {"suppressed": False, "respondents": 100, "satisfaction": share(78), "obstacles": share(26), "unable_to_go_ashore": share(11), "barriers": [{"label": "Transport delayed", "respondents": 18}, {"label": "Port gate restriction", "respondents": 12}, {"label": "Work duties", "respondents": 9}, {"label": "Facilities unavailable", "respondents": 6}], "coverage": "Fictional sample: 100 respondents. Missing answers are excluded per question. Barrier choices may overlap. Allegations are unverified."},
        "service_reviews": {"groups": [{"type": "driver", "suppressed": False, "average_rating": 4.24, "reviews": 100, "respondents": 100, "distribution": [{"rating": n, "count": count} for n, count in enumerate([5, 5, 10, 21, 59], 1)]}, {"type": "facility_stop", "suppressed": False, "average_rating": 4.01, "reviews": 80, "respondents": 80, "distribution": [{"rating": n, "count": count} for n, count in enumerate([5, 5, 10, 24, 36], 1)]}], "coverage": "Driver and facility ratings describe service experiences. They are separate from the overall shore-leave satisfaction survey."},
        "comparison": {"dimension": "port" if kind == "PORT" else "vessel", "rows": port_rows if kind == "PORT" else vessel_rows},
        "port_comparison": port_rows,
        "vessels": [{"name": f"MV Aster {i+1:04d}", "imo_number": None, "role_label": "Registered Owner", "period_label": "01 Jan 2026 - Ongoing"} for i in range(vessels)],
        "ports": [{"name": r["name"], "country_name": "Fictional jurisdiction", "recorded_calls": r["recorded_calls"]} for r in port_rows],
        "calls": calls,
        "safety": {"incident_count": 3, "unresolved_count": 1, "resolved_count": 2, "average_resolution_hours": None, "coverage": "Only the company-authorized fictional case summaries are shown. Crew identities and case narratives are excluded.", "cases": [{"reference": "CASE-001", "vessel_name": "MV Aster 0001", "port_name": "Harbour 001", "severity": "High", "status": "Under review"}, {"reference": "CASE-002", "vessel_name": "MV Aster 0002", "port_name": "Harbour 002", "severity": "Routine", "status": "Resolved"}, {"reference": "CASE-003", "vessel_name": "MV Aster 0003", "port_name": "Harbour 001", "severity": "Routine", "status": "Resolved"}]},
        "charts": [
            {"section": "executive_summary", "kind": "line", "title": "Recorded call activity", "description": "Fictional source-call counts. Activity counts are not a welfare score.", "items": call_trend},
            {"section": "shore_leave", "kind": "line", "title": "Crew booking trend", "description": "Fictional passenger-seat activity. Repeat passengers can count again.", "items": crew_trend},
            {"section": "crew_experience", "kind": "line", "title": "Satisfaction trend", "unit": "%", "description": "Fictional survey samples: 25 eligible answers in each week. Positive means a rating of 4 or 5 / 5.", "items": [{"label": f"Week {i+1}", "value": value} for i, value in enumerate([72, 76, 80, 84])]},
        ],
        "findings": [f"The selected fictional period covers {vessels} vessels and {len(calls)} recorded calls.", "Survey satisfaction is 78% from 100 eligible answers. Reported obstacles are shown separately from service ratings."],
        "methodology": "Fictional QA fixture only. It exercises the report renderer without reading production data. Live reports must use server-authorized aggregate services and current field grants.",
        "metric_version": "fixture.1", "source_dictionary_version": "fixture.1",
    }
