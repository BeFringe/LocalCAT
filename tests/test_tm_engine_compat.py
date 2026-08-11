"""Task 6.2 LegacyTMFacade (TMEngine) compatibility and authority tests.

The facade is the single legacy/canonical decision point: a never
activated resource keeps the exact JSONL last-write-wins behavior, an
activated resource serves exact/save only from the canonical store under
one generation lease, cold recovery rehydrates from durable authority,
marker/pair ambiguity or tampering fail-stops (never JSONL fallback),
and a provably cancelled first activation may continue on JSONL.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from typing import cast
from unittest.mock import patch

from tm_activation_journal import _activation_terminal_path
from tm_contracts import SourceBindingState
from tm_engine import TMEngine, SourceUnit, open_canonical_tm_store
from tm_sqlite_store import ResourceStoreCoordinator
from tests.test_tm_activation_journal import (
    SOURCE_BYTES,
    _candidate,
    _first_prepared,
    _identity,
)


def _activate_resource(
    root: Path,
    *,
    fts5_available: bool = True,
) -> Path:
    """Fully activate one first-generation canonical resource and return
    its configured JSONL path (the bytes stay equal to ``SOURCE_BYTES``).
    """

    identity, coordinator, _sealed, prepared, journal = _first_prepared(
        root,
        fts5_available=fts5_available,
    )
    with patch("tm_sqlite_store._probe_fts5", return_value=fts5_available):
        coordinator.publish_activation(prepared, journal)
    return identity.configured_jsonl_path


def _unit(source: str, target: str) -> SourceUnit:
    return SourceUnit(id="1", text=source, speaker=None)


def _query(engine: TMEngine, source: str):
    """Narrowed exact query helper for type-checked tests."""

    match = engine.query_exact(source)
    assert match is not None, f"expected exact match for {source!r}"
    return match


class NeverActivatedLegacyTests(unittest.TestCase):
    def test_never_activated_legacy_last_write_wins_query_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = root / "legacy.jsonl"
            tm_path.write_text(
                '{"source":"Hello","target":"你好"}\n'
                '{"source":"Hello","target":"您好"}\n',
                encoding="utf-8",
            )
            engine = TMEngine(str(tm_path))

            self.assertFalse(engine.canonical_active)
            match = _query(engine, "Hello")
            self.assertEqual(match.target, "您好")
            self.assertEqual(match.match_type, "EXACT")
            self.assertEqual(match.similarity, 1.0)
            self.assertEqual(match.tm_source, tm_path.name)
            self.assertIsNone(engine.query_exact("missing"))
            self.assertTrue(engine.save_record(_unit("World", ""), "世界"))
            lines = tm_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("世界", lines[-1])
            reloaded = TMEngine(str(tm_path))
            self.assertEqual(_query(reloaded, "World").target, "世界")

    def test_legacy_active_lookup_update_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = root / "legacy.jsonl"
            tm_path.write_text(
                '{"source":"Hello","target":"你好"}\n',
                encoding="utf-8",
            )
            no_lookup = TMEngine(
                str(tm_path),
                active=True,
                lookup=False,
                update=True,
            )

            self.assertIsNone(no_lookup.query_exact("Hello"))
            self.assertTrue(no_lookup.save_record(_unit("A", ""), "a"))
            # Legacy engines snapshot the JSONL at construction: the
            # readers below must be built after the write to observe it.
            no_update = TMEngine(
                str(tm_path),
                active=True,
                lookup=True,
                update=False,
            )
            inactive = TMEngine(
                str(tm_path),
                active=False,
                lookup=True,
                update=True,
            )

            self.assertEqual(_query(no_update, "A").target, "a")
            self.assertFalse(no_update.save_record(_unit("B", ""), "b"))
            self.assertIsNone(inactive.query_exact("A"))
            self.assertFalse(inactive.save_record(_unit("C", ""), "c"))
            self.assertEqual(tm_path.read_text(encoding="utf-8").count("\n"), 2)


class ActivatedCanonicalTests(unittest.TestCase):
    def test_activated_facade_exact_and_save_use_canonical_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            original = tm_path.read_bytes()

            engine = TMEngine(str(tm_path))
            self.assertTrue(engine.canonical_active)
            self.assertEqual(tm_path.read_bytes(), original)
            match = _query(engine, "same")
            self.assertEqual(match.target, "winner")
            self.assertEqual(match.match_type, "EXACT")
            self.assertEqual(match.similarity, 1.0)
            self.assertEqual(_query(engine, "other").target, "value")
            self.assertIsNone(engine.query_exact("missing"))

            self.assertTrue(engine.save_record(_unit("same", ""), "confirmed"))
            self.assertEqual(_query(engine, "same").target, "confirmed")
            self.assertEqual(tm_path.read_bytes(), original)

    def test_cold_recovery_rehydrates_one_generation_from_durable_authority(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)

            store = open_canonical_tm_store(tm_path)
            assert store is not None
            revision = store.canonical_revision()
            self.assertEqual(revision.generation, 0)
            self.assertEqual(revision.record_count, 3)
            store2 = open_canonical_tm_store(tm_path)
            assert store2 is not None
            self.assertEqual(store2.canonical_revision().generation, 0)

    def test_fresh_reopen_after_canonical_save_reports_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            original = tm_path.read_bytes()

            engine = TMEngine(str(tm_path))
            self.assertTrue(engine.canonical_active)
            self.assertTrue(engine.save_record(_unit("same", ""), "confirmed"))
            self.assertEqual(_query(engine, "same").target, "confirmed")

            # A canonical write advances the head past the manifest-bound
            # snapshot but never touches the configured JSONL: a fresh
            # process must rehydrate the same canonical lineage and the
            # monitor derives VERIFIED_HISTORY, never a JSONL fallback.
            reloaded = TMEngine(str(tm_path))
            self.assertTrue(reloaded.canonical_active)
            reloaded_store = reloaded._store
            assert reloaded_store is not None
            self.assertEqual(
                reloaded_store.source_binding_monitor.observe().state,
                SourceBindingState.VERIFIED_HISTORY,
            )
            self.assertEqual(_query(reloaded, "same").target, "confirmed")
            self.assertTrue(
                reloaded.save_record(_unit("same", ""), "confirmed-2")
            )
            self.assertEqual(_query(reloaded, "same").target, "confirmed-2")
            self.assertEqual(tm_path.read_bytes(), original)

    def test_activated_active_lookup_update_gates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            no_lookup = TMEngine(
                str(tm_path),
                active=True,
                lookup=False,
                update=True,
            )
            no_update = TMEngine(
                str(tm_path),
                active=True,
                lookup=True,
                update=False,
            )
            inactive = TMEngine(
                str(tm_path),
                active=False,
                lookup=True,
                update=True,
            )

            self.assertIsNone(no_lookup.query_exact("same"))
            self.assertTrue(no_lookup.save_record(_unit("flag", ""), "saved"))
            self.assertEqual(_query(no_update, "flag").target, "saved")
            self.assertFalse(no_update.save_record(_unit("flag", ""), "blocked"))
            self.assertIsNone(inactive.query_exact("flag"))
            self.assertFalse(inactive.save_record(_unit("flag", ""), "blocked"))
            self.assertEqual(_query(no_update, "flag").target, "saved")

    def test_active_lookup_update_gates_require_exact_bool(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            tm_path = Path(temporary) / "legacy.jsonl"
            tm_path.write_text("", encoding="utf-8")
            for field_name in ("active", "lookup", "update"):
                options = {"active": True, "lookup": True, "update": True}
                options[field_name] = cast(bool, cast(object, 1))
                with self.subTest(field_name=field_name):
                    with self.assertRaises(TypeError):
                        TMEngine(str(tm_path), **options)


class SourceDivergedTests(unittest.TestCase):
    def test_diverged_canonical_query_and_save_continue_and_keep_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)

            engine = TMEngine(str(tm_path))
            self.assertTrue(engine.canonical_active)
            store = engine._store
            assert store is not None
            monitor = store.source_binding_monitor
            self.assertEqual(
                monitor.observe().state,
                SourceBindingState.VERIFIED_CURRENT,
            )
            external = '{"source":"external","target":"外部"}\n'
            tm_path.write_text(external, encoding="utf-8")

            observation = monitor.observe()
            self.assertEqual(
                observation.state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertEqual(_query(engine, "same").target, "winner")
            self.assertTrue(
                engine.save_record(_unit("same", ""), "diverged-save")
            )
            self.assertEqual(
                _query(engine, "same").target,
                "diverged-save",
            )
            self.assertEqual(tm_path.read_text(encoding="utf-8"), external)

            observed_after = monitor.observe()
            self.assertEqual(
                observed_after.state,
                SourceBindingState.SOURCE_DIVERGED,
            )

    def test_fresh_reopen_after_divergence_latches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            original = tm_path.read_bytes()
            external = '{"source":"external","target":"外部"}\n'
            tm_path.write_text(external, encoding="utf-8")

            first = TMEngine(str(tm_path))
            first_store = first._store
            assert first_store is not None
            self.assertEqual(
                first_store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )

            # SOURCE_DIVERGED stays canonical authority across a fresh
            # process open: exact/save continue and the JSONL stays
            # untouched while divergence remains latched.
            reloaded = TMEngine(str(tm_path))
            self.assertTrue(reloaded.canonical_active)
            reloaded_store = reloaded._store
            assert reloaded_store is not None
            self.assertEqual(_query(reloaded, "same").target, "winner")
            self.assertTrue(
                reloaded.save_record(_unit("same", ""), "diverged-save")
            )
            self.assertEqual(
                _query(reloaded, "same").target,
                "diverged-save",
            )
            self.assertEqual(tm_path.read_text(encoding="utf-8"), external)
            self.assertEqual(
                reloaded_store.source_binding_monitor.observe().state,
                SourceBindingState.SOURCE_DIVERGED,
            )


class CanonicalFailStopTests(unittest.TestCase):
    def test_marker_without_pair_fail_stops_never_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            identity = _identity(root)
            identity.canonical_sidecar_path.unlink()
            identity.snapshot_manifest_path.unlink()

            with self.assertRaises(ValueError) as raised:
                TMEngine(str(tm_path))
            self.assertIn("TM.CANONICAL", str(raised.exception))

    def test_missing_manifest_reopens_diverged_never_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            _identity(root).snapshot_manifest_path.unlink()

            # The canonical DB/ledger/authority are intact; a missing
            # configured manifest is pair divergence, so a fresh process
            # reopens canonical and the monitor reports SOURCE_DIVERGED
            # instead of failing or falling back to JSONL.
            engine = TMEngine(str(tm_path))
            self.assertTrue(engine.canonical_active)
            store = engine._store
            assert store is not None
            observation = store.source_binding_monitor.observe()
            self.assertEqual(
                observation.state,
                SourceBindingState.SOURCE_DIVERGED,
            )
            self.assertIn(
                "SOURCE_BINDING.MANIFEST_MISSING",
                observation.diagnostic_codes,
            )
            self.assertEqual(_query(engine, "same").target, "winner")

    def test_corrupt_sidecar_fail_stops_never_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            _identity(root).canonical_sidecar_path.write_bytes(b"garbage")

            with self.assertRaises(ValueError) as raised:
                TMEngine(str(tm_path))
            self.assertIn("TM.CANONICAL", str(raised.exception))

    def test_same_bytes_foreign_sidecar_inode_fail_stops(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tm_path = _activate_resource(root)
            sidecar = _identity(root).canonical_sidecar_path
            same_bytes = sidecar.read_bytes()
            sidecar.unlink()
            sidecar.write_bytes(same_bytes)

            with self.assertRaises(ValueError) as raised:
                TMEngine(str(tm_path))
            self.assertIn("TM.CANONICAL", str(raised.exception))

    def test_sidecar_symlink_and_extra_hardlink_fail_stop(self) -> None:
        for mutation in ("symlink", "hardlink"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    tm_path = _activate_resource(root)
                    sidecar = _identity(root).canonical_sidecar_path
                    other = root / f"{mutation}.sqlite3"
                    if mutation == "symlink":
                        sidecar.rename(other)
                        sidecar.symlink_to(other)
                    else:
                        os.link(sidecar, other)

                    with self.assertRaises(ValueError) as raised:
                        TMEngine(str(tm_path))
                    self.assertIn("TM.CANONICAL", str(raised.exception))


class IncompleteActivationTests(unittest.TestCase):
    def test_pending_prepared_journal_recovers_via_coordinator_never_silent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, _journal = (
                _first_prepared(root, fts5_available=True)
            )
            tm_path = identity.configured_jsonl_path

            # A still-PREPARED first activation is an unclosed activation:
            # the facade routes it through the strict Task 5.8 coordinator
            # recovery, which cancels it back to the never-activated legacy
            # authority (never a silent JSONL fallback).
            engine = TMEngine(str(tm_path))
            self.assertFalse(engine.canonical_active)
            self.assertEqual(_query(engine, "same").target, "winner")
            self.assertTrue(engine.save_record(_unit("pending", ""), "仍可用"))
            self.assertEqual(_query(engine, "pending").target, "仍可用")

    def test_pending_prepared_journal_with_lost_candidate_fail_stops(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity, _coordinator, _sealed, _prepared, journal = (
                _first_prepared(root, fts5_available=True)
            )
            candidate_db = journal._record.candidate_stage_db_path
            candidate_db.unlink()

            with self.assertRaises(ValueError) as raised:
                TMEngine(str(identity.configured_jsonl_path))
            self.assertIn("TM.CANONICAL", str(raised.exception))


class CancelledFirstActivationTests(unittest.TestCase):
    def test_cancelled_first_activation_continues_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = _identity(root)
            identity.configured_jsonl_path.write_bytes(SOURCE_BYTES)
            coordinator = ResourceStoreCoordinator(
                canonical_store_id="store.primary",
                resource_identity=identity,
            )
            _stage, sealed = _candidate(
                coordinator,
                identity,
                fts5_available=True,
                expected_prior_generation=None,
            )
            with patch("tm_sqlite_store._probe_fts5", return_value=True):
                prepared = coordinator.activate(sealed)
            coordinator.cancel_prepared_activation(prepared)
            self.assertTrue(_activation_terminal_path(identity).exists())

            engine = TMEngine(str(identity.configured_jsonl_path))
            self.assertFalse(engine.canonical_active)
            self.assertEqual(_query(engine, "same").target, "winner")
            self.assertTrue(
                engine.save_record(_unit("cancelled", ""), "仍可用")
            )
            self.assertEqual(
                _query(engine, "cancelled").target,
                "仍可用",
            )


if __name__ == "__main__":
    unittest.main()
