"""Shared vector PDF rendering for authorized company report snapshots.

No database or remote assets are read. The caller must enforce company, tenure,
field-grant and snapshot-integrity checks before every preview or download.
"""
from reportlab.platypus import KeepTogether, Spacer
import re

from app.services.company_pdf_components import (
    WIDTH, BarChart, LineChart, KPIGrid, build, heading, number, p, period, stamp, table,
)
from app.services.company_report_templates import REPORT_TYPES, SECTIONS, report_options


def survey_share(survey, key):
    if survey.get("suppressed"):
        return "Withheld"
    item = survey.get(key)
    if not isinstance(item, dict):
        return "Unavailable"
    return "Withheld" if item.get("suppressed") else number(item.get("percent"), "%", 1)


def executive_kpis(payload):
    summary, ops = payload.get("summary") or {}, payload.get("operations") or {}
    survey, safety = payload.get("survey") or {}, payload.get("safety") or {}
    return [
        ("Vessels", number(summary.get("recorded_vessels")), "with recorded calls"),
        ("Ports", number(summary.get("recorded_ports")), "recorded coverage"),
        ("Port calls", number(summary.get("recorded_calls")), "inside role periods"),
        ("Shore-leave calls", number(ops.get("calls_with_shore_leave")), "recorded transport activity"),
        ("Crew bookings", number(ops.get("crew_participation", summary.get("started_passenger_seats"))), "passenger seats; repeats possible"),
        ("Shore-leave call share", number(ops.get("calls_with_shore_leave_share"), "%", 1), "share of recorded calls"),
        ("Average time ashore", number(ops.get("average_time_ashore_minutes"), " min"), "complete linked intervals"),
        ("Booked rides", number(summary.get("booked_rides")), "recorded booking activity"),
        ("Satisfaction", survey_share(survey, "satisfaction"), "survey ratings 4 or 5 / 5"),
        ("Reported obstacles", survey_share(survey, "obstacles"), "survey respondents"),
        ("Safety incidents", number(safety.get("incident_count")), "authorized case coverage"),
        ("Open escalations", number(safety.get("unresolved_count")), "authorized case coverage"),
    ]


def section_charts(payload, section):
    charts = []
    for spec in payload.get("charts", []):
        if not isinstance(spec, dict) or spec.get("section") != section or not isinstance(spec.get("items"), list) or not spec["items"]:
            continue
        cls = LineChart if spec.get("kind") == "line" else BarChart
        charts.append(KeepTogether([cls(spec["items"], spec.get("title", "Recorded comparison"), spec.get("unit", ""), spec.get("description", "")), Spacer(1, 8)]))
    return charts


def comparison(payload, dimension):
    block = payload.get("comparison") or {}
    return (block.get("rows") or []) if block.get("dimension") == dimension else []


def call_rows(payload, limit=None):
    calls = payload.get("calls") or []
    return [[
        call.get("vessel_name") or "Not recorded", call.get("port_name") or "Not recorded",
        stamp(call.get("started_at") or call.get("eta"), True),
        str(call.get("status") or "Not recorded").replace("_", " ").title(),
        number((call.get("metrics") or {}).get("total_booked_rides")),
    ] for call in (calls[:limit] if limit else calls)]


