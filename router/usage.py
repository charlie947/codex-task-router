"""Usage/resource-state validation.

TRUST BOUNDARY: this router does not call any provider API and does not
invent a usage-limit endpoint. It reads a JSON file the caller supplies
(default path is a per-host location the caller or an existing native tool
is expected to keep current), and treats that file as the only source of
truth for "is ordinary usage available for this lane". If that file is
missing, unreadable, stale, or reports the lane as exhausted/unsafe, this
module fails closed: dispatch is blocked. This module never queries a
provider, never fabricates a cost cap, and never falls back to a paid or
API-key path on its own.

`paid_fallback_allowed` is a host-maintained ATTESTATION, not a provider
enforcement mechanism and not a real cost cap: this module has no way to
verify a provider account cannot actually be billed. It only refuses to
proceed unless the caller has explicitly written `false` for that field.
A caller who lies in this file, or who separately sets a provider override
this router cannot introspect (a config file, not an env var — see
router.auth), defeats this check; that residual trust boundary is real
and is documented here rather than glossed over.

Expected JSON shape (all keys required unless noted):
{
  "generated_at": "<ISO-8601 UTC timestamp>",
  "paid_fallback_allowed": false,
  "lanes": {
    "luna":   {"available": true, "exhausted": false, "unsafe": false},
    "terra":  {"available": true, "exhausted": false, "unsafe": false},
    "claude": {"available": true, "exhausted": false, "unsafe": false},
    "astra":  {"available": true, "exhausted": false, "unsafe": false}
  }
}
`astra` is REQUIRED in `lanes` like every other lane: astra's named-approval
requirement (see router.dispatch) is an additional gate on top of this
check, never a substitute for it. There is no lane exempt from usage
validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_MAX_AGE_SECONDS = 300  # 5 minutes


@dataclass(frozen=True)
class UsageCheckResult:
    ok: bool
    reason: str


def _parse_timestamp(value: str) -> datetime:
    # Accept trailing "Z" as UTC.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def check_usage(
    lane: str,
    state_path: Path,
    now: Optional[datetime] = None,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> UsageCheckResult:
    """Validate that `lane` is safe to dispatch to, per the usage state file.

    Fails closed (ok=False) on: missing file, unreadable/invalid JSON, a
    non-object body, missing/wrong-type fields, an unparsable/stale/future
    timestamp, `paid_fallback_allowed` missing or not exactly `False`, or
    the lane reporting exhausted/unsafe/unavailable. Applies identically to
    every lane, including `astra`.
    """
    now = now or datetime.now(timezone.utc)

    if not state_path.exists():
        return UsageCheckResult(False, f"usage state file not found: {state_path}")

    try:
        raw = state_path.read_text(encoding="utf-8")
    except OSError as exc:
        return UsageCheckResult(False, f"usage state file unreadable: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return UsageCheckResult(False, f"usage state file is not valid JSON: {exc}")

    # A malformed top-level shape (e.g. a JSON array or scalar) must fail
    # closed, not crash on the first .get() call.
    if not isinstance(data, dict):
        return UsageCheckResult(
            False, f"usage state file must be a JSON object, got {type(data).__name__}"
        )

    generated_at = data.get("generated_at")
    if not isinstance(generated_at, str) or not generated_at.strip():
        return UsageCheckResult(False, "usage state 'generated_at' must be a non-empty string timestamp")

    try:
        generated_dt = _parse_timestamp(generated_at)
    except (ValueError, TypeError) as exc:
        return UsageCheckResult(False, f"usage state 'generated_at' unparsable: {exc}")

    age_seconds = (now - generated_dt).total_seconds()
    if age_seconds < 0:
        return UsageCheckResult(False, "usage state 'generated_at' is in the future")
    if age_seconds > max_age_seconds:
        return UsageCheckResult(
            False,
            f"usage state is stale: {age_seconds:.0f}s old, max allowed {max_age_seconds}s",
        )

    if "paid_fallback_allowed" not in data:
        return UsageCheckResult(False, "usage state missing 'paid_fallback_allowed'")
    paid_fallback_allowed = data["paid_fallback_allowed"]
    if not isinstance(paid_fallback_allowed, bool):
        return UsageCheckResult(
            False,
            f"usage state 'paid_fallback_allowed' must be a boolean, got "
            f"{type(paid_fallback_allowed).__name__}",
        )
    if paid_fallback_allowed:
        return UsageCheckResult(
            False, "usage state 'paid_fallback_allowed' is true; this router will not dispatch"
        )

    lanes = data.get("lanes")
    if not isinstance(lanes, dict):
        return UsageCheckResult(False, "usage state missing 'lanes' object")

    lane_state = lanes.get(lane)
    if not isinstance(lane_state, dict):
        return UsageCheckResult(False, f"usage state missing entry for lane {lane!r}")

    for required_key in ("available", "exhausted", "unsafe"):
        if required_key not in lane_state:
            return UsageCheckResult(
                False, f"usage state for lane {lane!r} missing key {required_key!r}"
            )
        value = lane_state[required_key]
        # Strict bool only: a string "false", 0/1, or null must not be
        # silently coerced by Python truthiness (bool("false") is True).
        if not isinstance(value, bool):
            return UsageCheckResult(
                False,
                f"usage state for lane {lane!r} key {required_key!r} must be a boolean, "
                f"got {type(value).__name__}",
            )

    if lane_state["exhausted"]:
        return UsageCheckResult(False, f"lane {lane!r} usage is exhausted")
    if lane_state["unsafe"]:
        return UsageCheckResult(False, f"lane {lane!r} usage state flagged unsafe")
    if not lane_state["available"]:
        return UsageCheckResult(False, f"lane {lane!r} not marked available")

    return UsageCheckResult(True, f"lane {lane!r} usage ok as of {generated_at}")
