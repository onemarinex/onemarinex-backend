"""Actual HTTP import errors identify source row and date field, without parsing ambiguous dates."""
import csv
import io
import unittest
from datetime import datetime, timezone
from app.services.company_fleet_declarations import boundary
import test_company_fleet_acceptance_http as fleet
from test_company_fleet_acceptance_http import number, BASE

class ImportDateHTTP(unittest.TestCase):
    setUp = fleet.FleetAcceptanceHTTP.setUp
    tearDown = fleet.FleetAcceptanceHTTP.tearDown
    upload = fleet.FleetAcceptanceHTTP.upload
    preview = fleet.FleetAcceptanceHTTP.preview
    confirm = fleet.FleetAcceptanceHTTP.confirm
    headers = fleet.FleetAcceptanceHTTP.headers
    request = fleet.FleetAcceptanceHTTP.request
    booking = fleet.FleetAcceptanceHTTP.booking
    def test_ambiguous_date_names_source_row_and_field_without_creating_claim(self):
        draft = self.upload([[number(51), "Date fixture", "Yes", "No", "No", "01-10-2026", ""]])
        data = self.preview(draft)
        self.assertEqual(data["counts"]["invalid"], 1)
        finding = data["findings"][0]
        self.assertEqual(finding["row"], 2)
        self.assertIn("YYYY-MM-DD", finding["message"])
        self.assertIn("start", finding["message"].lower())
        self.assertTrue(any(item["field"] == "start_date" for item in finding["field_errors"]))
        error = self.request("GET", BASE + f"/imports/{draft['id']}/errors.csv", self.admin)
        rows = list(csv.reader(io.StringIO(error.text)))
        self.assertIn("YYYY-MM-DD", rows[1][-1])
        self.assertEqual(self.confirm(draft)["counts"]["accepted"], 0)

    def test_xlsx_ambiguous_date_reports_source_field_and_iso_guidance(self):
        from openpyxl import Workbook
        book = Workbook();sheet = book.active;sheet.append(fleet.LABELS)
        sheet.append([number(52), "Spreadsheet fixture", "Yes", "No", "No", "10/01/2026", ""])
        out = io.BytesIO();book.save(out);book.close()
        response = self.client.post(BASE + "/imports", headers=self.headers(self.admin), files={"file": ("fleet.xlsx", out.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
        self.assertEqual(response.status_code, 201, response.text)
        result = self.preview(response.json())
        self.assertEqual(result["counts"]["invalid"], 1)
        self.assertEqual(result["findings"][0]["row"], 2)
        self.assertIn("YYYY-MM-DD", result["findings"][0]["message"])
        self.assertEqual(result["findings"][0]["field_errors"][0]["field"], "start_date")

class DateBoundary(unittest.TestCase):
    def test_iso_dates_are_ist_midnight_and_preserve_explicit_timezone(self):
        self.assertEqual(boundary("2026-10-01"), datetime(2026,9,30,18,30,tzinfo=timezone.utc))
        self.assertEqual(boundary("2026-10-01T00:00:00+05:30"), boundary("2026-10-01"))
        for value in ["01-10-2026", "10/01/2026", "2026-02-30", "20261001", "2026-10-01T00:00:00"]:
            with self.subTest(value=value), self.assertRaises(ValueError):boundary(value)
