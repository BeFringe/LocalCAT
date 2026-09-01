from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from platform_fs import compose_platform_file_backend
from platform_fs_contracts import (
    BoundDirectoryAuthority,
    EntrySnapshot,
    LockPolicy,
    LockWait,
    PlatformFileError,
    PlatformFileErrorCode,
)
from platform_fs_windows import WindowsPlatformAdapter
from parser_contracts import SourceReference
from resource_importer import import_tmx
from tm_contracts import (
    CanonicalResourceIdentity,
    MigrationReport,
    SourceBindingState,
)
from tm_engine import TMEngine
from tm_migration import TMMigrationService
from tm_sqlite_store import ResourceStoreCoordinator
from tmx_artifact_save import TmxDirectArtifactSaver
from tmx_bound_artifact_save import _lock_name, _lock_payload
from tmx_context_contracts import (
    TmxContextError,
    TmxEffectiveLocales,
    TmxExportUnit,
    TmxScopeBinding,
    TmxScopeKind,
)
from tmx_context_interchange import (
    ParserTmxColdValidator,
    inspect_tmx_payload,
    prepare_tmx_payload,
)


def _binding_and_payload():
    binding = TmxScopeBinding(
        TmxScopeKind.MANAGED_RESOURCE,
        "windows-resource",
        hashlib.sha256(b"windows-resource-generation").hexdigest(),
        1,
        attached_count=1,
    )
    payload = prepare_tmx_payload(
        binding,
        TmxEffectiveLocales("en-US", "zh-CN"),
        (TmxExportUnit("record-1", "source", "译文", True),),
    )
    return binding, payload


def _crash_direct_publish(destination_text: str, phase: str) -> None:
    destination = Path(destination_text)
    binding, payload = _binding_and_payload()

    def crash(current: str) -> None:
        if current == phase:
            os._exit(91)

    saver = TmxDirectArtifactSaver(
        ParserTmxColdValidator(),
        lambda current: current == binding,
        fault_hook=crash,
    )
    _preview, plan = saver.preview(binding, payload, destination)
    saver.apply(plan)


def _recover_direct_publish(destination_text: str, outcomes) -> None:
    destination = Path(destination_text)
    binding, _payload = _binding_and_payload()
    try:
        receipt = TmxDirectArtifactSaver(
            ParserTmxColdValidator(),
            lambda current: current == binding,
        ).recover(destination)
        if receipt is None:
            outcomes.put(("none", None))
        else:
            reopened = inspect_tmx_payload(destination)
            outcomes.put(("receipt", receipt.after_digest, reopened.payload_digest))
    except BaseException as error:
        outcomes.put(("error", type(error).__name__, getattr(error, "code", None)))
        raise


def _hold_destination_lock(
    root_text: str,
    destination_name: str,
    ready,
    release,
) -> None:
    root = Path(root_text)
    backend = compose_platform_file_backend(root)
    parent = backend.bind_root(root)
    lease = backend.acquire(
        parent,
        _lock_name(destination_name),
        _lock_payload(destination_name),
        LockPolicy(LockWait.BLOCK),
    )
    ready.set()
    release.wait(30)
    os._exit(92)


def _publish_direct_process(destination_text: str, outcomes) -> None:
    destination = Path(destination_text)
    binding, payload = _binding_and_payload()
    saver = TmxDirectArtifactSaver(
        ParserTmxColdValidator(),
        lambda current: current == binding,
    )
    _preview, plan = saver.preview(binding, payload, destination)
    receipt = saver.apply(plan)
    outcomes.put((receipt.after_digest, inspect_tmx_payload(destination).payload_digest))


def _query_imported_canonical(
    target_text: str,
    resource_id: str,
    outcomes,
) -> None:
    try:
        engine = TMEngine(
            target_text,
            update=False,
            expected_resource_id=resource_id,
        )
        store = engine.canonical_store
        if store is None:
            outcomes.put(("legacy",))
            return
        outcomes.put(
            (
                "canonical",
                store.canonical_revision().record_count,
                tuple(record.target_raw for record in store.exact_records("Alpha")),
                tuple(record.target_raw for record in store.exact_records("Beta")),
                store.source_binding_monitor.observe().state.value,
            )
        )
    except BaseException as error:
        outcomes.put(("error", type(error).__name__, str(error)))
        raise


