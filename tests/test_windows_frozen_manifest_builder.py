from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import unittest

from tools.windows_frozen_manifest import ManifestInputError, SourceEntry, prepare_manifest


class FrozenManifestBuilderTests(unittest.TestCase):
    def entries(self):
        return [
            SourceEntry("fixture", "_internal/fixture.txt", "fixture", b"a\x00b"),
            SourceEntry("bootstrap", "_internal/bootstrap.py", "bootstrap", b"x=1\n", ("fixture",)),
        ]

    def prepare(self, entries=None, **overrides):
        return prepare_manifest(
            self.entries() if entries is None else entries,
            candidate_input_digest=overrides.get("candidate_input_digest", "1" * 64),
            applied_sources_digest="2" * 64,
        )

    def test_canonical_replay_and_acyclic_manifest_layout(self):
        first = self.prepare()
        self.assertEqual(first, self.prepare(list(reversed(self.entries()))))
        data = first.runtime_bytes
        header = struct.unpack_from("<8s10I32s", data)
        self.assertEqual(header[:6], (b"LCFMV001", 1, 80, 2, 72, 80))
        self.assertEqual(header[6:11], (224, 1, 228, len(data) - 228, 0))
        prelink = json.loads(first.prelink_bytes)
        self.assertEqual(header[11], hashlib.sha256(first.prelink_bytes).digest())
        self.assertEqual(prelink["entries"][0]["id"], "bootstrap")
        self.assertEqual(prelink["entries"][1]["sha256"], hashlib.sha256(b"a\0b").hexdigest())
        self.assertEqual(struct.unpack_from("<I", data, 224)[0], 1)

    def test_input_changes_bind_both_manifests(self):
        old = self.prepare()
        changed = self.prepare(candidate_input_digest="3" * 64)
        self.assertNotEqual(old.prelink_bytes, changed.prelink_bytes)
        self.assertNotEqual(old.runtime_bytes, changed.runtime_bytes)
        entries = self.entries()
        entries[0] = SourceEntry("fixture", "_internal/fixture.txt", "fixture", b"other")
        self.assertNotEqual(old.runtime_bytes, self.prepare(entries).runtime_bytes)

    def test_render_header_has_exact_bound_digests(self):
        template = (Path(__file__).resolve().parents[1] /
                    "packaging/windows/frozen-entry/native/localcat_manifest.h").read_text(encoding="utf-8")
        prepared = self.prepare()
        header = prepared.render_header(template)
        self.assertNotIn("@@", header)
        self.assertIn(hashlib.sha256(prepared.prelink_bytes).hexdigest(), header)
        self.assertIn(",".join(str(byte) for byte in hashlib.sha256(prepared.runtime_bytes).digest()), header)
        for bad in (template + "@@UNKNOWN@@", template.replace("@@PRELINK_INPUT_DIGEST@@", "0"),
                    template + "@@PRELINK_INPUT_DIGEST@@"):
            with self.subTest(template=bad[-40:]), self.assertRaises(ManifestInputError):
                prepared.render_header(bad)

    def test_reject_ambiguous_names_paths_roles_and_source(self):
        for path in ("../x.py", "C:/x.py", "/x.py", "_internal/x:ads", "_internal/NUL.txt",
                     "_internal/COM\u00b9.txt", "_internal/a.", "_internal/a ", "a//b", "a\\b",
                     "_internal/\x00x", "x/*.py", "_internal/__pycache__/x.py", "x.pyc"):
            with self.subTest(path=path), self.assertRaises(ManifestInputError):
                self.prepare([SourceEntry("source", path, "critical-source", b"pass\n")])
        for entry in (SourceEntry("BAD", "a.py", "bootstrap", b"pass"),
                      SourceEntry("source", "a.py", "unknown", b"pass"),
                      SourceEntry("source", "a.py", "bootstrap", b"a\0b"),
                      SourceEntry("source", "a.py", "bootstrap", "not-bytes")):
            with self.subTest(entry=entry), self.assertRaises(ManifestInputError):
                self.prepare([entry])

    def test_reject_duplicates_missing_dependencies_cycles_and_limits(self):
        a = SourceEntry("a", "x/a.py", "bootstrap", b"pass", ("b",))
        b = SourceEntry("b", "x/b.txt", "fixture", b"ok", ("a",))
        cases = [[], [a], [a, b], [b, b],
                 [SourceEntry("a", "x/a.py", "bootstrap", b"pass"),
                  SourceEntry("b", "X/A.py", "bootstrap", b"pass")],
                 [SourceEntry("a", "a.txt", "fixture", b"", ("a", "a"))],
                 [SourceEntry(f"e{i}", f"e{i}.txt", "fixture", b"") for i in range(2049)]]
        for entries in cases:
            with self.subTest(entries=entries), self.assertRaises(ManifestInputError):
                self.prepare(entries)
        for digest in ("short", "A" * 64, "z" * 64):
            with self.subTest(digest=digest), self.assertRaises(ManifestInputError):
                self.prepare(candidate_input_digest=digest)

    def test_complete_source_closure_keeps_distinct_package_initializers(self):
        entries = [SourceEntry(f"package-{i}", f"_internal/packages/p{i}/__init__.py",
                               "critical-source", b"value = 1\n") for i in range(65)]
        prepared = self.prepare(entries)
        self.assertEqual(struct.unpack_from("<I", prepared.runtime_bytes, 16)[0], 65)
        self.assertEqual(len(json.loads(prepared.prelink_bytes)["entries"]), 65)

    def test_native_basename_remains_exclusive_across_roles_and_case(self):
        for other_role in ("native", "interpreter", "bootstrap", "critical-source", "fixture"):
            for reverse in (False, True):
                entries = [SourceEntry("native", "_internal/runtime/probe.dll", "native", b"MZ"),
                           SourceEntry("other", "_internal/other/PROBE.DLL", other_role, b"pass")]
                if reverse:
                    entries.reverse()
                with self.subTest(role=other_role, reverse=reverse), self.assertRaises(ManifestInputError):
                    self.prepare(entries)


if __name__ == "__main__":
    unittest.main()
