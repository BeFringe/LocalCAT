"""Render native template additions for the approved five-patch source series.

This development utility emits an apply_patch request on stdout; it never
writes source files, authenticates build inputs, or approves a frozen build.
The build owner must still replay and verify the complete series independently.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys


NATIVE_PATCH_MEMBERS = {
    "entry-policy": ("localcat_frozen_entry.c", "localcat_frozen_entry.h"),
    "manifest-and-hash": ("localcat_manifest.c", "localcat_manifest.h",
                          "localcat_sha256.c", "localcat_sha256.h"),
    "native-closure": ("localcat_rooted_io.c", "localcat_rooted_io.h",
                       "localcat_native_closure.c", "localcat_native_closure.h"),
    "bootstrap-handoff": ("localcat_frozen_bootstrap.c", "localcat_frozen_bootstrap.h"),
}


def _addition(name: str, content: bytes) -> bytes:
    # These source templates are UTF-8/LF. Refuse an unrepresentable change
    # rather than normalize bytes on their way into a security build input.
    content.decode("utf-8")
    if not content or not content.endswith(b"\n") or b"\r" in content or b"\0" in content:
        raise ValueError("native template must be nonempty UTF-8/LF: " + name)
    path = ("bootloader/src/" + name).encode("ascii")
    lines = content.split(b"\n")[:-1]
    return (b"diff --git a/" + path + b" b/" + path + b"\nnew file mode 100644\n"
            + b"--- /dev/null\n+++ b/" + path + b"\n@@ -0,0 +1,"
            + str(len(lines)).encode("ascii") + b" @@\n"
            + b"".join(b"+" + line + b"\n" for line in lines))


def render_patch_set(native_files: dict[str, bytes], entry_patch: bytes) -> dict[str, bytes]:
    """Preserve the existing main.c hook, replacing only native additions."""
    expected = {name for members in NATIVE_PATCH_MEMBERS.values() for name in members}
    if set(native_files) != expected:
        raise ValueError("native template inventory must match the twelve owned files")
    header = b"diff --git a/bootloader/src/main.c b/bootloader/src/main.c\n"
    if not entry_patch.startswith(header):
        raise ValueError("entry policy must begin with the approved main.c hook")
    split = entry_patch.find(b"\ndiff --git ")
    main_hook = entry_patch if split < 0 else entry_patch[:split + 1]
    result = {}
    for patch_id, members in NATIVE_PATCH_MEMBERS.items():
        result[patch_id] = (main_hook if patch_id == "entry-policy" else b"") + b"".join(
            _addition(name, native_files[name]) for name in members)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.repository.resolve()
    directory = root / "packaging/windows/frozen-entry"
    native = {name: (directory / "native" / name).read_bytes()
              for members in NATIVE_PATCH_MEMBERS.values() for name in members}
    rendered = render_patch_set(native, (directory / "patches/entry-policy.patch").read_bytes())
    request = ["*** Begin Patch\n"]
    for patch_id, content in rendered.items():
        target = directory / "patches" / (patch_id + ".patch")
        if target.exists():
            old = target.read_bytes()
            if old == content:
                continue
            request.extend(["*** Update File: " + target.as_posix() + "\n", "@@\n"])
            request.extend("-" + line + "\n" for line in old.decode("utf-8").split("\n")[:-1])
        else:
            request.append("*** Add File: " + target.as_posix() + "\n")
        request.extend("+" + line + "\n" for line in content.decode("utf-8").split("\n")[:-1])
    request.append("*** End Patch\n")
    sys.stdout.buffer.write("".join(request).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
