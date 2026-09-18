"""The patch replayer is an input assembler, not W3 execution evidence."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.windows_frozen_patch_series import (
    Patch, PatchInputError, replay_patch_series, source_inventory_digest,
)


def edit(path, old, new):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,2 +1,2 @@\n {old}\n-value\n+{new}\n").encode()


def create(path, text="int owned;\n"):
    return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n"
            f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{text}").encode()


class PatchSeriesTests(unittest.TestCase):
    def setUp(self):
        self.base = {"bootloader/src/main.c": b"entry\nvalue\n",
                     "bootloader/wscript": b"build\nvalue\n",
                     "bootloader/src/untouched.h": b"locked\n"}
        self.contract = {
            "owned_existing_files": ["bootloader/src/main.c", "bootloader/wscript"],
            "owned_new_files": ["bootloader/src/localcat_entry.c"],
            "series": [{"id": "entry"}, {"id": "build"}],
        }
        self.patches = [Patch("entry", edit("bootloader/src/main.c", "entry", "patched")
                             + create("bootloader/src/localcat_entry.c")),
                        Patch("build", edit("bootloader/wscript", "build", "release"))]
        self.digest = source_inventory_digest(self.base)

    def replay(self, *, base=None, patches=None, contract=None, digest=None):
        return replay_patch_series(
            self.base if base is None else base,
            self.patches if patches is None else patches,
            patch_contract=self.contract if contract is None else contract,
            expected_base_digest=self.digest if digest is None else digest,
        )

    def test_portable_provenance_binds_exact_sources(self):
        first = self.replay()
        second = self.replay(base=dict(reversed(list(self.base.items()))))
        self.assertEqual(first.files["bootloader/src/main.c"], b"entry\npatched\n")
        self.assertEqual(first.files["bootloader/src/untouched.h"], b"locked\n")
        self.assertEqual(first.files["bootloader/src/localcat_entry.c"], b"int owned;\n")
        self.assertEqual(first.provenance_bytes, second.provenance_bytes)
        self.assertEqual(first.applied_sources_digest,
                         hashlib.sha256(first.provenance_bytes).hexdigest())
        facts = json.loads(first.provenance_bytes)
        self.assertEqual([p["id"] for p in facts["patches"]], ["entry", "build"])
        self.assertEqual(facts["base_digest"], self.digest)
        self.assertEqual(facts["result_digest"], source_inventory_digest(first.files))
        self.assertNotIn("runw.exe", first.provenance_bytes.decode())
        with self.assertRaises(TypeError):
            first.files["bootloader/src/main.c"] = b"mutate"
        self.assertEqual(self.base["bootloader/src/main.c"], b"entry\nvalue\n")

    def test_patch_series_order_missing_duplicate_and_extra_are_rejected(self):
        for patches in (list(reversed(self.patches)), self.patches[:1],
                        [self.patches[0]] * 2, self.patches + [self.patches[0]]):
            with self.subTest(patches=[p.id for p in patches]), self.assertRaises(PatchInputError):
                self.replay(patches=patches)

    def test_drift_in_untouched_base_also_rejected(self):
        for path in self.base:
            with self.subTest(path=path), self.assertRaisesRegex(PatchInputError, "base"):
                self.replay(base={**self.base, path: b"drift\n"})

    def test_unowned_target_and_path_aliases_are_rejected_before_apply(self):
        for path in ("bootloader/src/untouched.h", "../outside.c", "C:/outside.c",
                     "bootloader/src/../src/main.c", "bootloader/src/Main.c",
                     "bootloader/src/main.c:stream", "bootloader/src/main.c."):
            with self.subTest(path=path), self.assertRaises(PatchInputError):
                self.replay(patches=[replace(self.patches[0], content=edit(path, "entry", "bad")),
                                     self.patches[1]])

    def test_missing_new_source_or_conflict_leaves_original_untouched(self):
        for payload in (edit("bootloader/src/main.c", "entry", "patched"),
                        edit("bootloader/src/main.c", "wrong context", "patched")
                        + create("bootloader/src/localcat_entry.c")):
            with self.subTest(payload=payload), self.assertRaises(PatchInputError):
                self.replay(patches=[replace(self.patches[0], content=payload), self.patches[1]])
        self.assertEqual(self.base["bootloader/src/main.c"], b"entry\nvalue\n")

    def test_rename_delete_binary_symlink_and_metadata_only_rejected(self):
        header = b"diff --git a/bootloader/src/main.c b/bootloader/src/main.c\n"
        for payload in (header + b"old mode 100644\nnew mode 100755\n",
                        header + b"deleted file mode 100644\n",
                        header + b"GIT binary patch\nliteral 0\nHcmV?d00001\n",
                        header + b"similarity index 100%\nrename from x\nrename to y\n",
                        create("bootloader/src/localcat_entry.c").replace(b"100644", b"120000")):
            with self.subTest(payload=payload), self.assertRaises(PatchInputError):
                self.replay(patches=[replace(self.patches[0], content=payload), self.patches[1]])

    def test_duplicate_file_section_and_mismatched_headers_rejected(self):
        for payload in (self.patches[0].content * 2,
                        self.patches[0].content.replace(b"+++ b/bootloader/src/main.c",
                                                       b"+++ b/bootloader/wscript")):
            with self.subTest(payload=payload), self.assertRaises(PatchInputError):
                self.replay(patches=[replace(self.patches[0], content=payload), self.patches[1]])

    def test_owner_inventory_and_unsafe_base_are_rejected(self):
        for base, contract in (
            ({**self.base, "bootloader/src/localcat_entry.c": b"pre-existing"}, self.contract),
            ({**self.base, "../escape": b"bad"}, self.contract),
            ({**self.base, "bootloader/src/MAIN.c": b"alias"}, self.contract),
            ({**self.base, "bootloader/src/main.c": "not bytes"}, self.contract),
            (self.base, {**self.contract, "owned_existing_files": ["missing.c"]}),
            (self.base, {**self.contract, "owned_new_files": ["bootloader/src/main.c"]}),
        ):
            with self.subTest(base=base, contract=contract), self.assertRaises(PatchInputError):
                self.replay(base=base, contract=contract)

    def test_replay_matches_git_for_regular_crlf_multihunk_and_no_newline(self):
        git = shutil.which("git")
        self.assertIsNotNone(git, "this build-input test requires the repository Git tool")
        variants = [
            (self.base, self.patches),
            ({**self.base, "bootloader/src/main.c": b"entry\r\nvalue\r\n"},
             [replace(self.patches[0], content=self.patches[0].content
                      .replace(b" entry\n-value\n+patched\n", b" entry\r\n-value\r\n+patched\r\n")),
              self.patches[1]]),
            ({**self.base, "bootloader/src/main.c": b"entry\nvalue"},
             [replace(self.patches[0], content=self.patches[0].content
                      .replace(b"-value\n+patched\n",
                               b"-value\n\\ No newline at end of file\n+patched\n\\ No newline at end of file\n")),
              self.patches[1]]),
            ({**self.base, "bootloader/src/main.c": b"entry\nvalue\nkeep\nmiddle\ntail\nvalue\n"},
             [replace(self.patches[0], content=edit("bootloader/src/main.c", "entry", "first")
                      .replace(b"@@ -1,2 +1,2 @@", b"@@ -1,3 +1,3 @@")
                      + b" keep\n@@ -5,2 +5,2 @@\n tail\n-value\n+second\n"
                      + create("bootloader/src/localcat_entry.c")), self.patches[1]]),
        ]
        for base, patches in variants:
            with self.subTest(base=base), tempfile.TemporaryDirectory(prefix="w3-patch-parity-") as name:
                root = Path(name)
                for relative, content in base.items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
                for patch in patches:
                    result = subprocess.run([git, "-C", name, "-c", "core.autocrlf=false", "apply",
                                             "--no-index", "--whitespace=nowarn", "-"],
                                            input=patch.content, capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                replay = self.replay(base=base, patches=patches, digest=source_inventory_digest(base))
                self.assertEqual(dict(replay.files), {path.relative_to(root).as_posix(): path.read_bytes()
                                                     for path in root.rglob("*") if path.is_file()})

    def test_malformed_hunk_ranges_counts_and_newline_markers_fail_closed(self):
        for old, new in ((b"@@ -1,2 +1,2 @@", b"@@ -1,3 +1,2 @@"),
                         (b"@@ -1,2 +1,2 @@", b"@@ -1,2 +2,2 @@"),
                         (b"@@ -1,2 +1,2 @@", b"@@ -0,2 +1,2 @@"),
                         (b"+patched\n", b"+patched\n\\ No newline at end of file\n extra\n")):
            with self.subTest(new=new), self.assertRaises(PatchInputError):
                self.replay(patches=[replace(self.patches[0], content=self.patches[0].content.replace(old, new)),
                                     self.patches[1]])

    def test_absent_eof_context_and_internal_no_newline_are_rejected(self):
        base = {**self.base, "bootloader/src/main.c": b"entry\nvalue\nmore\n"}
        with self.assertRaises(PatchInputError):
            self.replay(base=base, digest=source_inventory_digest(base))
        payload = create("bootloader/src/localcat_entry.c").replace(
            b"@@ -0,0 +1 @@\n+int owned;\n",
            b"@@ -0,0 +1,2 @@\n+int owned;\n\\ No newline at end of file\n+bad join;\n")
        with self.assertRaises(PatchInputError):
            self.replay(patches=[replace(self.patches[0], content=edit("bootloader/src/main.c", "entry", "patched")
                                        + payload), self.patches[1]])

    def test_bare_cr_is_content_not_a_hunk_line_separator(self):
        base = {**self.base, "bootloader/src/main.c": b"x\r-y\n"}
        header = (b"diff --git a/bootloader/src/main.c b/bootloader/src/main.c\n"
                  b"--- a/bootloader/src/main.c\n+++ b/bootloader/src/main.c\n")
        malformed = header + b"@@ -1,2 +1 @@\n-x\r--y\n+z\n"
        with self.assertRaises(PatchInputError):
            self.replay(base=base, digest=source_inventory_digest(base),
                        patches=[replace(self.patches[0], content=malformed
                                         + create("bootloader/src/localcat_entry.c")), self.patches[1]])
        valid = header + b"@@ -1 +1 @@\n-x\r-y\n+z\n"
        result = self.replay(base=base, digest=source_inventory_digest(base),
                             patches=[replace(self.patches[0], content=valid
                                              + create("bootloader/src/localcat_entry.c")), self.patches[1]])
        self.assertEqual(result.files["bootloader/src/main.c"], b"z\n")


if __name__ == "__main__":
    unittest.main()
