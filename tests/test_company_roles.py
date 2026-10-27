"""The three company roles, and the ones that are not roles yet.

Owner answer to Q5: Registered/Legal Owner, ISM Manager, Commercial Manager,
with technical/crew manager and charterer explicitly requiring later definition.
All three specification documents use the same three.

Document 2 is emphatic that the three must never be conflated, which is what
most of these pin: a claim filed under the wrong role would be approved against
the wrong evidence and would not collide with the claim it should have.
"""

import unittest

from app.services.company_roles import (
    COMMERCIAL_MANAGER,
    COMPANY_ROLES,
    ISM_MANAGER,
    REGISTERED_OWNER,
    ROLE_CODES,
    UnknownCompanyRole,
    evidence_for,
    normalize_role,
    validate_role,
)


class TaxonomyTests(unittest.TestCase):
    def test_there_are_exactly_three(self):
        self.assertEqual(len(COMPANY_ROLES), 3)
        self.assertEqual(
            set(ROLE_CODES),
            {"REGISTERED_OWNER", "ISM_MANAGER", "COMMERCIAL_MANAGER"},
        )

    def test_each_one_carries_its_evidence_standard(self):
        """Super Admin approves against evidence, so the role has to name it."""
        for role in COMPANY_ROLES:
            with self.subTest(role=role.code):
                self.assertTrue(role.evidence.strip())

    def test_the_evidence_is_the_owners_wording_not_a_paraphrase(self):
        self.assertIn("registry", REGISTERED_OWNER.evidence.lower())
        self.assertIn("doc", ISM_MANAGER.evidence.lower())
        self.assertIn("smc", ISM_MANAGER.evidence.lower())
        self.assertIn("management agreement", COMMERCIAL_MANAGER.evidence.lower())


class NormalisationTests(unittest.TestCase):
    def test_the_spellings_in_the_documents_all_resolve(self):
        for value in ("Owner", "Registered Owner", "Legal Owner",
                      "Registered/Legal Owner", "REGISTERED_OWNER"):
            with self.subTest(value=value):
                self.assertEqual(normalize_role(value), "REGISTERED_OWNER")
        for value in ("ISM", "ISM Manager", "ism_manager"):
            with self.subTest(value=value):
                self.assertEqual(normalize_role(value), "ISM_MANAGER")
        for value in ("Commercial", "Commercial Manager", "COMMERCIAL_MANAGER"):
            with self.subTest(value=value):
                self.assertEqual(normalize_role(value), "COMMERCIAL_MANAGER")

    def test_a_canonical_code_is_already_canonical(self):
        for code in ROLE_CODES:
            with self.subTest(code=code):
                self.assertEqual(normalize_role(code), code)

    def test_empty_input_is_not_a_role(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                self.assertIsNone(normalize_role(value))


class RefusalTests(unittest.TestCase):
    """What must not be quietly filed under one of the three."""

    def test_the_deferred_roles_are_refused_by_name(self):
        """So the submitter is told it is undefined, not that they mistyped."""
        for value in ("charterer", "technical manager", "crew manager"):
            with self.subTest(value=value):
                with self.assertRaises(UnknownCompanyRole) as caught:
                    validate_role(value)
                self.assertIn("not defined yet", str(caught.exception))

    def test_an_unrelated_role_is_refused_with_the_accepted_list(self):
        with self.assertRaises(UnknownCompanyRole) as caught:
            validate_role("Harbour Pilot")
        message = str(caught.exception)
        for code in ROLE_CODES:
            self.assertIn(code, message)

    def test_the_three_are_never_conflated(self):
        """Document 2: never conflate ISM, commercial manager and owner."""
        resolved = {normalize_role(v) for v in ("Owner", "ISM", "Commercial")}
        self.assertEqual(len(resolved), 3)

    def test_a_substring_of_a_role_is_not_that_role(self):
        for value in ("Manager", "Commercial Owner", "ISM Owner"):
            with self.subTest(value=value):
                with self.assertRaises(UnknownCompanyRole):
                    validate_role(value)


class EvidenceTests(unittest.TestCase):
    def test_evidence_is_reachable_by_any_accepted_spelling(self):
        self.assertEqual(evidence_for("ISM"), evidence_for("ISM_MANAGER"))

    def test_evidence_for_an_unknown_role_raises_rather_than_returning_blank(self):
        with self.assertRaises(UnknownCompanyRole):
            evidence_for("charterer")


if __name__ == "__main__":
    unittest.main()
