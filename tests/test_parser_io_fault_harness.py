"""Wave 0 tests for the Parser I/O fault-injection support.

These tests exercise test-only doubles.  They do not implement or authorize the
production Source Boundary, writer, Parser, or resource transaction semantics.
"""

from __future__ import annotations

from dataclasses import replace
import unittest

from tests.parser_io_test_support import (
    FaultInjector,
    FaultPoint,
    FaultingFilesystemOps,
    FaultingResourceCommitPort,
    InjectedIOFault,
    ParserIOFaultFixture,
)


def _exercise_candidate_writer(
    fixture: ParserIOFaultFixture,
    injector: FaultInjector,
    payload: bytes,
) -> None:
    """Small test subject proving that each injected operation is observable."""

    operations = FaultingFilesystemOps(injector)
    temporary = fixture.writer_temp
    try:
        operations.write_temp(temporary, payload)
        operations.fsync(temporary)
        operations.replace(temporary, fixture.target)
        fixture.receipts.issue_write("writer-receipt")
    finally:
        temporary.unlink(missing_ok=True)


class ParserIOFaultFixtureTests(unittest.TestCase):
    def test_declares_every_wave_zero_fault_point(self) -> None:
        self.assertEqual(
            {point.value for point in FaultPoint},
            {
                "source.concurrent_change",
                "source.root_escape",
                "source.non_regular",
                "source.snapshot_stale",
                "writer.temp_write",
                "writer.fsync",
                "writer.replace",
                "resource.commit",
            },
        )

    def test_source_cases_are_isolated_and_make_identity_changes_observable(self) -> None:
        with ParserIOFaultFixture() as fixture:
            temporary_root = fixture.temporary_root
            initial = fixture.capture_source_identity()

            self.assertTrue(fixture.source.is_relative_to(temporary_root))
            self.assertTrue(fixture.target.is_relative_to(temporary_root))
            self.assertTrue(fixture.resource_target.is_relative_to(temporary_root))
            self.assertTrue(fixture.outside_source.is_relative_to(temporary_root))
            self.assertFalse(fixture.outside_source.resolve().is_relative_to(fixture.safe_root))
            self.assertTrue(fixture.non_regular_source.is_dir())
            self.assertTrue(
                fixture.source_reference(FaultPoint.SOURCE_ROOT_ESCAPE).resolve()
                == fixture.outside_source.resolve()
            )
            self.assertEqual(
                fixture.source_reference(FaultPoint.SOURCE_NON_REGULAR),
                fixture.non_regular_source,
            )

            fixture.mutate_source_concurrently(b"changed-input")
            concurrent = fixture.capture_source_identity()
            self.assertNotEqual(concurrent, initial)

            stale_baseline = fixture.capture_source_identity()
            fixture.make_snapshot_stale(b"stale--input")
            stale_current = fixture.capture_source_identity()
            self.assertTrue(fixture.snapshot_is_stale(stale_baseline))
            self.assertNotEqual(stale_current, stale_baseline)

        self.assertFalse(temporary_root.exists())

    def test_injector_covers_each_source_failure_checkpoint(self) -> None:
        for point in (
            FaultPoint.SOURCE_CONCURRENT_CHANGE,
            FaultPoint.SOURCE_ROOT_ESCAPE,
            FaultPoint.SOURCE_NON_REGULAR,
            FaultPoint.SOURCE_SNAPSHOT_STALE,
        ):
            with self.subTest(point=point), ParserIOFaultFixture() as fixture:
                injector = FaultInjector(point)
                before = fixture.capture_authority_state()
                outside_before = fixture.outside_source.read_bytes()
                with self.assertRaisesRegex(InjectedIOFault, point.value):
                    injector.checkpoint(point)
                after = fixture.capture_authority_state()

                fixture.assert_failed_preserving_authority(before, after)
                self.assertEqual(injector.seen, (point,))
                self.assertEqual(fixture.outside_source.read_bytes(), outside_before)

    def test_injector_rejects_non_enum_fault_points(self) -> None:
        with self.assertRaisesRegex(TypeError, "exact FaultPoint"):
            FaultInjector("writer.replace")  # type: ignore[arg-type]
        injector = FaultInjector()
        with self.assertRaisesRegex(TypeError, "exact FaultPoint"):
            injector.checkpoint("writer.replace")  # type: ignore[arg-type]
        self.assertEqual(injector.seen, ())

    def test_writer_faults_preserve_bytes_and_never_issue_receipt(self) -> None:
        for point in (
            FaultPoint.WRITER_TEMP_WRITE,
            FaultPoint.WRITER_FSYNC,
            FaultPoint.WRITER_REPLACE,
        ):
            with self.subTest(point=point), ParserIOFaultFixture() as fixture:
                before = fixture.capture_authority_state()
                injector = FaultInjector(point)

                with self.assertRaisesRegex(InjectedIOFault, point.value):
                    _exercise_candidate_writer(fixture, injector, b"replacement")

                after = fixture.capture_authority_state()
                fixture.assert_failed_preserving_authority(before, after)
                self.assertNotIn("writer-receipt", after.write_receipts)
                expected_checkpoints = {
                    FaultPoint.WRITER_TEMP_WRITE: (FaultPoint.WRITER_TEMP_WRITE,),
                    FaultPoint.WRITER_FSYNC: (
                        FaultPoint.WRITER_TEMP_WRITE,
                        FaultPoint.WRITER_FSYNC,
                    ),
                    FaultPoint.WRITER_REPLACE: (
                        FaultPoint.WRITER_TEMP_WRITE,
                        FaultPoint.WRITER_FSYNC,
                        FaultPoint.WRITER_REPLACE,
                    ),
                }
                self.assertEqual(injector.seen, expected_checkpoints[point])
                self.assertFalse(fixture.writer_temp.exists())

    def test_resource_commit_fault_records_attempt_but_no_commit_or_receipt(self) -> None:
        with ParserIOFaultFixture() as fixture:
            before = fixture.capture_authority_state()
            injector = FaultInjector(FaultPoint.RESOURCE_COMMIT)
            port = FaultingResourceCommitPort(
                fixture.resource_target,
                fixture.receipts,
                injector,
            )

            with self.assertRaisesRegex(InjectedIOFault, FaultPoint.RESOURCE_COMMIT.value):
                port.commit(b"replacement-resource")

            after = fixture.capture_authority_state()
            fixture.assert_failed_preserving_authority(
                before,
                after,
                expected_resource_attempt_delta=1,
            )
            self.assertEqual(after.resource_commit_successes, 0)
            self.assertEqual(after.resource_receipts, ())

    def test_success_controls_prove_receipt_and_commit_observation_is_live(self) -> None:
        with ParserIOFaultFixture() as fixture:
            before = fixture.capture_authority_state()
            _exercise_candidate_writer(fixture, FaultInjector(), b"replacement")
            writer_after = fixture.capture_authority_state()

            self.assertEqual(writer_after.target_bytes, b"replacement")
            self.assertEqual(writer_after.write_receipts, ("writer-receipt",))
            self.assertNotEqual(writer_after, before)

            port = FaultingResourceCommitPort(
                fixture.resource_target,
                fixture.receipts,
                FaultInjector(),
            )
            receipt = port.commit(b"replacement-resource")
            resource_after = fixture.capture_authority_state()

            self.assertEqual(receipt, "resource-receipt-1")
            self.assertEqual(resource_after.resource_bytes, b"replacement-resource")
            self.assertEqual(resource_after.resource_commit_attempts, 1)
            self.assertEqual(resource_after.resource_commit_successes, 1)
            self.assertEqual(resource_after.resource_receipts, (receipt,))

    def test_atomicity_assertion_rejects_byte_or_receipt_drift(self) -> None:
        with ParserIOFaultFixture() as fixture:
            baseline = fixture.capture_authority_state()
            drift_cases = (
                ("target bytes changed", replace(baseline, target_bytes=b"drift"), 0),
                ("resource bytes changed", replace(baseline, resource_bytes=b"drift"), 0),
                (
                    "write receipt changed",
                    replace(baseline, write_receipts=("unauthorized",)),
                    0,
                ),
                (
                    "resource receipt changed",
                    replace(baseline, resource_receipts=("unauthorized",)),
                    0,
                ),
                (
                    "resource commit completed",
                    replace(baseline, resource_commit_successes=1),
                    0,
                ),
                (
                    "unexpected resource commit attempt count",
                    replace(baseline, resource_commit_attempts=1),
                    0,
                ),
                (
                    "unexpected resource commit attempt count",
                    baseline,
                    1,
                ),
            )
            for message, drifted, expected_attempt_delta in drift_cases:
                with self.subTest(message=message), self.assertRaisesRegex(
                    AssertionError,
                    message,
                ):
                    fixture.assert_failed_preserving_authority(
                        baseline,
                        drifted,
                        expected_resource_attempt_delta=expected_attempt_delta,
                    )


if __name__ == "__main__":
    unittest.main()
