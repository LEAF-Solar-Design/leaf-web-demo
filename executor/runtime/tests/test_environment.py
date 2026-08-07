"""The environment label contract both log emitters depend on.

These cases exist because the failure they prevent is INVISIBLE: a metric
filter whose dimension selector finds no field publishes no datapoint at all,
and the alarms above it were configured to treat absence as non-breaching. So
a broken label here does not raise, does not log, and does not page -- it just
makes the metric quietly stop existing.
"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from executor.runtime.environment import (
    ENVIRONMENT_VARIABLE,
    UNSET_ENVIRONMENT_LABEL,
    EnvironmentLabelError,
    require_environment_label,
    resolve_environment_label,
)


class ResolveEnvironmentLabelTests(unittest.TestCase):
    """The TOTAL path used by the emitters. It must never raise."""

    def test_returns_the_configured_label(self) -> None:
        self.assertEqual("staging", resolve_environment_label({ENVIRONMENT_VARIABLE: "staging"}))

    def test_surrounding_whitespace_is_stripped_not_rejected(self) -> None:
        """A trailing newline is what reading a value from a file leaves
        behind, and it is not a reason to lose the deployment's identity.
        """
        self.assertEqual("staging", resolve_environment_label({ENVIRONMENT_VARIABLE: " staging\n"}))

    def test_an_unset_or_empty_variable_yields_the_sentinel_never_none(self) -> None:
        for value in ({}, {ENVIRONMENT_VARIABLE: ""}, {ENVIRONMENT_VARIABLE: "   "}):
            with self.subTest(value=value):
                resolved = resolve_environment_label(value)
                self.assertEqual(UNSET_ENVIRONMENT_LABEL, resolved)
                # The emitters interpolate this straight into a record. A None
                # would serialise as JSON null, which the metric filter's
                # selector treats as no better than a missing key.
                self.assertIsInstance(resolved, str)
                self.assertTrue(resolved)

    def test_a_label_that_would_corrupt_the_line_is_downgraded_not_raised(self) -> None:
        """A space would shift every field of the control plane's
        space-delimited access line; the other cases are values that have no
        business becoming a CloudWatch dimension. None of them may raise,
        because this runs inside the accounting emitter, whose failure costs
        the caller its invocation.
        """
        for value in ("two words", "has\ttab", "has\nnewline", "-leading", "a" * 65, '"quoted"'):
            with self.subTest(value=value):
                self.assertEqual(
                    UNSET_ENVIRONMENT_LABEL,
                    resolve_environment_label({ENVIRONMENT_VARIABLE: value}),
                )

    def test_realistic_labels_are_accepted(self) -> None:
        for value in ("staging", "production", "prod-us-east-1", "dev_2", "a", "a" * 64):
            with self.subTest(value=value):
                self.assertEqual(value, resolve_environment_label({ENVIRONMENT_VARIABLE: value}))

    def test_reads_the_process_environment_when_given_none(self) -> None:
        with mock.patch.dict(os.environ, {ENVIRONMENT_VARIABLE: "staging"}, clear=True):
            self.assertEqual("staging", resolve_environment_label())


class RequireEnvironmentLabelTests(unittest.TestCase):
    """The LOUD path used at startup. It must raise on exactly the cases the
    total path silently downgrades, or the sentinel becomes reachable in a
    deployed environment and every datapoint lands under a label no alarm
    watches.
    """

    def test_returns_the_configured_label(self) -> None:
        self.assertEqual("staging", require_environment_label({ENVIRONMENT_VARIABLE: "staging"}))

    def test_refuses_an_unset_or_empty_variable(self) -> None:
        for value in ({}, {ENVIRONMENT_VARIABLE: ""}, {ENVIRONMENT_VARIABLE: "  "}):
            with self.subTest(value=value):
                with self.assertRaises(EnvironmentLabelError):
                    require_environment_label(value)

    def test_refuses_every_label_the_total_path_downgrades(self) -> None:
        """The two functions must agree on what is valid. If the strict path
        accepted something the total path rejects, a container would boot
        happily and then publish under the sentinel forever.
        """
        for value in ("two words", "has\ttab", "-leading", "a" * 65, '"quoted"'):
            with self.subTest(value=value):
                self.assertEqual(
                    UNSET_ENVIRONMENT_LABEL,
                    resolve_environment_label({ENVIRONMENT_VARIABLE: value}),
                )
                with self.assertRaises(EnvironmentLabelError):
                    require_environment_label({ENVIRONMENT_VARIABLE: value})

    def test_never_refuses_a_label_the_total_path_accepts(self) -> None:
        """The converse direction, which is the one that would take a healthy
        deployment down: a strict path stricter than the emitter would refuse
        to boot a container whose telemetry would have been perfectly fine.
        """
        for value in ("staging", "production", "prod-us-east-1", "dev_2", "a", "a" * 64):
            with self.subTest(value=value):
                self.assertEqual(value, require_environment_label({ENVIRONMENT_VARIABLE: value}))

    def test_the_sentinel_is_not_silently_acceptable_as_a_real_label(self) -> None:
        """`unset` is a syntactically valid label, so it passes. That is
        deliberate and worth pinning: the guard against it is the entrypoint
        requiring the variable to be SET, not a denylist on its value. Anyone
        tightening this must not assume the string itself is refused.
        """
        self.assertEqual(
            UNSET_ENVIRONMENT_LABEL,
            require_environment_label({ENVIRONMENT_VARIABLE: UNSET_ENVIRONMENT_LABEL}),
        )


if __name__ == "__main__":
    unittest.main()
