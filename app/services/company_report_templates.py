"""Shared presentation choices, never permissions or new metric definitions."""

REPORT_TYPES = {
    "EXECUTIVE": "Executive Fleet Intelligence",
    "FLEET": "Fleet Operations",
    "PORT": "Port Performance & Welfare Intelligence",
    "CALL": "Vessel & Port Call",
    "WELFARE": "Crew Welfare & Shore Leave",
    "SAFETY": "Safety & Incident",
}
SECTIONS = {
    "executive_summary": "Executive Summary", "fleet_operations": "Fleet Operations",
    "shore_leave": "Shore Leave", "port_comparison": "Port Comparison",
    "crew_experience": "Crew Experience", "obstacles": "Welfare Obstacles",
    "service_ratings": "Service Ratings", "safety": "Safety",
    "vessel_details": "Vessel Details", "port_details": "Port Details",
    "detailed_tables": "Detailed Tables", "written_feedback": "Written Feedback",
    "appendix": "Appendix",
}
DEFAULT_SECTIONS = {
    "EXECUTIVE": ("executive_summary",),
    "FLEET": ("executive_summary", "fleet_operations", "port_comparison", "vessel_details", "detailed_tables", "appendix"),
    "PORT": ("executive_summary", "port_comparison", "shore_leave", "crew_experience", "obstacles", "service_ratings", "port_details", "appendix"),
    "CALL": ("executive_summary", "fleet_operations", "shore_leave", "safety", "vessel_details", "port_details", "detailed_tables", "appendix"),
    "WELFARE": ("executive_summary", "shore_leave", "crew_experience", "obstacles", "service_ratings", "port_comparison", "detailed_tables", "appendix"),
    "SAFETY": ("executive_summary", "safety", "vessel_details", "port_details", "detailed_tables", "appendix"),
}


def report_options(report_type, options=None):
    """Validate saved options and use a canonical section order."""
    if report_type not in REPORT_TYPES:
        raise ValueError("Choose a supported company report type.")
    options = dict(options or {})
    if set(options) - {"sections", "detail", "charts", "tables"}:
        raise ValueError("Unknown report configuration field.")
    detail = options.get("detail", "detailed")
    if detail not in ("summary", "detailed"):
        raise ValueError("Choose summary or detailed output.")
    sections = options.get("sections")
    if sections is None:
        sections = ["executive_summary"] if detail == "summary" else list(DEFAULT_SECTIONS[report_type])
    if not isinstance(sections, (list, tuple)) or not sections:
        raise ValueError("Select at least one report section.")
    if any(not isinstance(s, str) or s not in SECTIONS for s in sections):
        raise ValueError("Choose known report sections.")
    if len(sections) != len(set(sections)):
        raise ValueError("A report section can be selected only once.")
    for flag in ("charts", "tables"):
        if flag in options and not isinstance(options[flag], bool):
            raise ValueError(f"The {flag} setting must be true or false.")
    return {
        "sections": [s for s in SECTIONS if s in sections], "detail": detail,
        "charts": options.get("charts", True), "tables": options.get("tables", True),
    }


def report_catalogue():
    return [{"code": c, "label": label, "default_sections": list(DEFAULT_SECTIONS[c])} for c, label in REPORT_TYPES.items()]
