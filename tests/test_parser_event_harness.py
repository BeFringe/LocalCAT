"""Wave 0 contract tests for reusable hostile raw-event doubles."""

from __future__ import annotations

from dataclasses import replace
import unittest

from tests.parser_contract_test_support import (
    InjectedConsumerFailure,
    InjectedRawFailure,
    ScriptedRawIterator,
    StreamEnd,
    StubIssue,
    StubRecord,
    VALID_EVENTS,
    assert_views_equivalent,
    consume_with_injected_failure,
    project_event_view,
    record_then_fatal_tail,
)


class RawEventDoubleTests(unittest.TestCase):
    def test_valid_stream_only_proves_eof_after_next_observes_stop_iteration(self) -> None:
        stream = ScriptedRawIterator(VALID_EVENTS)
        observed = [next(stream), next(stream), next(stream)]
        self.assertEqual(observed, list(VALID_EVENTS))
        self.assertIs(stream.end, StreamEnd.OPEN)

        with self.assertRaises(StopIteration):
            next(stream)

        self.assertIs(stream.end, StreamEnd.NATURAL_EOF)

    def test_fatal_tail_follows_a_provisional_record(self) -> None:
        events = record_then_fatal_tail()
        stream = ScriptedRawIterator(events)
        observed = list(stream)

        self.assertIsInstance(observed[0], StubRecord)
        self.assertIsInstance(observed[-1], StubIssue)
        self.assertEqual(observed[-1].severity, "fatal")
        self.assertIs(stream.end, StreamEnd.NATURAL_EOF)

    def test_early_close_never_becomes_natural_eof(self) -> None:
        stream = ScriptedRawIterator(VALID_EVENTS)
        self.assertEqual(next(stream), VALID_EVENTS[0])

        stream.close()

        self.assertIs(stream.end, StreamEnd.EARLY_CLOSE)
        with self.assertRaises(StopIteration):
            next(stream)
        self.assertIs(stream.end, StreamEnd.EARLY_CLOSE)

    def test_missing_eof_is_a_finite_raw_exception(self) -> None:
        stream = ScriptedRawIterator(
            (StubRecord(local_id="record-1", value="provisional"),),
            fail_instead_of_eof=True,
        )
        self.assertIsInstance(next(stream), StubRecord)

        with self.assertRaisesRegex(InjectedRawFailure, "without an observable EOF"):
            next(stream)

        self.assertIs(stream.end, StreamEnd.RAW_EXCEPTION)
        with self.assertRaisesRegex(InjectedRawFailure, "without an observable EOF"):
            next(stream)
        self.assertIs(stream.end, StreamEnd.RAW_EXCEPTION)

    def test_consumer_failure_is_distinct_from_raw_eof(self) -> None:
        stream = ScriptedRawIterator(VALID_EVENTS)

        with self.assertRaisesRegex(InjectedConsumerFailure, "after 2 provisional"):
            consume_with_injected_failure(stream, fail_after_events=2)

        self.assertIs(stream.end, StreamEnd.OPEN)
        stream.close()
        self.assertIs(stream.end, StreamEnd.EARLY_CLOSE)

    def test_public_double_configuration_rejects_ambiguous_values(self) -> None:
        with self.assertRaisesRegex(TypeError, "exact bool"):
            ScriptedRawIterator(VALID_EVENTS, fail_instead_of_eof=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "severity"):
            StubIssue("PARSER.TEST.INVALID", "info")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "positive exact int"):
            StubIssue("PARSER.TEST.INVALID", "fatal", 0)


class EventViewComparatorTests(unittest.TestCase):
    def test_equivalent_iterator_and_materialized_views_compare_all_fields(self) -> None:
        iterator_view = project_event_view(iter(VALID_EVENTS))
        materialized_view = project_event_view(tuple(VALID_EVENTS))

        assert_views_equivalent(iterator_view, materialized_view)
        self.assertEqual(iterator_view.record_count, 2)
        self.assertEqual(iterator_view.warning_count, 1)
        self.assertEqual(iterator_view.fatal_count, 0)
        self.assertEqual(
            iterator_view.issue_counts,
            (("PARSER.TEST.WARNING", 1),),
        )
        self.assertEqual(
            iterator_view.event_order,
            (
                ("record", "record-1"),
                ("warning", "PARSER.TEST.WARNING"),
                ("record", "record-2"),
            ),
        )

    def test_comparator_rejects_each_view_field_drift(self) -> None:
        expected = project_event_view(VALID_EVENTS)
        drifted = (
            replace(expected, records=tuple(reversed(expected.records))),
            replace(expected, issues=()),
            replace(expected, record_count=expected.record_count + 1),
            replace(expected, warning_count=expected.warning_count + 1),
            replace(expected, fatal_count=expected.fatal_count + 1),
            replace(expected, issue_counts=(("PARSER.TEST.OTHER", 1),)),
            replace(expected, event_order=tuple(reversed(expected.event_order))),
        )
        for observed in drifted:
            with self.subTest(field_drift=observed), self.assertRaisesRegex(
                AssertionError,
                "event views differ",
            ):
                assert_views_equivalent(expected, observed)

    def test_stub_projection_rejects_unknown_events_explicitly(self) -> None:
        with self.assertRaisesRegex(TypeError, "unsupported stub event type"):
            project_event_view((object(),))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
