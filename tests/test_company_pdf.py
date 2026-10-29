"""Pure renderer acceptance. Run with the bundled Python runtime (pypdf).

No application database, environment file, network or email access is used.
"""
import base64
import copy
import io
import unittest
try:
    from pypdf import PdfReader
except ImportError:
    raise unittest.SkipTest("PDF inspection needs pypdf; run this suite with the bundled Python runtime.")
from PIL import Image
from app.services.company_pdf import render_report, render
from app.services.company_report_templates import REPORT_TYPES, report_options
from app.services.company_pdf_components import draw_text, FONT
from reportlab.pdfbase import pdfmetrics
from fixtures.reporting.company_report_samples import payload


class CompanyPDF(unittest.TestCase):
    def pages(self, data):
        self.assertTrue(data.startswith(b"%PDF"))
        return PdfReader(io.BytesIO(data)).pages

    def test_executive_is_one_page_with_branded_total_footer_and_real_vector_chart(self):
        pages = self.pages(render_report(payload()))
        self.assertEqual(len(pages), 1)
        text = pages[0].extract_text()
        for value in ("Aster Maritime", "Executive Fleet Intelligence", "FICTIONAL DATA", "Page 1 of 1", "Powered by HeyPorts", "Recorded call activity", "Crew bookings"):
            self.assertIn(value, text)
        self.assertNotIn("commenced passenger-seat events", text)
        self.assertGreater(pages[0].get_contents().get_data().count(b" l"), 4)
        self.assertFalse(pages[0].images)

    def test_same_snapshot_has_byte_identical_preview_and_download(self):
        snapshot = payload("WELFARE")
        self.assertEqual(render_report(snapshot), render_report(snapshot))

    def test_unbroken_legal_name_cannot_overflow_its_header_box(self):
        from unittest.mock import Mock
        canvas = Mock()
        draw_text(canvas, "LONGCOMPANY" * 30, 0, 0, 100, size=11, max_lines=2)
        lines = [call.args[2] for call in canvas.drawString.call_args_list]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("..."))
        self.assertTrue(all(pdfmetrics.stringWidth(line, FONT, 11) <= 100 for line in lines))

    def test_all_six_templates_render_without_changing_the_input(self):
        for kind in REPORT_TYPES:
            data = payload(kind, vessels=1)
            original = copy.deepcopy(data)
            pages = self.pages(render_report(data))
            text = "\n".join(page.extract_text() for page in pages)
            self.assertIn(REPORT_TYPES[kind], text)
            for n, page in enumerate(pages, 1):
                self.assertIn(f"Page {n} of {len(pages)}", page.extract_text())
            self.assertEqual(data, original)

    def test_table_headers_repeat_and_the_final_record_is_preserved_at_scale(self):
        data = payload("FLEET", vessels=500, ports=100, annual=True)
        pages = self.pages(render_report(data, {"sections": ["detailed_tables"]}))
        self.assertGreater(len(pages), 20)
        for page in pages:
            text = page.extract_text()
            self.assertIn("Call date (UTC)", text)
            self.assertIn("MV Aster", text)
        self.assertIn("MV Aster 0500", pages[-1].extract_text())

    def test_deselected_sections_and_raw_restricted_fields_are_absent(self):
        data = payload("WELFARE")
        data["approved_feedback"] = [{"context": "Approved", "text": "APPROVED-COMMENT"}]
        data["crew"] = [{"email": "PRIVATE-CREW-EMAIL"}]
        data["survey"]["message"] = "PRIVATE-SURVEY-MESSAGE"
        data["safety"]["cases"][0]["description"] = "PRIVATE-INCIDENT-NARRATIVE"
        text = "\n".join(p.extract_text() for p in self.pages(render_report(data, {"sections": ["shore_leave"]})))
        for absent in ("Welfare Obstacles", "Service Ratings", "APPROVED-COMMENT", "PRIVATE-CREW-EMAIL", "PRIVATE-SURVEY-MESSAGE", "PRIVATE-INCIDENT-NARRATIVE", "Appendix"):
            self.assertNotIn(absent, text)
        comments = "\n".join(p.extract_text() for p in self.pages(render_report(data, {"sections": ["written_feedback"]})))
        self.assertIn("APPROVED-COMMENT", comments)
        self.assertNotIn("PRIVATE-SURVEY-MESSAGE", comments)

    def test_suppressed_groups_do_not_render_their_child_values(self):
        data = payload("WELFARE")
        data["survey"].update(suppressed=True, barriers=[{"label": "HIDDEN-BARRIER", "respondents": 123456789}])
        data["service_reviews"]["groups"][0].update(suppressed=True, average_rating=987654321)
        text = "\n".join(p.extract_text() for p in self.pages(render_report(data, {"sections": ["crew_experience", "obstacles", "service_ratings"]})))
        self.assertIn("withheld", text)
        self.assertNotIn("HIDDEN-BARRIER", text)
        self.assertNotIn("987654321", text)

    def test_company_logo_is_embedded_and_invalid_logo_uses_name_fallback(self):
        logo = io.BytesIO()
        Image.new("RGB", (80, 30), "#087F83").save(logo, format="PNG")
        data = payload()
        data["branding"]["logo"] = "data:image/png;base64," + base64.b64encode(logo.getvalue()).decode()
        self.assertEqual(len(self.pages(render_report(data))[0].images), 1)
        data["branding"]["logo"] = "https://example.invalid/company-logo.png"
        page = self.pages(render_report(data))[0]
        self.assertFalse(page.images)
        self.assertIn("Aster Maritime", page.extract_text())

    def test_legacy_disclosure_keeps_long_approved_content_and_escapes_markup(self):
        note = "Approved <note> & context " * 450
        pages = self.pages(render("Approved case information", "Company <A>", [("Reviewed note", note)], "Generated 2026-10-29T09:30:00Z"))
        text = "\n".join(p.extract_text() for p in pages)
        self.assertGreater(len(pages), 1)
        self.assertIn("Company <A>", text)
        self.assertEqual(text.count("Approved <note>"), 450)

    def test_configuration_rejects_unknown_empty_and_malformed_choices(self):
        for options in ({"sections": []}, {"sections": ["raw_crew"]}, {"charts": "false"}, {"detail": "everything"}, {"sections": ["safety", "safety"]}, {"raw_records": True}):
            with self.assertRaises(ValueError):
                report_options("FLEET", options)
        with self.assertRaises(ValueError):
            report_options("UNKNOWN")

    def test_zero_data_and_missing_data_remain_distinct(self):
        data = payload()
        data["summary"]["booked_rides"] = 0
        data["operations"]["average_time_ashore_minutes"] = None
        values = "\n".join(p.extract_text() for p in self.pages(render_report(data)))
        self.assertIn("\n0\n", values)
        self.assertIn("Unavailable", values)


if __name__ == "__main__":
    unittest.main()
