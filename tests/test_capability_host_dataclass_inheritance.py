from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
from types import FunctionType, SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import patch

import capability_host as host
import tm_activation_journal as journal
import tm_sqlite_store as store
from tests.source_authority_support import current_source_authority


_ROOT = Path(__file__).resolve().parents[1]


def _binding() -> Any:
    authority = current_source_authority()
    with authority.proof_window():
        base = host._ModuleSourceCodeAnchor.capture(
            module_name="tm_activation_journal",
            path=_ROOT / "tm_activation_journal.py",
            source_authority=authority,
        )
        anchor = host._ModuleSourceCodeAnchor.capture(
            module_name="tm_sqlite_store",
            path=_ROOT / "tm_sqlite_store.py",
            source_authority=authority,
            base_anchors=(base,),
        )
        graphs = (
            host._RuntimeModuleCodeBinding.capture(journal, base),
            host._RuntimeModuleCodeBinding.capture(store, anchor),
        )
        return SimpleNamespace(
            anchor=anchor,
            is_current=lambda: all(graph.is_current() for graph in graphs),
        )


class CapabilityHostDataclassInheritanceTests(unittest.TestCase):
    def test_real_imported_base_fields_and_generated_methods_match_source(self) -> None:
        binding = _binding()
        shape = binding.anchor.dataclass_shape("_SQLiteStageView")
        self.assertEqual(
            tuple(field.name for field in shape.fields),
            ("stage", "canonical_store_id", "generation", "fts5_available",
             "active_content_attestation", "stage_borrow"),
        )
        self.assertTrue(host._dataclass_shape_is_current(store._SQLiteStageView, shape))
        self.assertTrue(binding.is_current())

    def test_inherited_field_mutation_is_rejected(self) -> None:
        binding = _binding()
        self.assertTrue(binding.is_current())
        field = dataclasses.fields(journal._SQLiteGenerationView)[0]
        with patch.object(field, "compare", False):
            self.assertFalse(binding.is_current())
        self.assertTrue(binding.is_current())

    def test_base_generated_method_replacement_before_capture_is_rejected(self) -> None:
        original = cast(Any, journal._SQLiteGenerationView.__eq__)

        def replacement(self: object, other: object) -> object:
            return original(self, other)

        with patch.object(journal._SQLiteGenerationView, "__eq__", replacement):
            self.assertFalse(_binding().is_current())
        self.assertTrue(_binding().is_current())

    def test_base_generated_method_default_drift_is_rejected(self) -> None:
        binding = _binding()
        self.assertTrue(binding.is_current())
        function = cast(FunctionType, journal._SQLiteGenerationView.__init__)
        with patch.object(function, "__defaults__", ("forged",)):
            self.assertFalse(binding.is_current())
        self.assertTrue(binding.is_current())

    def test_base_and_subclass_defaults_changed_before_capture_are_rejected(self) -> None:
        for value, defaults in (
            (journal._SQLiteGenerationView, ("forged",)),
            (store._SQLiteStageView, ("forged", None)),
            (store._SQLiteStageView, (None, "forged")),
        ):
            with self.subTest(value=value.__name__, defaults=defaults):
                function = cast(FunctionType, value.__init__)
                with patch.object(function, "__defaults__", defaults):
                    self.assertFalse(_binding().is_current())
                self.assertTrue(_binding().is_current())

    def test_defaults_changed_after_startup_block_first_gate_c_and_recover(self) -> None:
        from tests.test_capability_host_gate_c import (
            _composition, _gate_c_owner, _GENERATED_AT, _VALID_UNTIL, _EVALUATED_AT,
        )

        for value, defaults in (
            (journal._SQLiteGenerationView, ("forged",)),
            (store._SQLiteStageView, ("forged", None)),
            (store._SQLiteStageView, (None, "forged")),
        ):
            with self.subTest(value=value.__name__, defaults=defaults):
                composition = _composition()
                owner = _gate_c_owner(composition)
                execution = owner._RetrievalGateCValidationOwner__execution
                old = composition.host.retrieval_snapshot()
                self.assertIsNone(execution._RealGateCExecution__validation_binding)
                function = cast(FunctionType, value.__init__)
                with patch.object(function, "__defaults__", defaults):
                    returned = owner.validate_gate_c(
                        generated_at_utc=_GENERATED_AT,
                        valid_until_utc=_VALID_UNTIL,
                        evaluated_at_utc=_EVALUATED_AT,
                    )
                    self.assertIs(returned, old)
                    self.assertFalse(returned.display.context_available)
                    self.assertIsNone(execution._RealGateCExecution__validation_binding)
                restored = owner.validate_gate_c(
                    generated_at_utc=_GENERATED_AT,
                    valid_until_utc=_VALID_UNTIL,
                    evaluated_at_utc=_EVALUATED_AT,
                )
                self.assertEqual(restored.generation, old.generation + 1)
                self.assertTrue(restored.display.context_available)

    def test_inherited_field_default_changed_before_capture_is_rejected(self) -> None:
        field = next(
            field for field in dataclasses.fields(journal._SQLiteGenerationView)
            if field.name == "active_content_attestation"
        )
        with patch.object(field, "default", "forged"):
            self.assertFalse(_binding().is_current())
        self.assertTrue(_binding().is_current())

    def test_named_and_keyword_defaults_match_source_and_reject_mutation(self) -> None:
        source = b'''from dataclasses import dataclass, field
SOURCE_DEFAULT = "source-default"
@dataclass(frozen=True)
class Base:
    value: str = SOURCE_DEFAULT
@dataclass(frozen=True)
class Child(Base):
    other: tuple[str, ...] = field(default=("second",), kw_only=True)
'''
        namespace: dict[str, Any] = {}
        exec(compile(source, "<defaults-test>", "exec"), namespace)
        shapes = host._source_declarations(
            source, path=Path("<defaults-test>"), named_defaults={}
        )[-1]
        shape = next(shape for shape in shapes if shape.qualname == "Child")
        value = namespace["Child"]
        self.assertTrue(host._dataclass_shape_is_current(value, shape))
        with patch.object(value.__init__, "__kwdefaults__", {"other": ("forged",)}):
            self.assertFalse(host._dataclass_shape_is_current(value, shape))
        self.assertTrue(host._dataclass_shape_is_current(value, shape))

    def test_base_generated_method_closure_drift_is_rejected(self) -> None:
        binding = _binding()
        self.assertTrue(binding.is_current())
        function = cast(FunctionType, journal._SQLiteGenerationView.__init__)
        assert function.__closure__ is not None
        cell = function.__closure__[0]
        original = cell.cell_contents
        cell.cell_contents = type("ForeignObject", (), {})
        try:
            self.assertFalse(binding.is_current())
        finally:
            cell.cell_contents = original
        self.assertTrue(binding.is_current())

    def test_foreign_base_mro_before_and_after_capture_is_rejected(self) -> None:
        binding = _binding()
        self.assertTrue(binding.is_current())
        stage = store._SQLiteStageView
        original = stage.__bases__

        class ForeignBase:
            pass

        stage.__bases__ = (ForeignBase,)
        try:
            self.assertFalse(binding.is_current())
            self.assertFalse(_binding().is_current())
        finally:
            stage.__bases__ = original
        self.assertTrue(binding.is_current())

    def test_local_field_override_retains_inherited_order(self) -> None:
        source = b'''from dataclasses import dataclass, field
@dataclass(frozen=True)
class Base:
    first: int
    second: int
@dataclass(frozen=True)
class Child(Base):
    first: str = field(repr=False)
    third: int
'''
        namespace: dict[str, Any] = {}
        exec(compile(source, "<inheritance-test>", "exec"), namespace)
        shapes = host._source_declarations(
            source, path=Path("<inheritance-test>"), named_defaults={}
        )[-1]
        shape = next(shape for shape in shapes if shape.qualname == "Child")
        self.assertEqual(tuple(field.name for field in shape.fields), ("first", "second", "third"))
        self.assertFalse(shape.fields[0].repr)
        self.assertTrue(host._dataclass_shape_is_current(namespace["Child"], shape))

    def test_unresolved_dynamic_and_multiple_bases_are_rejected(self) -> None:
        for base in ("Unbound", "factory()", "First, Second"):
            with self.subTest(base=base), self.assertRaises(RuntimeError):
                host._source_declarations(
                    ("from dataclasses import dataclass\n@dataclass\nclass Child(" + base + "):\n    value: int\n").encode(),
                    path=Path("<inheritance-test>"), named_defaults={},
                )

    def test_base_source_replacement_after_startup_is_blocked_or_detected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for source in _ROOT.glob("*.py"):
                shutil.copy2(source, root / source.name)
            shutil.copy2(_ROOT / "benchmark_tm_contract.json", root)
            fixtures = root / "tests" / "fixtures"
            fixtures.mkdir(parents=True)
            for source in (_ROOT / "tests" / "fixtures").glob("*.json"):
                shutil.copy2(source, fixtures / source.name)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", textwrap.dedent('''
                    from datetime import datetime, timezone
                    from pathlib import Path
                    import sys
                    from capability_host import compose_capability_host
                    from platform_source_authority import compose_rooted_source_authority
                    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
                    authority = compose_rooted_source_authority(Path.cwd())
                    composition = compose_capability_host(source_authority=authority, evaluated_at_utc=now)
                    owner = composition.retrieval_gate_c_validation_owner
                    execution = owner._RetrievalGateCValidationOwner__execution
                    assert execution._RealGateCExecution__validation_binding is None
                    seeds = execution._RealGateCExecution__module_anchor_seeds
                    base = next(seed for seed in seeds if seed.module_name == "tm_activation_journal")
                    assert base.source.is_current()
                    original = base.source.content
                    changed = original + b"\\n# source replacement\\n"
                    replacement = base.source.path.with_suffix(".replacement")
                    replacement.write_bytes(changed)
                    windows = sys.platform == "win32"
                    if windows:
                        try:
                            replacement.replace(base.source.path)
                        except PermissionError as error:
                            assert error.winerror == 32, repr(error)
                        else:
                            raise AssertionError("retained Windows source must block replacement")
                        assert base.source.path.read_bytes() == original
                        assert replacement.read_bytes() == changed
                        assert base.source.is_current()
                    else:
                        replacement.replace(base.source.path)
                        assert base.source.path.read_bytes() == changed
                        assert not replacement.exists()
                        assert not base.source.is_current()
                    assert execution._RealGateCExecution__validation_binding is None
                    old = composition.host.retrieval_snapshot()
                    assert not old.display.context_available
                    returned = owner.validate_gate_c(
                        generated_at_utc=now,
                        valid_until_utc=datetime(2030, 1, 2, tzinfo=timezone.utc),
                        evaluated_at_utc=now,
                    )
                    if windows:
                        assert returned is not old
                        assert returned.generation == old.generation + 1
                        assert returned.display.context_available
                        assert execution._RealGateCExecution__validation_binding is not None
                        assert base.source.is_current()
                        assert base.source.path.read_bytes() == original
                        assert replacement.read_bytes() == changed
                    else:
                        assert returned is old
                        assert not returned.display.context_available
                        assert execution._RealGateCExecution__validation_binding is None
                ''')],
                cwd=root,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
