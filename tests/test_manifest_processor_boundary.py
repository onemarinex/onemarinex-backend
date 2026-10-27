"""A crew list PDF leaves this system; CSV and Excel do not.

The read-only audit raised the PDF path as a processor boundary whose
retention and data handling were unverified, and asked that format support be
preserved while accounting for it. Three properties are worth pinning, and all
three are about the boundary rather than about parsing:

  - an operator can refuse the boundary, and the refusal names the way out;
  - CSV and Excel never touch it, so "upload it as a spreadsheet" is true;
  - the record of a crossing counts rows and never quotes one — a log that
    carried a passport number would move the exposure rather than record it.
"""

import io
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services import crew_manifest
from app.services.crew_manifest import ManifestError, ParsedCrewRow, parse_manifest

CSV_MANIFEST = b"Name,Rank,Nationality,Passport\nRavi Kumar,MASTER,India,Z5267418\n"


def _fake_anthropic(rows):
    """An Anthropic client that returns `rows` without a network call."""
    client = MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=SimpleNamespace(crew=rows)
    )
    module = MagicMock()
    module.Anthropic.return_value = client
    return module, client


class ProcessorSwitchTests(unittest.TestCase):
    def test_off_refuses_pdfs_and_names_the_way_out(self):
        with patch.object(crew_manifest, "MANIFEST_PDF_PROCESSOR", "off"):
            with self.assertRaises(ManifestError) as error:
                parse_manifest(b"%PDF-1.4 fake", "crew list.pdf")
        message = str(error.exception)
        self.assertIn("external document service", message)
        # A refusal that does not say what to do instead is a dead end.
        self.assertIn("CSV", message)

    def test_off_does_not_affect_csv(self):
        with patch.object(crew_manifest, "MANIFEST_PDF_PROCESSOR", "off"):
            parsed = parse_manifest(CSV_MANIFEST, "crew.csv")
        self.assertEqual(len(parsed.crew), 1)
        self.assertEqual(parsed.crew[0].passport_number, "Z5267418")

    def test_a_misspelt_setting_is_refused_rather_than_ignored(self):
        # "offf" must not quietly mean "keep sending crew lists to a third
        # party", which is what falling back to the default would do.
        for typo in ("offf", "none", "disabled", "anthropc"):
            with self.assertRaises(RuntimeError):
                crew_manifest.resolve_pdf_processor(typo)

    def test_the_supported_values_resolve(self):
        self.assertEqual(crew_manifest.resolve_pdf_processor(None), "anthropic")
        self.assertEqual(crew_manifest.resolve_pdf_processor(" OFF "), "off")


class CrossingRecordTests(unittest.TestCase):
    def test_a_crossing_is_recorded_without_any_manifest_content(self):
        row = ParsedCrewRow(
            name="Ravi Kumar", rank="MASTER", nationality="India",
            passport_number="Z5267418",
        )
        module, _ = _fake_anthropic([row])
        with patch.object(crew_manifest, "MANIFEST_PDF_PROCESSOR", "anthropic"), \
             patch.object(crew_manifest, "ANTHROPIC_API_KEY", "test-key"), \
             patch.dict("sys.modules", {"anthropic": module}), \
             self.assertLogs("heyports.crew_manifest", level=logging.INFO) as logs:
            crew_manifest._parse_pdf(b"%PDF-1.4 fake", "ALPHA crew list.pdf")

        recorded = "\n".join(logs.output)
        self.assertIn("manifest.pdf.external_processing", recorded)
        self.assertIn("ALPHA crew list.pdf", recorded)
        self.assertIn("rows=1", recorded)
        for secret in ("Ravi Kumar", "Z5267418", "MASTER", "India"):
            self.assertNotIn(secret, recorded)

    def test_the_whole_document_is_what_crosses(self):
        """Not an endorsement — a statement, so a later change has to face it."""
        module, client = _fake_anthropic([])
        with patch.object(crew_manifest, "MANIFEST_PDF_PROCESSOR", "anthropic"), \
             patch.object(crew_manifest, "ANTHROPIC_API_KEY", "test-key"), \
             patch.dict("sys.modules", {"anthropic": module}):
            # An empty result is refused downstream; what crossed already did.
            with self.assertRaises(ManifestError):
                crew_manifest._parse_pdf(b"%PDF-1.4 fake", "crew.pdf")

        import base64
        content = client.messages.parse.call_args.kwargs["messages"][0]["content"]
        document = next(part for part in content if part["type"] == "document")
        self.assertEqual(
            base64.standard_b64decode(document["source"]["data"]), b"%PDF-1.4 fake"
        )


class LocalFormatsStayLocalTests(unittest.TestCase):
    def test_csv_never_constructs_a_client(self):
        module, _ = _fake_anthropic([])
        with patch.dict("sys.modules", {"anthropic": module}):
            parse_manifest(CSV_MANIFEST, "crew.csv")
        module.Anthropic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
