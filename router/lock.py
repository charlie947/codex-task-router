"""Atomic active-task protection, scoped by canonical workdir only.

Uses os.open with O_CREAT|O_EXCL, which is atomic at the filesystem level:
two processes racing to create the same lock file cannot both succeed. On
an existing lock, this raises immediately. It never retries and never
falls back to a different workdir silently, and it never clears a stale
lock automatically: a lock left behind by a crashed process must be
removed by a human, on purpose, not by this module guessing it is safe.

SCOPING: the lock FILE is keyed by the canonical workdir alone, not by
task_id. A workdir is the actual shared resource being protected — two
different task_ids racing to write into the same directory at the same
time is exactly the conflict this lock exists to prevent, and task_id
must never let that collision through. (An earlier version of this module
included task_id in the lock filename itself, so two different task_ids
on the identical workdir computed two different lock paths and both
succeeded concurrently — a real bug, not a hypothetical one.) task_id is
still recorded inside the lock file's contents for diagnostics (which
task currently holds this workdir), it just no longer participates in
which file gets created.

This module only protects concurrent *dispatches made through this router
in this process tree*. It has no visibility into, and does not discover,
any other worker (a different terminal, a different tool, a manually run
codex/claude session) that might be writing to the same workdir outside
this router — detecting that remains a manual host responsibility this
module does not and cannot automate.

The lock path is derived with hashlib (stable across processes and Python
versions), not Python's builtin hash() (randomized per-process via
PYTHONHASHSEED unless disabled, so two processes racing on the identical
workdir could previously compute two different lock file paths and both
succeed, defeating the lock entirely).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ActiveTaskError(Exception):
    """Raised when a workdir is already locked by another dispatch."""


@dataclass(frozen=True)
class LockInfo:
    task_id: str
    workdir: str
    pid: int
    acquired_at: float


def _lock_path(lock_dir: Path, workdir: str) -> Path:
    # Scoped to the canonical workdir ONLY. Do not add task_id here: this
    # lock protects the workdir as a shared resource, and two different
    # task_ids must collide on it, not bypass each other.
    canonical_workdir = str(Path(workdir).resolve())
    workdir_hash = hashlib.sha256(canonical_workdir.encode("utf-8")).hexdigest()[:16]
    return lock_dir / f"workdir-{workdir_hash}.lock"


class TaskLock:
    """Context manager. Raises ActiveTaskError on an already-held lock.

    Locking is keyed by `workdir` alone (see module docstring). `task_id`
    is still required and still recorded in the lock file's contents, but
    two different task_ids pointed at the same workdir will always
    conflict — that is the point of this lock.
    """

    def __init__(self, lock_dir: Path, task_id: str, workdir: str):
        if not task_id or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not workdir or not workdir.strip():
            raise ValueError("workdir must be a non-empty string")
        self.lock_dir = lock_dir
        self.task_id = task_id
        self.workdir = workdir
        self.path = _lock_path(lock_dir, workdir)
        self._fd: Optional[int] = None

    def __enter__(self) -> "TaskLock":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = "<unreadable>"
            try:
                existing = self.path.read_text(encoding="utf-8")
            except OSError:
                pass
            raise ActiveTaskError(
                f"workdir={self.workdir!r} is already locked (requested by "
                f"task_id={self.task_id!r}) at {self.path}: {existing}"
            )
        self._fd = fd
        info = {
            "task_id": self.task_id,
            "workdir": self.workdir,
            "pid": os.getpid(),
            "acquired_at": time.time(),
        }
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        self._fd = None  # fdopen closed it
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # Preserve evidence rather than masking a failure; do not retry.
            pass
        return None