def _activate_canonical_identity(root: Path) -> CanonicalResourceIdentity:
    target = (root / "tm.jsonl").resolve()
    target.write_bytes(
        b'{"source":"seed","target":"initial"}\n'
        b'{"source":"other","target":"value"}\n'
    )
    identity = CanonicalResourceIdentity.from_configured_jsonl(
        "tm.windows.tmx",
        target,
    )
    coordinator = ResourceStoreCoordinator(
        canonical_store_id="store.windows.tmx",
        resource_identity=identity,
    )
    outcome = TMMigrationService(
        resource_identity=identity,
        canonical_store_id="store.windows.tmx",
        coordinator=coordinator,
    ).activate_initial(target, identity.resource_id)
    if type(outcome) is not MigrationReport:
        raise AssertionError(f"canonical activation failed: {outcome!r}")
    return identity


def _activate_and_import_canonical(
    root_text: str,
    source_text: str,
    outcomes,
) -> None:
    try:
        root = Path(root_text)
        identity = _activate_canonical_identity(root)
        configured_before = identity.configured_jsonl_path.read_bytes()
        report = import_tmx(
            Path(source_text),
            identity.configured_jsonl_path,
            " EN_us ",
            "zh_CN",
            expected_resource_id=identity.resource_id,
        )
        outcomes.put(
            (
                "report",
                report.imported,
                report.skipped,
                report.overwritten,
                report.errors,
                identity.configured_jsonl_path.read_bytes() == configured_before,
            )
        )
    except BaseException as error:
        outcomes.put(("error", type(error).__name__, str(error)))
        raise


def _import_existing_canonical(
    target_text: str,
    source_text: str,
    resource_id: str,
    outcomes,
) -> None:
    try:
        report = import_tmx(
            Path(source_text),
            Path(target_text),
            "en-US",
            "zh-CN",
            expected_resource_id=resource_id,
        )
        outcomes.put(
            (
                "report",
                report.imported,
                report.skipped,
                report.overwritten,
                report.errors,
            )
        )
    except BaseException as error:
        outcomes.put(("error", type(error).__name__, str(error)))
        raise


def _remove_activation_quarantine(root: Path) -> None:
    quarantine_root = root / ".localcat-activation-quarantine-v1"
    if not quarantine_root.exists():
        return
    for attempt_directory in quarantine_root.iterdir():
        for path in attempt_directory.iterdir():
            os.unlink("\\\\?\\" + str(path))
        os.rmdir("\\\\?\\" + str(attempt_directory))
    os.rmdir("\\\\?\\" + str(quarantine_root))


