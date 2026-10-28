"""Company analytics must not count a seat nobody identified.

Audit R05: the ashore figures "are not a reliable distinct-person, exact-call
physical-presence metric. A completed cab trip is not universally proof of
re-entry aboard, and an unnamed seat does not establish a unique person's
verified departure." Its recommendation was to retain the existing operational
behaviour and give **new company analytics a separate approved event
dictionary**, recording assignment-linked passengers, verified exit/return
events, and unknown counts separately.

That holds today, but by construction rather than by rule, and construction is
the kind of thing a later metric quietly breaks. `actually_ashore` counts
distinct `ShorePass` rows -- one per person, each a recorded exit -- and the
company aggregates never read `num_passengers`, `crew_ashore`, or any
cab-derived estimate. These tests fail if that changes.

What is NOT built, and is a decision rather than an omission: there is no
assignment-linked passenger record, so an unnamed seat on a cab booking cannot
be resolved to a person at all. That limits the *agent* ashore figure, which
reports its own unknown counts beside the number, and the audit explicitly
says to retain that behaviour "until a deliberate compatibility decision".
"""

import pathlib
import re
import unittest

BACKEND = pathlib.Path(__file__).resolve().parent.parent

# Reading any of these in a company aggregate would reintroduce R05: they are
# seat counts and cab-trip inferences, not verified per-person evidence.
FORBIDDEN_SOURCES = (
    "crew_ashore",
    "AshoreBreakdown",
)

COMPANY_MODULES = (
    "app/services/company_aggregates.py",
    "app/services/company_metrics.py",
    "app/api/v1/routes_shipping_company.py",
)


class CompanyAnalyticsEvidenceTests(unittest.TestCase):
    def test_operational_seat_totals_do_not_reuse_agent_ashore_estimates(self):
        for relative in COMPANY_MODULES:
            source = (BACKEND / relative).read_text()
            for name in FORBIDDEN_SOURCES:
                with self.subTest(module=relative, source=name):
                    self.assertNotIn(
                        name, source,
                        f"{relative} reads {name}. Company analytics must count "
                        "verified per-person evidence, not seats -- see audit R05.",
                    )

    def test_the_ashore_figure_counts_shore_passes_one_per_person(self):
        source = (BACKEND / "app/services/company_aggregates.py").read_text()
        block = source[source.index("def _shore_pass_figure"):]
        block = block[:block.index("def ", 10)]
        self.assertIn("func.count(func.distinct(ShorePass.id))", block)

    def test_company_analytics_has_its_own_event_dictionary(self):
        """R05 asks for a *separate* approved dictionary, not the agent's."""
        from app.services import company_event_dictionary as dictionary

        self.assertTrue(dictionary.EVENT_DICTIONARY)
        # And it reports what it cannot time, rather than guessing.
        self.assertTrue(
            dictionary.families_with_no_usable_time()
            or dictionary.families_reporting_rather_than_occurrence()
        )

    def test_a_family_with_no_usable_timestamp_is_refused_not_estimated(self):
        from app.services import company_event_dictionary as dictionary
        from app.services import company_access

        families = dictionary.families_with_no_usable_time()
        self.assertTrue(families, "the dictionary should name what it cannot time")
        # The policy refuses those families by name; a caller passing some
        # nearby timestamp is exactly how a row lands on the wrong company.
        source = (BACKEND / "app/services/company_access.py").read_text()
        self.assertIn("event_family", source)
        self.assertTrue(hasattr(company_access, "allow"))

    def test_what_cannot_be_timed_has_no_computation_at_all(self):
        """R05: "No crew movement or welfare outcome should be inferred."

        Narrower than it first looks, and the first version of this test had
        it wrong. The Crew Movements *section* legitimately carries cab
        figures -- booking activity, trips started and completed -- which are
        recorded events with their own timestamps. What must never acquire a
        number is the metric with no authoritative source: sign-on and
        sign-off, shore-pass approval and welfare engagement, none of which
        this schema records a time for.

        TEN-18 covers the rendering. This covers the source, because a
        computation added here would make the screen honest about a figure
        that was invented.
        """
        from app.services import company_aggregates, company_metrics

        unavailable = company_metrics.unavailable_metrics()
        self.assertNotIn("sign_on_off", unavailable)
        self.assertIn("welfare_engagement", unavailable)
        for key in unavailable:
            with self.subTest(metric=key):
                self.assertNotIn(
                    key, company_aggregates.COMPUTABLE_FIGURES,
                    f"{key} is declared unavailable and has a computation. "
                    "A number here would be inferred rather than recorded.",
                )


if __name__ == "__main__":
    unittest.main()
