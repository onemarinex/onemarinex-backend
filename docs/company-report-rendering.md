# Company report PDF rendering

`app.services.company_pdf.render_report(payload)` renders all six company report types. The engine uses selectable text, embedded fonts, vector charts and real tables. It does not render screenshots or fetch data.

## Integration contract

The caller must authorize the company and membership, filter every source by vessel identity and role dates, apply cell suppression and current field grants, and validate the saved snapshot before **each** PDF preview or download. A section choice does not grant access.

Use the exact returned PDF bytes for the browser preview and download. An immutable payload produces byte-identical output. The timestamp comes from `payload.generated_at`; rendering never adds the current time.

```python
from app.services.company_pdf import render_report
from app.services.company_report_templates import (
    REPORT_TYPES, SECTIONS, report_catalogue, report_options,
)

pdf_bytes = render_report(authorized_snapshot)
```

Required identity fields:

- `report_type`: EXECUTIVE, FLEET, PORT, CALL, WELFARE or SAFETY.
- `company_name` or `branding.name`: the authenticated company's name.
- `generated_at`: the snapshot generation instant with a time zone.
- `period.start` and `period.end`: the half-open reporting window, with time zones.

`branding.logo` may contain a validated PNG or JPEG data URL. The engine never opens a remote URL or local file from this field. Invalid or missing logos use the company name. No HeyPorts logo is added. Each page has the company header, reporting period, generated timestamp, Page X of Y and Powered by HeyPorts.

### Configuration

Store these values in `payload.configuration`, so saved preview and download use the same choices:

```json
{
  "sections": ["executive_summary", "fleet_operations", "detailed_tables", "appendix"],
  "detail": "detailed",
  "charts": true,
  "tables": true
}
```

`report_options(report_type, options)` rejects unknown fields, unknown or repeated sections, empty selections and invalid booleans. It returns the canonical section order. `detail: "summary"` defaults to the executive section. Explicit summary sections limit detail rows to five. Section IDs and labels are in `SECTIONS`; defaults for each template are in `DEFAULT_SECTIONS`.

### Authorized data blocks

The renderer supports the existing report snapshot blocks: `summary`, `operations`, `survey`, `service_reviews`, `comparison` and `calls`. Missing metrics remain Unavailable. Zero stays zero. Suppressed survey/review groups hide their child values.

Optional prepared blocks:

- `findings`: up to three evidence-backed executive statements. The renderer does not infer causes or rank ports by welfare.
- `port_comparison`: authorized aggregate rows with `name`, `recorded_calls`, `booked_rides` and `started_passenger_seats`.
- `vessels`: authorized display rows with `name`, `imo_number`, `role_label` and `period_label`.
- `ports`: authorized rows with `name`, `country_name`, `recorded_calls` and optional `coverage`.
- `safety`: authorized `incident_count`, `unresolved_count`, `resolved_count`, `average_resolution_hours`, `coverage` and `cases`. Omit unavailable counters. A case row has `reference`, `vessel_name`, `port_name`, `urgency`, `severity` and `status`. Urgency is a separate review priority; it is never treated as incident severity or resolution status. Supply private fields only when their current field grants permit them.
- `approved_feedback`: approved `{context, text}` records. There is no fallback to raw crew messages, review text or incident notes.
- `charts`: precomputed authorized series. A chart contains `section`, `kind` (`line` or `bar`), `title`, optional `unit` and `description`, and `items` with `label`, `value` and optional `suppressed`. Chart values must come from the shared metric/aggregation service. The renderer does not calculate alternative business metrics. It shows up to 12 trend points or eight comparison categories; comparison truncation is labelled and detail tables can retain all records.
- `data_mode: "DEMO"`: adds the standalone fictional-data notice. Demo payloads must come from an isolated adapter. Never combine them with Live snapshots.

Raw unknown payload keys are ignored. This is defense in depth; it does not replace server authorization or grant revalidation.

## Compatibility

The existing `render(title, company, rows, metadata, logo=None)` signature remains for protected disclosure exports. Long approved notes flow as text across pages. Markup is escaped. These exports also use the shared company header and numbered footer.

## Verification

The pure renderer suite is `tests/test_company_pdf.py`. It needs ReportLab, Pillow and pypdf. The bundled Codex Python runtime has these dependencies. A normal backend test run without pypdf explicitly skips this inspection suite; it must not be counted as a pass.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=tests:. \
  /Users/trexii/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  -m unittest tests/test_company_pdf.py -v
```

Eleven checks cover six templates, one-page executive output, byte-identical rendering, 500-vessel final-record retention and repeated headers, section removal, suppression, raw-field exclusion, branding image/fallback, long approved notes, long unbroken names and zero versus missing values.

Synthetic sample PDFs and page previews are in `new work/Round-2-Delivery/report-pdf-evidence` in the parent workspace. The samples use Aster Maritime, a fictional fixture. They do not prove production authorization, report UI completion or correct production data. The official OneMarineX logo is still awaiting identification; the generic image test is only a renderer test.