@unittest.skipUnless(sys.platform == "win32", "Windows owner compatibility matrix")
class TmxWindowsCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _activate_canonical_target(root: Path) -> CanonicalResourceIdentity:
        return _activate_canonical_identity(root)

    @staticmethod
    def _canonical_target_bytes(
        identity: CanonicalResourceIdentity,
    ) -> tuple[bytes, bytes, bytes]:
        return (
            identity.configured_jsonl_path.read_bytes(),
            identity.canonical_sidecar_path.read_bytes(),
            identity.snapshot_manifest_path.read_bytes(),
        )

    @staticmethod
    def _fresh_import_facts(
        context,
        identity: CanonicalResourceIdentity,
    ):
        outcomes = context.Queue()
        process = context.Process(
            target=_query_imported_canonical,
            args=(
                str(identity.configured_jsonl_path),
                identity.resource_id,
                outcomes,
            ),
        )
        process.start()
        process.join(timeout=60)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.exitcode != 0:
            raise AssertionError(f"fresh canonical query exited {process.exitcode}")
        outcome = outcomes.get(timeout=5)
        outcomes.close()
        outcomes.join_thread()
        return outcome

    @staticmethod
    def _run_process(context, target, args):
        outcomes = context.Queue()
        process = context.Process(target=target, args=(*args, outcomes))
        process.start()
        process.join(timeout=60)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.exitcode != 0:
            raise AssertionError(f"TMX worker exited {process.exitcode}")
        outcome = outcomes.get(timeout=5)
        outcomes.close()
        outcomes.join_thread()
        return outcome

    @staticmethod
    def _recover_in_fresh_process(context, destination: Path):
        outcomes = context.Queue()
        process = context.Process(
            target=_recover_direct_publish,
            args=(str(destination), outcomes),
        )
        process.start()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if process.exitcode != 0:
            raise AssertionError(f"fresh recovery process exited {process.exitcode}")
        outcome = outcomes.get(timeout=5)
        outcomes.close()
        outcomes.join_thread()
        return outcome

    @staticmethod
    def _write_tmx(path: Path) -> None:
        path.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tmx version="1.4"><header srclang="en-US"/><body>'
            '<tu><tuv xml:lang="en-US"><seg>Hello</seg></tuv>'
            '<tuv xml:lang="zh-CN"><seg>你好</seg></tuv></tu>'
            '</body></tmx>\n',
            encoding="utf-8",
            newline="\n",
        )

    def test_parser_sealed_tmx_import_accepts_valid_and_rejects_hardlink_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "valid.tmx"
            self._write_tmx(source)
            target = root / "tm.jsonl"
            target.write_bytes(b'{"source":"Keep","target":"stable"}\n')

            report = import_tmx(source, target, "en-US", "zh-CN")
            self.assertEqual(report.imported, 1)
            records = tuple(json.loads(line) for line in target.read_text("utf-8").splitlines())
            self.assertEqual({item["source"] for item in records}, {"Keep", "Hello"})

            hardlink = root / "hardlink.tmx"
            os.link(source, hardlink)
            before = target.read_bytes()
            rejected = import_tmx(hardlink, target, "en-US", "zh-CN")
            self.assertTrue(rejected.errors)
            self.assertEqual(target.read_bytes(), before)

    def test_parser_sealed_tmx_import_rejects_junction_ancestor_without_body_or_target_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            container = Path(raw).resolve()
            outside = container / "Outside"
            outside.mkdir()
            source = outside / "secret.tmx"
            self._write_tmx(source)
            junction = container / "Junction"
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            target = container / "tm.jsonl"
            target.write_bytes(b'{"source":"Keep","target":"stable"}\n')
            before = target.read_bytes()
            try:
                report = import_tmx(junction / "secret.tmx", target, "en-US", "zh-CN")
                self.assertTrue(report.errors)
                self.assertNotIn("Hello", " ".join(report.errors))
                self.assertEqual(target.read_bytes(), before)
            finally:
                os.rmdir(junction)

    def test_active_canonical_import_preserves_counts_conflicts_and_fresh_query(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.addCleanup(_remove_activation_quarantine, root)
        identity = CanonicalResourceIdentity.from_configured_jsonl(
            "tm.windows.tmx",
            (root / "tm.jsonl").resolve(),
        )
        source = root / "batch.tmx"
        source.write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<tmx version="1.4"><header srclang="en-US"/><body>'
            '<tu><tuv xml:lang="en_US"><seg>Alpha</seg></tuv>'
            '<tuv xml:lang="zh-CN"><seg>甲</seg></tuv></tu>'
            '<tu><tuv xml:lang="en-GB"><seg>Beta</seg></tuv>'
            '<tuv xml:lang="zh-Hans"><seg>乙</seg></tuv></tu>'
            '<tu><tuv xml:lang="en-US"><seg>Alpha</seg></tuv>'
            '<tuv xml:lang="zh_CN"><seg>甲二</seg></tuv></tu>'
            '</body></tmx>\n',
            encoding="utf-8",
            newline="\n",
        )

        context = multiprocessing.get_context("spawn")
        imported = self._run_process(
            context,
            _activate_and_import_canonical,
            (str(root), str(source)),
        )

        self.assertEqual(imported, ("report", 3, 0, 0, (), True))
        with closing(sqlite3.connect(identity.canonical_sidecar_path)) as connection:
            origin = connection.execute(
                "SELECT status, valid_count, invalid_count, "
                "duplicate_source_count, source_digest, source_path "
                "FROM tm_origin_batch WHERE kind = 'import'"
            ).fetchone()
        self.assertEqual(
            origin,
            (
                "completed",
                3,
                0,
                1,
                hashlib.sha256(source.read_bytes()).hexdigest(),
                str(source),
            ),
        )
        with closing(sqlite3.connect(identity.canonical_sidecar_path)) as connection:
            count_after_first = connection.execute(
                "SELECT COUNT(*) FROM tm_record"
            ).fetchone()[0]
        repeated = self._run_process(
            context,
            _import_existing_canonical,
            (
                str(identity.configured_jsonl_path),
                str(source),
                identity.resource_id,
            ),
        )
        self.assertEqual(repeated[0:4], ("report", 0, 0, 0))
        self.assertTrue(any("already applied" in error for error in repeated[4]))
        with closing(sqlite3.connect(identity.canonical_sidecar_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM tm_record").fetchone()[0],
                count_after_first,
            )

        guarded_before = self._canonical_target_bytes(identity)
        wrong_identity = import_tmx(
            source,
            identity.configured_jsonl_path,
            "en-US",
            "zh-CN",
            expected_resource_id="tm.windows.wrong",
        )
        self.assertEqual(wrong_identity.imported, 0)
        self.assertEqual(wrong_identity.skipped, 0)
        self.assertEqual(wrong_identity.overwritten, 0)
        self.assertEqual(len(wrong_identity.errors), 1)
        self.assertTrue(wrong_identity.errors[0].startswith("TM.CANONICAL_"))
        self.assertNotIn(str(source), wrong_identity.errors[0])
        self.assertNotIn("Alpha", wrong_identity.errors[0])
        self.assertEqual(self._canonical_target_bytes(identity), guarded_before)

        facts = self._fresh_import_facts(context, identity)
        self.assertEqual(
            facts,
            (
                "canonical",
                5,
                ("甲二", "甲"),
                ("乙",),
                SourceBindingState.VERIFIED_HISTORY.value,
            ),
        )

    def test_active_canonical_import_rejects_untrusted_sources_without_target_writes(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.addCleanup(_remove_activation_quarantine, root)
        identity = self._activate_canonical_target(root)
        baseline = self._canonical_target_bytes(identity)

        invalid = root / "invalid.tmx"
        invalid.write_bytes(b'<!DOCTYPE tmx SYSTEM "unsafe"><tmx><body/></tmx>')
        invalid_report = import_tmx(
            invalid,
            identity.configured_jsonl_path,
            "en-US",
            "zh-CN",
            expected_resource_id=identity.resource_id,
        )
        self.assertTrue(invalid_report.errors)
        self.assertEqual(self._canonical_target_bytes(identity), baseline)

        selected = root / "selected.tmx"
        self._write_tmx(selected)
        authorized = root / "authorized"
        authorized.mkdir()
        escaped_reference = SourceReference(
            safe_root=str(authorized),
            selected_path=str(selected),
            display_hint="selected resource",
        )
        with mock.patch(
            "resource_importer._source_reference",
            return_value=escaped_reference,
        ):
            escaped_report = import_tmx(
                selected,
                identity.configured_jsonl_path,
                "en-US",
                "zh-CN",
                expected_resource_id=identity.resource_id,
            )
        self.assertTrue(
            any("OUTSIDE_ROOT" in error for error in escaped_report.errors),
            escaped_report.errors,
        )
        self.assertEqual(self._canonical_target_bytes(identity), baseline)

        outside = root / "Outside"
        outside.mkdir()
        reparse_source = outside / "reparse.tmx"
        self._write_tmx(reparse_source)
        junction = root / "Junction"
        completed = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        try:
            reparse_report = import_tmx(
                junction / reparse_source.name,
                identity.configured_jsonl_path,
                "en-US",
                "zh-CN",
                expected_resource_id=identity.resource_id,
            )
            self.assertTrue(reparse_report.errors)
            self.assertEqual(self._canonical_target_bytes(identity), baseline)
        finally:
            os.rmdir(junction)

        swap_source = root / "swap.tmx"
        self._write_tmx(swap_source)
        swapped = False

        def rewrite_after_probe(phase: str) -> None:
            nonlocal swapped
            if phase == "windows_after_entry_probe" and not swapped:
                swap_source.write_bytes(b"<tmx><body></body></tmx>")
                swapped = True

        adapter = WindowsPlatformAdapter(_fault_injector=rewrite_after_probe)
        with mock.patch(
            "parser_composition._compose_platform_file_backend",
            return_value=adapter,
        ):
            swap_report = import_tmx(
                swap_source,
                identity.configured_jsonl_path,
                "en-US",
                "zh-CN",
                expected_resource_id=identity.resource_id,
            )
        self.assertTrue(swapped)
        self.assertTrue(
            any("PARSER.SOURCE.STALE" in error for error in swap_report.errors),
            swap_report.errors,
        )
        self.assertEqual(self._canonical_target_bytes(identity), baseline)

        facts = self._fresh_import_facts(multiprocessing.get_context("spawn"), identity)
        self.assertEqual(facts[0:2], ("canonical", 2))

    def test_bound_direct_export_waits_for_destination_family_lock_and_cold_reopens(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            destination = root / "locked.tmx"
            binding, payload = _binding_and_payload()
            saver = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda current: current == binding,
            )
            _preview, plan = saver.preview(binding, payload, destination)
            backend = compose_platform_file_backend(root)
            parent = backend.bind_root(root)
            lease = backend.acquire(
                parent,
                _lock_name(destination.name),
                _lock_payload(destination.name),
                LockPolicy(LockWait.BLOCK),
            )

            def release() -> None:
                time.sleep(0.2)
                lease.close()
                parent.close()

            releaser = threading.Thread(target=release)
            releaser.start()
            started = time.monotonic()
            receipt = saver.apply(plan)
            elapsed = time.monotonic() - started
            releaser.join(timeout=5)
            self.assertFalse(releaser.is_alive())
            self.assertGreaterEqual(elapsed, 0.1)
            self.assertEqual(receipt.after_digest, payload.proof.payload_digest)
            self.assertEqual(inspect_tmx_payload(destination).payload_digest, receipt.after_digest)

    def test_process_phase_exits_are_classified_by_independent_fresh_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            context = multiprocessing.get_context("spawn")
            binding, payload = _binding_and_payload()
            prior = b"prior exact bytes"
            phases = (
                ("journal_armed", False),
                ("stage_validated", False),
                ("lkg_ready", False),
                ("before_target_publish", False),
                ("after_target_publish", True),
                ("owner_commit", True),
                ("terminal_reproof", True),
            )
            for phase, after_expected in phases:
                with self.subTest(phase=phase):
                    destination = root / f"process-{phase}.tmx"
                    destination.write_bytes(prior)
                    process = context.Process(
                        target=_crash_direct_publish,
                        args=(str(destination), phase),
                    )
                    process.start()
                    process.join(timeout=30)
                    self.assertEqual(process.exitcode, 91)

                    outcome = self._recover_in_fresh_process(context, destination)
                    if after_expected:
                        self.assertEqual(
                            outcome,
                            (
                                "receipt",
                                payload.proof.payload_digest,
                                payload.proof.payload_digest,
                            ),
                        )
                        self.assertEqual(destination.read_bytes(), payload.data)
                    else:
                        self.assertEqual(outcome, ("none", None))
                        self.assertEqual(destination.read_bytes(), prior)
                    self.assertFalse(
                        any(path.name.endswith(".journal") for path in root.iterdir())
                    )

    def test_real_replacement_is_denied_while_root_authority_is_live(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            container = Path(raw).resolve()
            root = container / "Root"
            root.mkdir()
            destination = root / "replace-window.tmx"
            destination.write_bytes(b"preview prior")
            outside = container / "Outside"
            outside.mkdir()
            foreign = outside / "foreign.tmx"
            foreign.write_bytes(b"foreign replacement")
            binding, payload = _binding_and_payload()

            replacement_denied = False

            def replace_after_release(phase: str) -> None:
                nonlocal replacement_denied
                if phase == "prior_released":
                    try:
                        os.replace(foreign, destination)
                    except PermissionError:
                        replacement_denied = True

            saver = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda current: current == binding,
                fault_hook=replace_after_release,
            )
            _preview, plan = saver.preview(binding, payload, destination)
            receipt = saver.apply(plan)
            self.assertTrue(replacement_denied)
            self.assertEqual(destination.read_bytes(), payload.data)
            self.assertEqual(receipt.after_digest, payload.proof.payload_digest)
            self.assertEqual(foreign.read_bytes(), b"foreign replacement")
            self.assertFalse(any(path.name.endswith(".journal") for path in root.iterdir()))

    def test_changed_namespace_fact_after_prior_release_is_stale_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            destination = root / "namespace-drift.tmx"
            prior = b"preview prior"
            destination.write_bytes(prior)
            binding, payload = _binding_and_payload()
            released = False
            original_inspect = BoundDirectoryAuthority.inspect_entry

            def mark_release(phase: str) -> None:
                nonlocal released
                if phase == "prior_released":
                    released = True

            def drifted_inspect(authority, name: str) -> EntrySnapshot | None:
                observed = original_inspect(authority, name)
                if released and name == destination.name and observed is not None:
                    return replace(observed, byte_count=observed.byte_count + 1)
                return observed

            saver = TmxDirectArtifactSaver(
                ParserTmxColdValidator(),
                lambda current: current == binding,
                fault_hook=mark_release,
            )
            with mock.patch.object(
                BoundDirectoryAuthority,
                "inspect_entry",
                new=drifted_inspect,
            ):
                _preview, plan = saver.preview(binding, payload, destination)
                with self.assertRaises(TmxContextError) as caught:
                    saver.apply(plan)
            self.assertEqual(caught.exception.code, "TMX.DESTINATION_STALE")
            self.assertEqual(destination.read_bytes(), prior)
            self.assertFalse(any(path.name.endswith(".journal") for path in root.iterdir()))

    def test_process_lock_waiter_proceeds_after_owner_process_exit(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            destination = root / "process-lock.tmx"
            context = multiprocessing.get_context("spawn")
            ready = context.Event()
            release = context.Event()
            holder = context.Process(
                target=_hold_destination_lock,
                args=(str(root), destination.name, ready, release),
            )
            holder.start()
            self.assertTrue(ready.wait(15))

            outcomes = context.Queue()
            waiter = context.Process(
                target=_publish_direct_process,
                args=(str(destination), outcomes),
            )
            waiter.start()
            time.sleep(0.25)
            self.assertTrue(waiter.is_alive())
            release.set()
            holder.join(timeout=15)
            self.assertEqual(holder.exitcode, 92)
            waiter.join(timeout=30)
            self.assertEqual(waiter.exitcode, 0)
            expected_digest = _binding_and_payload()[1].proof.payload_digest
            self.assertEqual(
                outcomes.get(timeout=5),
                (expected_digest, expected_digest),
            )
            outcomes.close()
            outcomes.join_thread()

    def test_uncertain_publication_phases_retain_recoverable_journal(self) -> None:
        cases = (
            (2, False, False),
            (3, True, False),
            (4, True, False),
            (5, True, True),
            (6, True, True),
        )
        for fail_at, existing, after_expected in cases:
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve()
                destination = root / "uncertain-sidecar.tmx"
                prior = b"prior exact bytes"
                if existing:
                    destination.write_bytes(prior)
                publish_count = 0

                def inject(phase: str) -> None:
                    nonlocal publish_count
                    if phase != "publish_after_rename":
                        return
                    publish_count += 1
                    if publish_count == fail_at:
                        raise PlatformFileError(
                            PlatformFileErrorCode.RECOVERY_REQUIRED,
                            retryable=True,
                        )

                binding, payload = _binding_and_payload()
                saver = TmxDirectArtifactSaver(
                    ParserTmxColdValidator(),
                    lambda current: current == binding,
                    backend=WindowsPlatformAdapter(_fault_injector=inject),
                )
                _preview, plan = saver.preview(binding, payload, destination)
                with self.assertRaises(TmxContextError) as caught:
                    saver.apply(plan)
                self.assertEqual(caught.exception.code, "TMX.RECOVERY_REQUIRED")
                self.assertTrue(any(path.name.endswith(".journal") for path in root.iterdir()))

                recovered = TmxDirectArtifactSaver(
                    ParserTmxColdValidator(),
                    lambda current: current == binding,
                ).recover(destination)
                if after_expected:
                    self.assertIsNotNone(recovered)
                    assert recovered is not None
                    self.assertEqual(recovered.after_digest, payload.proof.payload_digest)
                    self.assertEqual(destination.read_bytes(), payload.data)
                elif existing:
                    self.assertIsNone(recovered)
                    self.assertEqual(destination.read_bytes(), prior)
                else:
                    self.assertIsNone(recovered)
                    self.assertFalse(destination.exists())
                self.assertFalse(any(path.name.endswith(".journal") for path in root.iterdir()))

    def test_junction_destination_parent_is_rejected_without_outside_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            container = Path(raw).resolve()
            outside = container / "Outside"
            outside.mkdir()
            destination = outside / "export.tmx"
            destination.write_bytes(b"outside prior")
            junction = container / "Junction"
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            binding, payload = _binding_and_payload()
            try:
                saver = TmxDirectArtifactSaver(
                    ParserTmxColdValidator(),
                    lambda current: current == binding,
                )
                with self.assertRaises(TmxContextError):
                    saver.preview(binding, payload, junction / destination.name)
                self.assertEqual(destination.read_bytes(), b"outside prior")
            finally:
                os.rmdir(junction)


if __name__ == "__main__":
    unittest.main()