def report_section(payload, section, options):
    """Render only named aggregate fields, never arbitrary payload contents."""
    content = []
    charts, tables, brief = options["charts"], options["tables"], options["detail"] == "summary"
    ops, survey = payload.get("operations") or {}, payload.get("survey") or {}
    if section == "executive_summary":
        content.append(KPIGrid(executive_kpis(payload)))
        if payload.get("findings"):
            content += [p(finding) for finding in payload["findings"][:3]]
        elif payload.get("executive_summary"):
            content.append(p(payload["executive_summary"]))
        else:
            content.append(p("Only authorized records in the selected period are included.", "small"))
        if charts:
            content += section_charts(payload, section)
        content.append(p("Crew bookings count passenger seats on started rides. Repeat passengers can count again. Missing values are unavailable; privacy-protected values are withheld.", "small"))
    elif section == "fleet_operations":
        if charts:
            content += section_charts(payload, section)
        rows = comparison(payload, "vessel")
        if rows and charts and not content:
            content.append(KeepTogether([BarChart(
                [{"label": row.get("name"), "value": row.get("recorded_calls")} for row in rows],
                "Recorded calls by vessel", description="Call volume is recorded activity, not a welfare ranking.",
            )]))
        if tables:
            content += table(["Vessel", "Port calls", "Booked rides", "Crew bookings"], [
                [r.get("name"), number(r.get("recorded_calls")), number(r.get("booked_rides")), number(r.get("started_passenger_seats"))] for r in (rows[:5] if brief else rows)
            ], [WIDTH * .43, WIDTH * .17, WIDTH * .2, WIDTH * .2])
        content.append(p("Crew bookings count passenger seats, not distinct people. Active and historical calls use the recorded call status.", "small"))
    elif section == "shore_leave":
        content.append(KPIGrid([
            ("Crew bookings", number(ops.get("crew_participation")), "passenger seats; repeats possible"),
            ("Shore-leave calls", number(ops.get("calls_with_shore_leave")), "recorded transport activity"),
            ("Average time ashore", number(ops.get("average_time_ashore_minutes"), " min"), "complete linked intervals"),
            ("Facilities reached", number(ops.get("facilities_visited")), "recorded reach events"),
        ]))
        if charts:
            content += section_charts(payload, section)
        content.append(p(ops.get("duration_coverage") or "No complete, authorized time-ashore source is available.", "small"))
        content.append(p(ops.get("service_coverage") or "Welfare service-category bookings are unavailable where no approved category mapping exists.", "small"))
    elif section == "port_comparison":
        rows = comparison(payload, "port") or payload.get("port_comparison") or []
        if charts:
            content += section_charts(payload, section)
            if rows:
                content.append(KeepTogether([BarChart(
                    [{"label": row.get("name"), "value": row.get("recorded_calls")} for row in rows],
                    "Recorded calls by port", description="Volume comparison only. No best or worst port is inferred from limited feedback.",
                )]))
        if tables:
            content += table(["Port", "Port calls", "Booked rides", "Crew bookings"], [
                [r.get("name"), number(r.get("recorded_calls")), number(r.get("booked_rides")), number(r.get("started_passenger_seats"))] for r in (rows[:5] if brief else rows)
            ], [WIDTH * .43, WIDTH * .17, WIDTH * .2, WIDTH * .2])
        if tables and any("respondents" in row for row in rows):
            content += table(["Port", "Access", "Satisfaction", "Obstacles", "Sample"], [
                [row.get("name"), number(row.get("access_percent"), "%"),number(row.get("satisfaction_percent"), "%"),number(row.get("obstacle_percent"), "%"),number(row.get("respondents"))]
                for row in (rows[:5] if brief else rows)
            ], [WIDTH*.32,WIDTH*.17,WIDTH*.19,WIDTH*.17,WIDTH*.15])
            content.append(p("Rates describe applicable survey respondents. Missing or privacy-protected samples do not establish poor welfare. Reported allegations are not verified misconduct.","small"))
    elif section == "crew_experience":
        if charts:
            content += section_charts(payload, section)
        if survey.get("suppressed"):
            content.append(p("Crew survey results are withheld. Too few eligible respondents."))
        else:
            content.append(p(f"Satisfaction: {survey_share(survey, 'satisfaction')}. Ratings of 4 or 5 out of 5 among eligible survey answers."))
            item = survey.get("satisfaction") or {}
            if not item.get("suppressed"):
                content.append(p(f"Answer sample: {number(item.get('denominator'))}; positive answers: {number(item.get('numerator'))}.", "small"))
        content.append(p(survey.get("coverage") or "No eligible crew survey source is available.", "small"))
    elif section == "obstacles":
        if survey.get("suppressed"):
            content.append(p("Reported obstacles are withheld. Too few eligible respondents."))
        else:
            content.append(p(f"Reported difficulty: {survey_share(survey, 'obstacles')}. Unable to go ashore: {survey_share(survey, 'unable_to_go_ashore')}."))
            barriers = survey.get("barriers") or []
            if charts and barriers:
                content.append(KeepTogether([BarChart(
                    [{"label": row.get("label") or row.get("code"), "value": row.get("respondents")} for row in barriers],
                    "Reported barriers", description="Respondents may select more than one barrier. Allegations are unverified.",
                )]))
            if not barriers:
                content.append(p("No publishable category counts. Categories with fewer than five eligible respondents are withheld.", "small"))
            if tables and barriers:
                content += table(["Reported barrier", "Respondents"], [[row.get("label") or row.get("code"), number(row.get("respondents"))] for row in (barriers[:5] if brief else barriers)], [WIDTH * .78, WIDTH * .22])
        if charts:
            content += section_charts(payload, section)
    elif section == "service_ratings":
        groups = (payload.get("service_reviews") or {}).get("groups") or []
        for group in (groups[:2] if brief else groups):
            name = {"driver": "Driver", "facility_stop": "Facility"}.get(group.get("type"), "Service")
            if group.get("suppressed"):
                content.append(p(f"{name} ratings are withheld. Too few eligible reviewers."))
                continue
            content.append(p(f"{name}: {number(group.get('average_rating'), ' / 5', 2)}. Reviews: {number(group.get('reviews'))}. Reviewers: {number(group.get('respondents'))}."))
            if charts and group.get("distribution") and any(row.get("count") is not None for row in group["distribution"]):
                content.append(KeepTogether([BarChart(
                    [{"label": str(row.get("rating")) + " stars", "value": row.get("count"), "suppressed": row.get("count") is None} for row in group["distribution"]],
                    f"{name} rating distribution", description="Small cells are withheld under the review privacy rule.",
                )]))
            elif charts and group.get("distribution"):
                content.append(p(f"{name} rating distribution is withheld. Small sample cells are protected.", "small"))
        if not groups:
            content.append(p("No authorized service ratings are available."))
        content.append(p((payload.get("service_reviews") or {}).get("coverage") or "Service star ratings are separate from overall shore-leave satisfaction.", "small"))
    elif section == "safety":
        safety = payload.get("safety") or {}
        content.append(KPIGrid([
            ("Safety incidents", number(safety.get("incident_count")), "authorized cases only"),
            ("Open escalations", number(safety.get("unresolved_count")), "authorized cases only"),
            ("Resolved cases", number(safety.get("resolved_count")), "recorded status"),
            ("Resolution time", number(safety.get("average_resolution_hours"), " h", 1), "when supported"),
        ]))
        if charts:
            content += section_charts(payload, section)
        if tables:
            cases = safety.get("cases") or []
            content += table(["Case", "Vessel / port", "Priority", "Severity", "Status"], [
                [case.get("reference"), f"{case.get('vessel_name') or 'Not linked'} / {case.get('port_name') or 'Not linked'}", case.get("urgency") or "Not recorded", case.get("severity") or "Not recorded", case.get("status") or "Not recorded"] for case in (cases[:5] if brief else cases)
            ], [WIDTH * .15, WIDTH * .34, WIDTH * .17, WIDTH * .17, WIDTH * .17])
        content.append(p(safety.get("coverage") or "No authorized safety summary source is available. Missing case data does not prove that no incident occurred.", "small"))
    elif section == "vessel_details":
        rows = payload.get("vessels") or []
        if tables:
            content += table(["Vessel", "IMO", "Company role", "Effective period"], [
                [r.get("name"), r.get("imo_number") or "Not recorded", r.get("role_label") or "Not provided", r.get("period_label") or "Not provided"] for r in (rows[:5] if brief else rows)
            ], [WIDTH * .29, WIDTH * .16, WIDTH * .25, WIDTH * .30])
        else:
            content.append(p("Vessel tables are disabled in this report configuration.", "small"))
    elif section == "port_details":
        rows = payload.get("ports") or []
        if tables:
            content += table(["Port", "Country", "Port calls", "Coverage"], [
                [r.get("name"), r.get("country_name") or "Not recorded", number(r.get("recorded_calls")), r.get("coverage") or "Recorded activity"] for r in (rows[:5] if brief else rows)
            ], [WIDTH * .35, WIDTH * .2, WIDTH * .15, WIDTH * .3])
        else:
            content.append(p("Port tables are disabled in this report configuration.", "small"))
    elif section == "detailed_tables":
        if tables:
            content += table(["Vessel", "Port", "Call date (UTC)", "Status", "Booked rides"], call_rows(payload, 5 if brief else None), [WIDTH * .25, WIDTH * .24, WIDTH * .2, WIDTH * .16, WIDTH * .15], compact=True)
        else:
            content.append(p("Detailed tables are disabled in this report configuration.", "small"))
    elif section == "written_feedback":
        # Explicit approved field only. Never fall back to raw survey messages.
        comments = payload.get("approved_feedback") or []
        for item in (comments[:5] if brief else comments):
            content.append(p(item.get("context") or "Approved feedback", "small"))
            content.append(p(item.get("text") or "No approved text."))
        if not comments:
            content.append(p("No written feedback is approved for this report."))
    elif section == "appendix":
        content.append(p(payload.get("methodology") or "Only authorized source records are included. Missing source data is unavailable."))
        content.append(p("The reporting interval is half-open: its end instant is excluded. Displayed report dates use UTC. Event attribution uses the event's own timestamp and the applicable company-role interval.", "small"))
        content.append(p(f"Metric version: {payload.get('metric_version') or 'Not provided'}. Source dictionary: {payload.get('source_dictionary_version') or 'Not provided'}.", "small"))
    return content or [p("No authorized data is available for this section.", "small")]


