"""Every dashboard number must be defined before anything computes it.

Document 2 section 22 is titled "no invented welfare or movement data" and
requires ten things documented per metric. Document 3's Gate G6 names
"invented movement/welfare metrics" as a no-go condition.

The failure these guard against is specific and easy: rendering 0 where the
honest answer is "we cannot tell". On a chart those look identical and mean
opposite things, and the second one is a claim about a ship nobody made.
"""

import unittest

from app.services.company_event_dictionary import EVENT_DICTIONARY
from app.services.company_metrics import (
    DEFAULT_SUPPRESSION_THRESHOLD,
    DISPLAY_TIMEZONE,
    METRICS,
    SECTIONS,
    SECTIONS_WITHOUT_METRICS,
    Availability,
    UnknownMetric,
    labelled_metrics,
    metric,
    metrics_for,
    section_manifest,
    sections_with_nothing_to_show,
    unavailable_metrics,
)


class CompletenessTests(unittest.TestCase):
    """Section 22's ten required attributes, per metric."""

    def test_every_metric_documents_all_ten(self):
        for m in METRICS:
            with self.subTest(metric=m.key):
                for attribute in ("numerator", "unique_key", "event_time",
                                  "eligible_status", "missing_data",
                                  "aggregation_grain", "timezone", "source_version"):
                    self.assertTrue(
                        str(getattr(m, attribute)).strip(),
                        f"{m.key} has no {attribute}",
                    )
                self.assertIsInstance(m.suppression_threshold, int)
                # denominator is the one that may legitimately be None: a count
                # is not always a ratio.
                self.assertTrue(hasattr(m, "denominator"))

    def test_every_metric_carries_a_caveat(self):
        for m in METRICS:
            with self.subTest(metric=m.key):
                self.assertTrue(m.caveat.strip())

    def test_every_metric_belongs_to_a_real_section(self):
        for m in METRICS:
            with self.subTest(metric=m.key):
                self.assertIn(m.section, SECTIONS)

    def test_the_timezone_is_the_one_the_owner_chose(self):
        self.assertEqual(DISPLAY_TIMEZONE, "Asia/Kolkata")
        for m in METRICS:
            self.assertEqual(m.timezone, DISPLAY_TIMEZONE)

    def test_an_unknown_metric_raises_rather_than_returning_a_default(self):
        with self.assertRaises(UnknownMetric) as caught:
            metric("crew_happiness")
        self.assertIn("pre-build gate", str(caught.exception))


class AgainstTheEventDictionaryTests(unittest.TestCase):
    """A metric's event time must be a family someone inspected the schema for."""

    def test_every_event_time_names_a_known_family_or_says_why_not(self):
        for m in METRICS:
            with self.subTest(metric=m.key):
                family = m.event_time.split(",")[0].strip()
                if family in EVENT_DICTIONARY or family.startswith(("n/a", "incidents.")):
                    continue
                self.fail(f"{m.key} counts by {family!r}, which is in no dictionary")

    def test_a_metric_whose_family_has_no_time_is_itself_unavailable(self):
        """Otherwise the section would compute something from nothing."""
        for m in METRICS:
            family = m.event_time.split(",")[0].strip()
            entry = EVENT_DICTIONARY.get(family)
            if entry is not None and not entry.usable:
                with self.subTest(metric=m.key):
                    self.assertEqual(m.availability, Availability.UNAVAILABLE)


class WhatCannotBeComputedTests(unittest.TestCase):
    def test_the_three_with_no_source(self):
        self.assertEqual(
            set(unavailable_metrics()),
            {"welfare_engagement"},
        )

    def test_shore_pass_approval_has_no_timestamp_to_slice_by(self):
        self.assertEqual(metric("passes_approved").availability, Availability.LABELLED)
        self.assertIn("proxy", metric("passes_approved").label)

    def test_sign_on_off_invents_no_count(self):
        """TEN-18: Crew Movements without an actual event source."""
        m = metric("sign_on_off")
        self.assertEqual(m.availability, Availability.LABELLED)
        self.assertIn("planned transport", m.missing_data.lower())

    def test_welfare_claims_no_wellbeing_outcome(self):
        """Section 22 forbids inferring wellbeing from rides."""
        caveat = metric("welfare_engagement").caveat.lower()
        self.assertIn("not evidence of welfare", caveat)

    def test_welfare_is_the_one_section_that_can_show_nothing(self):
        self.assertEqual(sections_with_nothing_to_show(), ("welfare_intelligence",))


class LabellingTests(unittest.TestCase):
    def test_the_labelled_ones_say_what_is_wrong_with_them(self):
        for key in labelled_metrics():
            with self.subTest(metric=key):
                self.assertGreater(len(metric(key).caveat), 40)

    def test_eligibility_admits_it_has_no_history(self):
        m = metric("eligible_crew")
        self.assertEqual(m.availability, Availability.LABELLED)
        self.assertIn("current state", m.caveat.lower())

    def test_an_unrecorded_return_is_not_a_crew_member_still_ashore(self):
        self.assertIn("nobody wrote down", metric("actually_ashore").missing_data)

    def test_incidents_resolved_warns_the_two_counts_need_not_reconcile(self):
        self.assertIn("need not reconcile", metric("incidents_resolved").caveat)

    def test_a_booking_is_not_a_trip_or_a_welfare_outcome(self):
        caveat = metric("booking_activity").caveat.lower()
        self.assertIn("not a trip", caveat)
        self.assertIn("welfare outcome", caveat)


class SuppressionTests(unittest.TestCase):
    """TEN-19: no individual inference from a tiny cohort."""

    def test_every_metric_carries_a_threshold(self):
        for m in METRICS:
            with self.subTest(metric=m.key):
                if m.key in {
                    "incidents_reported",
                    "incidents_resolved",
                    "sos_raised",
                    "welfare_engagement",
                }:
                    self.assertEqual(m.suppression_threshold, 5)
                else:
                    self.assertEqual(m.suppression_threshold, 0)

    def test_the_default_is_above_the_case_the_document_names(self):
        """Document 2: on a vessel with three crew an incident rate names the
        individual. A threshold of 3 would not withhold that figure."""
        self.assertGreater(DEFAULT_SUPPRESSION_THRESHOLD, 3)


class ManifestTests(unittest.TestCase):
    def test_all_eight_sections_appear(self):
        self.assertEqual(len(SECTIONS), 8)
        self.assertEqual(len(section_manifest()), 8)

    def test_the_two_without_metrics_are_named_rather_than_missing(self):
        for key in SECTIONS_WITHOUT_METRICS:
            self.assertIn(key, SECTIONS)
            self.assertEqual(metrics_for(key), ())

    def test_a_section_is_buildable_only_if_something_in_it_can_be_computed(self):
        for manifest in section_manifest():
            if not manifest.metrics:
                continue
            with self.subTest(section=manifest.key):
                expected = any(
                    m.availability != Availability.UNAVAILABLE for m in manifest.metrics
                )
                self.assertEqual(manifest.buildable, expected)


if __name__ == "__main__":
    unittest.main()
