"""Fail-closed validation of the real charlie-session-guard resource state.

TRUST BOUNDARY: this router does not compute machine load itself. It reads
the JSON file an existing local guard/hook already maintains (default path
~/.local/state/charlie-session-guard/status.json) and treats it as the only
source of truth for "is it safe to start new heavy work right now". If that
file is missing, unreadable, malformed, stale, timestamped in the future, or
reports defer_new_heavy_work=true, this module fails closed: dispatch is
blocked for every lane, including an already-approved astra lane. Astra
approval exempts the human-approval gate only; it never exempts this gate.

Real observed schema (2026-09-20, ~/.local/state/charlie-session-guard/status.json):
{
  "at": 1789912909.80599,          # float, unix seconds
  "level": 1,
  "defer_new_heavy_work": false,   # bool
  ...additional informational fields (load_1m, cpus, largest_processes, ...)
}
Only "at" and "defer_new_heavy_work" are required and validated here; the
guard is free to carry other informational fields this module ignores.

Freshness window: the guard schema itself does not document a staleness
contract, so this module applies a conservative default of 120 seconds
(tighter than usage.py's 5-minute window, because this is a live pressure
sample meant to reflect current machine load, not a slow-changing quota).
Callers needing a different window should pass max_age_seconds explicitly.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_MAX_AGE_SECONDS = 120  # 2 minutes; see module docstring.


@dataclass(frozen=True)
class GuardCheckResult:
    ok: bool
    reason: str


def _is_finite_number(value: object) -> bool:
    # bool is a subclass of int in Python; explicitly exclude it so a JSON
    # `true`/`false` is never silently accepted as a timestamp.
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def check_guard(
    state_path: Path,
    now: Optional[datetime] = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> GuardCheckResult:
    """Validate that the machine is safe to start new heavy work on.

    Fails closed (ok=False) on: missing file, unreadable/invalid JSON, a
    non-object body, a missing/non-numeric/non-finite/negative/future 'at',
    a stale 'at', a missing or non-boolean 'defer_new_heavy_work', or
    defer_new_heavy_work=true. There is no unsafe default: any validation
    failure blocks, it never falls through to "assume safe".
    """
    now_ts = (now or datetime.now(timezone.utc)).timestamp()

    if not state_path.exists():
        return GuardCheckResult(False, f"guard state file not found: {state_path}")

    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError as exc:
        return GuardCheckResult(False, f"guard state file unreadable: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return GuardCheckResult(False, f"guard state file is not valid JSON: {exc}")

    if not isinstance(data, dict):
        return GuardCheckResult(False, "guard state file is not a JSON object")

    at = data.get("at")
    if "at" not in data or not _is_finite_number(at):
        return GuardCheckResult(
            False, f"guard state 'at' must be a finite numeric unix timestamp, got {at!r}"
        )

    age_seconds = now_ts - at
    if age_seconds < 0:
        return GuardCheckResult(False, "guard state 'at' is in the future")
    if age_seconds > max_age_seconds:
        return GuardCheckResult(
            False,
            f"guard state is stale: {age_seconds:.0f}s old, max allowed {max_age_seconds}s",
        )

    if "defer_new_heavy_work" not in data:
        return GuardCheckResult(False, "guard state missing 'defer_new_heavy_work'")

    defer = data["defer_new_heavy_work"]
    if not isinstance(defer, bool):
        return GuardCheckResult(
            False,
            f"guard state 'defer_new_heavy_work' must be a boolean, got {type(defer).__name__}",
        )

    if defer:
        return GuardCheckResult(False, "guard state reports defer_new_heavy_work=true")

    return GuardCheckResult(True, f"guard state ok as of at={at}")