def render_report(payload, options=None):
    """Use these exact bytes for preview and download after server scope checks.

    Optional snapshot keys: configuration, charts, findings, vessels, ports,
    safety and approved_feedback. No renderer key authorizes access by itself.
    """
    kind = payload.get("report_type", "FLEET")
    config = report_options(kind, options if options is not None else payload.get("configuration"))
    title = REPORT_TYPES[kind]
    branding = payload.get("branding") or {}
    company = branding.get("name") or payload.get("company_name")
    if not isinstance(company, str) or not company.strip():
        raise ValueError("The report requires the authorized company name.")
    story = []
    if payload.get("data_mode") == "DEMO":
        story.append(p("FICTIONAL DATA - Read-only demonstration. Do not use for operational decisions.", "small"))
    for section in config["sections"]:
        content = report_section(payload, section, config)
        title_row = heading(SECTIONS[section], content[0] if content else None)
        if section == "appendix" or (section == "detailed_tables" and len(payload.get("calls") or []) <= 20):
            story.append(KeepTogether([title_row, *content]))
        else:
            story.append(title_row)
            story += content
    return build(title, company, story, period(payload), stamp(payload.get("generated_at")), branding.get("logo"), fictional=payload.get("data_mode") == "DEMO")


def render(title, company, rows, metadata, logo=None):
    """Compatible protected-disclosure renderer. Long approved notes can flow
    across pages without an unbreakable oversized table cell.
    """
    story = [p(metadata, "subtitle")]
    for label, value in rows:
        story += [heading(label), p(value)]
    generated = re.search(r"Generated\s+(\d{4}-\d{2}-\d{2}T[\d:.Z+\-]+)", str(metadata))
    return build(title, company, story, "Approved information", stamp(generated.group(1)) if generated else "Not recorded", logo, period_has_end=False)
