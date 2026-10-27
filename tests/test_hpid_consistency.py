"""An HPID is per port visit, and its rendering must not change who it names.

HP-{passport}-{NAT}-{PORT} is issued fresh each time a crew member comes to a
port, so the PORT fragment is meant to differ between visits. The rendering was
not: both trailing fragments came from raw slicing, so one person at one port
produced different identifiers depending on how the source was written.

The dangerous mistake in either direction:
  - collapsing the port would merge two separate visits into one;
  - not collapsing the rendering leaves one visit looking like two people.
"""

import unittest

from app.services.crew_identity import (
    AMBIGUOUS_NATIONALITY_FRAGMENTS,
    canonical_hpid,
    canonical_nationality_fragment,
    hpid_is_conforming,
    hpids_match,
)
from app.services.crew_service import generate_hpid


class PortIsPartOfTheIdentityTests(unittest.TestCase):
    """The fragment that must never be collapsed."""

    def test_two_ports_are_two_visits_not_one_person(self):
        self.assertFalse(hpids_match("HP-R1522877-IN-VIS", "HP-R1522877-IN-KRI"))

    def test_the_port_survives_canonicalisation(self):
        self.assertTrue(canonical_hpid("HP-R1522877-IN-KRI").endswith("-KRI"))


class RenderingTests(unittest.TestCase):
    """The fragments that must be collapsed."""

    def test_case_and_whitespace_do_not_make_a_second_person(self):
        self.assertTrue(hpids_match("  hp-r1522877-in-vis ", "HP-R1522877-IN-VIS"))

    def test_an_unambiguous_nationality_spelling_collapses(self):
        self.assertTrue(hpids_match("HP-P1234567-FIL-VIS", "HP-P1234567-PH-VIS"))
        self.assertTrue(hpids_match("HP-P1234567-UKR-VIS", "HP-P1234567-UA-VIS"))

    def test_a_fragment_cannot_be_trimmed_back_to_a_code(self):
        """FILIPINO gives FIL for PH, UKRAINIAN gives UKR for UA."""
        self.assertEqual(canonical_nationality_fragment("FIL"), "PH")
        self.assertEqual(canonical_nationality_fragment("UKR"), "UA")


class AmbiguityTests(unittest.TestCase):
    """What the identifier cannot express, and must not pretend to."""

    def test_ind_names_two_countries_and_is_left_alone(self):
        self.assertIn("IND", AMBIGUOUS_NATIONALITY_FRAGMENTS)
        self.assertEqual(canonical_nationality_fragment("IND"), "IND")

    def test_an_ambiguous_fragment_does_not_collapse_into_a_code(self):
        """Guessing would silently merge India and Indonesia."""
        self.assertFalse(hpids_match("HP-P1234567-IND-VIS", "HP-P1234567-IN-VIS"))

    def test_new_identifiers_avoid_the_ambiguity_entirely(self):
        self.assertIn("-IN-", generate_hpid("P1234567", "INDIAN", "port_visakhapatnam"))


class NotAnHpidTests(unittest.TestCase):
    def test_a_retired_products_identifier_is_not_coerced(self):
        self.assertIsNone(canonical_hpid("ITEM46-0-005C"))

    def test_none_and_empty_are_not_identifiers(self):
        for value in (None, "", "   ", "-", "HP-"):
            with self.subTest(value=value):
                self.assertIsNone(canonical_hpid(value))

    def test_nothing_matches_a_non_identifier(self):
        self.assertFalse(hpids_match("ITEM46-0-005C", "ITEM46-0-005C"))

    def test_the_short_forms_are_reported_as_non_conforming(self):
        for value in ("HP-9876-011", "HP-BUGBASH-NOACC"):
            with self.subTest(value=value):
                self.assertFalse(hpid_is_conforming(value))
        self.assertTrue(hpid_is_conforming("HP-R1522877-IND-VIS"))


class GeneratorStabilityTests(unittest.TestCase):
    """One visit, one identifier, however the source was written."""

    def test_every_spelling_of_one_visit_gives_one_identifier(self):
        produced = {
            generate_hpid("R1522877", nationality, port)
            for nationality in ("IN", "INDIAN", "India", "Indian")
            for port in ("port_visakhapatnam", "Port of Visakhapatnam", "VISAKHAPATNAM")
        }
        self.assertEqual(produced, {"HP-R1522877-IN-VIS"})

    def test_a_different_port_still_gives_a_different_identifier(self):
        self.assertNotEqual(
            generate_hpid("R1522877", "INDIAN", "port_visakhapatnam"),
            generate_hpid("R1522877", "INDIAN", "port_krishnapatnam"),
        )

    def test_an_unrecognised_nationality_still_produces_one(self):
        """Falling back beats failing: an odd nationality is not a crash."""
        self.assertTrue(
            generate_hpid("R1522877", "Atlantean", "port_visakhapatnam").startswith("HP-")
        )


if __name__ == "__main__":
    unittest.main()
