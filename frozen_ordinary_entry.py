"""The ordinary distribution has one product and two fixed worker entries."""
from __future__ import annotations

import sys


def _diagnostic(event: dict) -> None:
    """Bounded local startup facts without paths, arguments or exception text."""
    import json
    import os
    from pathlib import Path
    from datetime import datetime, timezone
    allowed = {"stage", "status", "elapsed_ms", "exception_type", "failure_module", "failure_line", "errno", "winerror", "code", "import_name"}
    facts = {key: value for key, value in event.items() if key in allowed}
    facts["time"] = datetime.now(timezone.utc).isoformat()
    facts["pid"] = os.getpid()
    try:
        folder = Path(os.environ["LOCALAPPDATA"]) / "LocalCAT/logs"
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "ordinary-startup.jsonl"
        if log.exists() and log.stat().st_size > 256 * 1024:
            log.replace(folder / "ordinary-startup.previous.jsonl")
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(facts, ensure_ascii=True, sort_keys=True) + "\n")
    except (OSError, KeyError):
        pass


def _failure_event(stage: str, code: str, error: Exception) -> dict:
    import re
    event = {"stage": stage, "status": "failed", "code": code, "exception_type": type(error).__name__}
    if isinstance(error, ImportError) and isinstance(error.name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", error.name):
        event["import_name"] = error.name
    current = error.__traceback__
    while current is not None:
        name = current.tb_frame.f_globals.get("__name__", "")
        if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,119}", name):
            event["failure_module"] = name
            event["failure_line"] = current.tb_lineno
        current = current.tb_next
    return event


def main() -> int:
    arguments = sys.argv[1:]
    try:
        from frozen_candidate import running_candidate
        running_candidate()
    except Exception as error:
        _diagnostic(_failure_event("candidate", "ORDINARY.CANDIDATE_INVALID", error))
        return 1
    if "--localcat-tm-worker" in arguments:
        if len(arguments) != 2 or arguments[0] != "--localcat-tm-worker" or arguments[1] not in ("migration", "query"):
            return 2
        from frozen_worker_entry import serve_ordinary_worker
        try:
            return serve_ordinary_worker(arguments[1])
        except Exception as error:
            _diagnostic(_failure_event("worker-entry", "ORDINARY.WORKER_ENTRY_FAILED", error))
            return 1
    try:
        import qt_editor
        qt_editor._startup_diagnostic = _diagnostic
        result = qt_editor.main(arguments, _ordinary=True)
        if result:
            _diagnostic({"stage": "product", "status": "failed", "code": "ORDINARY.PRODUCT_START_FAILED"})
        return result
    except Exception as error:
        _diagnostic(_failure_event("product", "ORDINARY.PRODUCT_START_FAILED", error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
