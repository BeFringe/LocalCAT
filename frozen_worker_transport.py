"""Directed same-executable transport after the native E10 handoff."""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Any


def run_frozen_child(command: tuple[str, ...], request: bytes, timeout: float) -> subprocess.CompletedProcess[bytes]:
    from tm_gate_inputs import _require_native_frozen_producer

    try:
        bootstrap = sys.modules["frozen_source_bootstrap"]
        authority: Any = getattr(bootstrap, "_producer")
        _require_native_frozen_producer(authority)
        authority.require_worker_wait_allowed()
        executable = getattr(bootstrap, "_executable")
        if command not in ((executable, "--localcat-tm-worker", "migration"),
                           (executable, "--localcat-tm-worker", "query")):
            raise ValueError("worker command is not this native-bound executable")
        authority._invoke(lambda: getattr(bootstrap, "_raw").module_reproof("bootstrap"))
    except (KeyError, AttributeError, RuntimeError, ValueError, TypeError) as error:
        raise OSError("frozen worker requires the platform E10 transport") from error
    if type(request) is not bytes:
        raise TypeError("frozen worker request must be exact bytes")
    environment = {name: value for name, value in os.environ.items()
                   if not name.upper().startswith(("PYTHON", "PYINSTALLER", "_PYINSTALLER", "QT_", "MIMALLOC_"))
                   and name.upper() != "__PYVENV_LAUNCHER__"}
    system_root = next(value for name, value in environment.items() if name.upper() == "SYSTEMROOT")
    environment["PATH"] = os.path.join(system_root, "System32")
    # CPython's Windows Popen constructs a STARTUPINFOEX handle-list containing
    # only its child stdin/stdout/stderr ends when close_fds=True. The native
    # retained handles were created non-inheritable and are never handed down.
    return subprocess.run(command, input=request, stdin=None, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, close_fds=True, check=False,
                          cwd=os.path.dirname(executable), env=environment, timeout=timeout)
