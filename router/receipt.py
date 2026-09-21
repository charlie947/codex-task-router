"""Immutable per-dispatch receipt.

A receipt is written once as JSON and then chmod'd read-only. It always
records requested vs observed model separately: "requested" is what this
router asked for, "observed" is what was actually parsed back from the
subprocess output (or None if it could not be determined). Never conflate
the two.
"""

from __future__ import annotations

import json
import os
import stat
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional


@dataclass(frozen=True)
class Receipt:
    task_id: str
    workdir: str
    lane: str
    requested_model: str
    observed_model: Optional[str]
    argv: List[str]
    dry_run: bool
    status: str  # one of: "dry_run", "ok", "error", "blocked"
    returncode: Optional[int]
    stdout: str
    stderr: str
    reason: str
    created_at: float = field(default_factory=time.time)


def write_receipt(receipt_dir: Path, receipt: Receipt) -> Path:
    """Write `receipt` as a new, immutable (read-only) JSON file.

    Never overwrites an existing receipt: the filename includes a
    monotonic-ish suffix, and if a collision is somehow hit, this raises
    rather than silently overwriting evidence.
    """
    receipt_dir.mkdir(parents=True, exist_ok=True)
    safe_task = "".join(c if c.isalnum() or c in "-_." else "_" for c in receipt.task_id)
    ts_ns = time.time_ns()
    path = receipt_dir / f"{safe_task}__{ts_ns}.json"
    if path.exists():
        raise FileExistsError(f"receipt path already exists, refusing to overwrite: {path}")

    payload = asdict(receipt)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")

    # Make immutable: read-only for owner/group/other.
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return path
