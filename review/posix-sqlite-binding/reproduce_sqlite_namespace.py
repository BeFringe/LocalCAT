"""仅用合成数据观察 SQLite 路径与 journal；不验证产品能力。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import tempfile


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def read_rows(path: Path) -> dict[str, object]:
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            return {"rows": list(db.execute("SELECT value FROM entries"))}
    except sqlite3.Error as exc:
        return {"sqlite_errorname": getattr(exc, "sqlite_errorname", type(exc).__name__)}


def experiment(moment: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="sqlite-namespace-review-") as directory:
        base = Path(directory).resolve()
        configured = base / "resources"
        configured.mkdir()
        main = configured / "data.sqlite3"
        with closing(sqlite3.connect(main, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("CREATE TABLE entries(value TEXT)")
            db.execute("INSERT INTO entries VALUES ('seed')")
        parent_fd = os.open(configured, os.O_RDONLY | os.O_DIRECTORY)
        main_fd = os.open("data.sqlite3", os.O_RDONLY, dir_fd=parent_fd)
        before = digest(main)
        held = configured
        db = None
        result: dict[str, object] = {"case": moment, "replacement_created": False}

        def replace_parent() -> None:
            nonlocal held
            held = base / "held-resources"
            configured.rename(held)
            shutil.copytree(held, configured)
            result["replacement_created"] = True
            result["replacement_journal_before_commit"] = (
                configured / "data.sqlite3-journal"
            ).exists()

        try:
            if moment == "before_connect":
                replace_parent()
            db = sqlite3.connect(main.as_uri() + "?mode=rw", uri=True, isolation_level=None)
            result["journal_mode"] = db.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO entries VALUES ('transaction')")
            if moment == "after_insert":
                replace_parent()
            db.commit()
            result["commit_returned_success"] = True
        except sqlite3.Error as exc:
            result["commit_returned_success"] = False
            result["sqlite_errorname"] = getattr(exc, "sqlite_errorname", type(exc).__name__)
        finally:
            if db is not None:
                db.close()
            result["retained_inode_still_original"] = (
                os.fstat(main_fd).st_ino == (held / "data.sqlite3").stat().st_ino
            )
            result["retained_database_changed"] = digest(held / "data.sqlite3") != before
            result["configured_database_changed"] = digest(main) != before
            result["retained_journal_after_close"] = (held / "data.sqlite3-journal").exists()
            result["configured_journal_after_close"] = (configured / "data.sqlite3-journal").exists()
            os.close(main_fd)
            os.close(parent_fd)
        # 观察 journal 之后才做独立只读打开，避免把恢复副作用混入观察。
        result["retained_readonly_reopen"] = read_rows(held / "data.sqlite3")
        result["configured_readonly_reopen"] = read_rows(main)
        return result


def main() -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        raise SystemExit("此合成探针只面向 POSIX，不是 Windows 反例。")
    print(json.dumps({
        "kind": "observation_only_not_product_pass",
        "runtime": {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                    "platform": sys.platform, "kernel": platform.release(),
                    "architecture": platform.machine()},
        "experiments": [experiment(case) for case in
                        ("control", "before_connect", "after_insert")],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
