"""Owned temporary test directories with explicit Windows long-path cleanup."""

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile

from tm_benchmark_platform_io import _windows_extended_path


@contextmanager
def worker_temporary_directory() -> Iterator[str]:
    temporary = tempfile.TemporaryDirectory(prefix="tm-worker-")
    path = Path(os.path.abspath(temporary.name))
    expected_parent = Path(os.path.abspath(tempfile.gettempdir()))
    try:
        yield temporary.name
    finally:
        if sys.platform == "win32":
            # Only the exact directory allocated by this context is eligible;
            # no broad tmp scan, ignored failure, or production path change.
            if path.parent != expected_parent or not path.name.startswith("tm-worker-"):
                raise RuntimeError("temporary worker root escaped its owner")
            shutil.rmtree(_windows_extended_path(path))
        temporary.cleanup()
