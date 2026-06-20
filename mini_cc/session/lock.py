"""Per-project file locks. Same project_id serializes; different projects
run fully in parallel."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import ContextManager

if sys.platform == "win32":
    # msvcrt-based locking for Windows; fall back to in-process Lock
    import msvcrt

    class _FileLock:
        def __init__(self, path: Path):
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = None
            self._thread_lock = threading.Lock()

        def __enter__(self):
            self._thread_lock.acquire()
            self._fh = open(self.path, "a+b")
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
            return self

        def __exit__(self, *exc):
            try:
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._fh.close()
                self._thread_lock.release()
else:
    import fcntl

    class _FileLock:  # type: ignore[no-redef]
        def __init__(self, path: Path):
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = None

        def __enter__(self):
            self._fh = open(self.path, "a+b")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            return self

        def __exit__(self, *exc):
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()


def lock_for(projects_root: Path, project_id: str) -> ContextManager:
    return _FileLock(Path(projects_root) / project_id / ".state" / ".session.lock")
