"""Orchestrate classification, safety gates, argv build, and dispatch.

Dry-run is the default everywhere in this module: a caller must pass
execute=True to allow a subprocess to actually run. Every path — dry-run,
blocked, error, or ok — writes an immutable receipt.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from . import auth, classify, cli_help, guard, lock, receipt, usage

Runner = Callable[..., "subprocess.CompletedProcess"]


class DispatchBlocked(Exception):
    """Raised when a safety gate blocks dispatch. Callers should not retry
    or auto-fallback on this; the receipt already records the reason."""

    def __init__(self, reason: str, receipt_path: Path):
        super().__init__(reason)
        self.reason = reason
        self.receipt_path = receipt_path

    def __str__(self) -> str:
        return f"{self.reason} (receipt: {self.receipt_path})"


@dataclass(frozen=True)
class LaneSpec:
    binary: str
    model: str
    help_argv: Sequence[str]  # exact argv used to introspect flags, e.g. ("codex","exec","--help")
    required_flags: Sequence[str]


# codex has no --effort flag (verified via `codex exec --help`, 2026-09-20).
# Effort is set via `-c model_reasoning_effort="medium"`. `--json` requests
# structured JSONL event output (verified present in `codex exec --help`) so
# a real error can be detected even when the process exits 0. The prompt is
# sent over stdin (codex exec reads stdin when the PROMPT arg is "-"), not as
# an argv token, so a task string starting with "-" can never be parsed as an
# option (option injection).
CODEX_HELP_ARGV = ("codex", "exec", "--help")
CODEX_REQUIRED_FLAGS = ("-m", "--model", "-c", "--config", "-C", "--cd", "--json")

LANE_SPECS = {
    classify.LANE_LUNA: LaneSpec(
        binary="codex",
        model="gpt-5.6-luna",
        help_argv=CODEX_HELP_ARGV,
        required_flags=CODEX_REQUIRED_FLAGS,
    ),
    classify.LANE_TERRA: LaneSpec(
        binary="codex",
        model="gpt-5.6-terra",
        help_argv=CODEX_HELP_ARGV,
        required_flags=CODEX_REQUIRED_FLAGS,
    ),
    classify.LANE_CLAUDE: LaneSpec(
        binary="claude",
        model="sonnet",
        help_argv=("claude", "--help"),
        required_flags=("-p", "--model", "--effort", "--output-format"),
    ),
    classify.LANE_ASTRA: LaneSpec(
        binary="codex",
        model="gpt-6-astra",
        help_argv=CODEX_HELP_ARGV,
        required_flags=CODEX_REQUIRED_FLAGS,
    ),
}


def build_argv(lane: str, task_text: str, workdir: str) -> Tuple[List[str], Optional[str]]:
    """Return (argv, stdin_input) for `lane`. stdin_input is None unless the
    lane sends the task text over stdin instead of as an argv token.

    Both binaries are protected against option injection (a task string
    starting with "-"): codex via stdin (its own documented "-" convention),
    claude via a "--" end-of-options marker (the standard POSIX/commander.js
    convention every well-behaved argv parser honours — this is an argv
    parsing rule, not model behaviour, so it needed no live invocation to
    verify safe to rely on).
    """
    spec = LANE_SPECS[lane]
    if spec.binary == "codex":
        argv = [
            "codex",
            "exec",
            "-m",
            spec.model,
            "-c",
            'model_reasoning_effort="medium"',
            "-C",
            workdir,
            "--json",
            "-",
        ]
        return argv, task_text
    if spec.binary == "claude":
        argv = [
            "claude",
            "-p",
            "--model",
            spec.model,
            "--effort",
            "medium",
            "--output-format",
            "json",
            "--",
            task_text,
        ]
        return argv, None
    raise ValueError(f"unknown binary {spec.binary!r} for lane {lane!r}")


def _default_runner(
    argv: Sequence[str], input_text: Optional[str] = None, cwd: Optional[str] = None
) -> "subprocess.CompletedProcess":
    return subprocess.run(
        list(argv),
        input=input_text,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=600,
        shell=False,
    )


def _decode(value: object) -> str:
    """Safely decode a stdout/stderr value that may be str, bytes, or None.

    subprocess.TimeoutExpired.stdout/.output/.stderr exist in the stdlib
    (they are populated from whatever was captured before the process was
    killed) and, unlike a completed subprocess.run(text=True) result, are
    NOT guaranteed to already be decoded — they can still be bytes even
    when the original call passed text=True, because the timeout can fire
    before the text-mode wrapper finishes decoding. Never invent a value
    for None; an empty string is the honest representation of "nothing was
    captured", not "decoding failed silently".
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


_MALFORMED_LINE = "__malformed_line__"


def _parse_structured_envelope(stdout: str) -> List[dict]:
    """Parse `stdout` as either a single JSON object (claude's
    `--output-format json`) or JSONL, one JSON object per line (codex's
    `--json`). Returns every successfully parsed JSON *object* found, plus
    a `{"__malformed_line__": True}` marker for each non-blank line that
    failed to parse as JSON at all.

    A malformed line is NOT silently dropped: a genuinely truncated or
    corrupted stream (e.g. a valid `turn.completed` line followed by a cut-
    off/garbled tail) must not be read as a clean success just because the
    one well-formed line in it happened to be a completion event. The
    marker lets `_terminal_result` fail closed on that case without this
    function needing to know what "terminal" means. Non-JSON-object values
    that DID parse (e.g. a bare JSON list on the whole-stdout parse path)
    are still skipped as before, since that shape has never been observed
    from either CLI and forcing every possible shape into scope here would
    be unrelated scope creep beyond the reported bug.
    """
    stripped = stdout.strip()
    if not stripped:
        return []

    try:
        whole = json.loads(stripped)
    except json.JSONDecodeError:
        whole = None
    if isinstance(whole, dict):
        return [whole]

    objects: List[dict] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            objects.append({_MALFORMED_LINE: True})
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


# Terminal-event type discriminators, confirmed by LOCAL, OFFLINE, no-model
# evidence (never a real generation call):
#
# - claude: `codex-route`'s prior pass already confirmed
#   `claude auth status --json`'s real shape via a live no-generation `--json`
#   probe. For the RESULT envelope itself, `strings` against the installed
#   Claude Code binary (~/.local/share/claude/versions/2.1.267, static
#   inspection only, no execution) turned up the literal log line
#   `result subtype=success` plus the field names `is_error`,
#   `total_cost_usd`, `num_turns`, `duration_ms` adjacent to it — consistent
#   with the publicly documented Claude Code SDK `SDKResultMessage` shape
#   (`{"type":"result","subtype":"success"|"error_max_turns"|
#   "error_during_execution",...,"is_error":bool}`). Only a `type: "result"`
#   object counts as terminal; `is_error` decides success vs failure.
# - codex: `codex app-server generate-json-schema` (a local, offline,
#   read-only schema dump — no model call) confirmed real notification
#   method names `turn/started`, `turn/completed`, `thread/started`, and a
#   `Turn.status` enum whose `error` field is "only populated when the
#   Turn's status is failed". `codex exec --json`'s own JSONL stream uses a
#   dotted `type` field (`"turn.completed"`, `"turn.failed"`,
#   `"thread.started"`, etc. — same event family, dot- not slash-separated
#   on the wire); a lifecycle event like `thread.started`/`turn.started` is
#   NOT completion and must never be read as one.
CLAUDE_TERMINAL_TYPE = "result"
CODEX_SUCCESS_TYPE = "turn.completed"
FAILURE_TYPES = {"turn.failed", "error"}


def _terminal_result(objects: Sequence[dict]) -> Optional[str]:
    """Classify parsed structured-output objects as "success", "error",
    "malformed", or None ("no terminal evidence found — treat as
    unverified, not success").

    Any explicit failure signal anywhere in the stream wins outright (a
    stream that later claims success after reporting a failure is still a
    failed run) — checked first. Only after ruling out an explicit failure
    does a malformed/unparseable line or an untrustworthy `type` field
    (anything other than a string or None — e.g. a list, which is not
    hashable and must never reach a set-membership check) force
    `"malformed"`, so a real successful stream that also carries an
    unrelated-but-valid warning line (e.g. codex's own `item.error`
    lifecycle events, which are valid JSON with a `type` string that just
    isn't one this function recognises) is still read correctly — only a
    line this parser could not make sense of at all downgrades the result.
    Success itself requires an actual TERMINAL completion event
    (`type: "turn.completed"`, or `type: "result"` with `is_error: false`)
    — a lifecycle/progress event alone (e.g. `thread.started`, `turn.started`)
    is explicitly NOT evidence of success, even though it is valid JSON.
    """
    saw_malformed = False
    for obj in objects:
        if not isinstance(obj, dict) or obj.get(_MALFORMED_LINE):
            saw_malformed = True
            continue
        obj_type = obj.get("type")
        if obj_type is not None and not isinstance(obj_type, str):
            # A non-string, non-null "type" (e.g. a list) cannot be
            # trusted as a discriminator, and is also unhashable, so it
            # must never reach `obj_type in FAILURE_TYPES` below.
            saw_malformed = True
            continue
        if obj_type in FAILURE_TYPES:
            return "error"
        if obj_type == CLAUDE_TERMINAL_TYPE and obj.get("is_error") is True:
            return "error"

    if saw_malformed:
        return "malformed"

    for obj in objects:
        obj_type = obj.get("type")
        if obj_type == CODEX_SUCCESS_TYPE:
            return "success"
        if obj_type == CLAUDE_TERMINAL_TYPE and obj.get("is_error") is False:
            return "success"

    return None


def _parse_observed_model(stdout: str) -> Optional[str]:
    """Always returns None.

    This is not a stub awaiting a better parser: it is the correct answer
    given what the local, offline evidence above actually shows. Claude's
    terminal `result` envelope carries no `"model"` field at all — the
    model name lives on a separate, earlier `"system"` init message
    (`SDKSystemMessage`, confirmed via the same binary `strings` pass:
    `["system":["model":"` appears adjacent in the shipped strings table),
    which is not a terminal event and is not parsed here. Codex's `Turn`
    schema (dumped locally, see above) exposed no trustworthy per-turn
    model field either. A bare top-level `"model"` key on an arbitrary
    JSON line is not validated provider evidence — a tool-call payload, a
    config echo, or the model's own output could carry one — so treating
    it as observed_model would let output spoof this field, exactly what
    this function exists to prevent.

    REMAINING GAP: confirming the true field (if any) that carries actual
    executed-model metadata, for either CLI, requires a real invocation
    (a live pilot) to inspect the full, real event stream end to end —
    static/offline evidence only shows what is NOT there. That live-pilot
    proof step is out of scope for this session (no model calls
    permitted) and remains open; see RESULT.md.
    """
    return None


@dataclass(frozen=True)
class DispatchOutcome:
    receipt_path: Path
    receipt: "receipt.Receipt"


def dispatch(
    task_text: str,
    task_id: str,
    workdir: str,
    receipt_dir: Path,
    lock_dir: Path,
    usage_state_path: Path,
    guard_state_path: Optional[Path] = None,
    override: Optional[str] = None,
    execute: bool = False,
    astra_approved_by: Optional[str] = None,
    runner: Optional[Runner] = None,
    help_runner: Optional[cli_help.Runner] = None,
    auth_runner: Optional[auth.Runner] = None,
) -> DispatchOutcome:
    """Run the full pipeline for one task. Returns the written receipt.

    Raises DispatchBlocked for any safety-gate failure (graphic requests,
    astra without approval, a nonexistent workdir, guard/usage/auth
    failures, or an active-task lock conflict). A DispatchBlocked receipt
    is still written before raising.

    The resource-state guard (router.guard) AND the usage-state check
    (router.usage) are both checked for every lane, including an
    already-approved astra lane: astra's named-approval requirement is an
    ADDITIONAL gate on top of usage validation, never a substitute for it.
    Astra is never exempt from any gate here.
    """
    result = classify.classify(task_text, override=override)
    lane = result.lane

    def _blocked(reason: str) -> DispatchOutcome:
        r = receipt.Receipt(
            task_id=task_id,
            workdir=workdir,
            lane=lane,
            requested_model=LANE_SPECS.get(lane).model if lane in LANE_SPECS else "n/a",
            observed_model=None,
            argv=[],
            dry_run=not execute,
            status="blocked",
            returncode=None,
            stdout="",
            stderr="",
            reason=reason,
        )
        path = receipt.write_receipt(receipt_dir, r)
        raise DispatchBlocked(reason, path)

    if lane == classify.LANE_MANUAL_GRAPHIC:
        return _blocked(
            "graphic request: stays in interactive native Figma; no worker dispatched"
        )

    if lane == classify.LANE_ASTRA:
        if not astra_approved_by or not astra_approved_by.strip():
            return _blocked(
                "astra lane requires explicit named approval (astra_approved_by); none given"
            )

    if not Path(workdir).is_dir():
        return _blocked(f"workdir does not exist or is not a directory: {workdir}")

    # Active-task protection: atomic, scoped by canonical workdir (any
    # task_id racing on the same workdir conflicts). Only protects
    # concurrent dispatches made through this router; see router.lock
    # module docstring for the external-worker caveat.
    try:
        task_lock = lock.TaskLock(lock_dir, task_id, workdir)
        task_lock.__enter__()
    except lock.ActiveTaskError as exc:
        return _blocked(f"active-task lock conflict: {exc}")

    try:
        spec = LANE_SPECS[lane]

        effective_guard_path = guard_state_path or (
            Path.home() / ".local/state/charlie-session-guard/status.json"
        )
        guard_result = guard.check_guard(effective_guard_path)
        if not guard_result.ok:
            return _blocked(f"guard check failed: {guard_result.reason}")

        # Usage validation applies to EVERY lane, astra included. Astra's
        # named-approval requirement (checked above) is an additional gate,
        # never a replacement for this one.
        usage_result = usage.check_usage(lane, usage_state_path)
        if not usage_result.ok:
            return _blocked(f"usage check failed: {usage_result.reason}")

        argv, stdin_input = build_argv(lane, task_text, workdir)

        if not execute:
            # Dry-run spawns NO subprocess at all: the auth check
            # (`claude auth status` / `codex login status`) and the help
            # check (`<binary> [exec] --help`) are both real subprocess
            # calls, so they are deliberately skipped here and only run
            # under --execute. A dry-run receipt therefore reflects
            # classify/lock/workdir/guard/usage gates only; auth and CLI
            # flags are unverified preflight, not a pass.
            r = receipt.Receipt(
                task_id=task_id,
                workdir=workdir,
                lane=lane,
                requested_model=spec.model,
                observed_model=None,
                argv=argv,
                dry_run=True,
                status="dry_run",
                returncode=None,
                stdout="",
                stderr="",
                reason=(
                    "dry-run: no subprocess invoked; auth and CLI-help checks "
                    "are deferred to --execute and are unverified here"
                ),
            )
            path = receipt.write_receipt(receipt_dir, r)
            return DispatchOutcome(path, r)

        # --execute: run every remaining gate, including the two that
        # spawn a real subprocess, before touching the actual dispatch.
        if spec.binary == "claude":
            auth_result = auth.check_claude_auth(runner=auth_runner)
        elif spec.binary == "codex":
            auth_result = auth.check_codex_auth(runner=auth_runner)
        else:
            return _blocked(f"no auth check defined for binary {spec.binary!r}")
        if not auth_result.ok:
            return _blocked(f"auth check failed: {auth_result.reason}")

        help_result = cli_help.check_flags_present(
            spec.help_argv, spec.required_flags, runner=help_runner
        )
        if not help_result.ok:
            return _blocked(f"CLI help check failed: {help_result.reason}")

        run = runner or _default_runner
        try:
            proc = run(argv, input_text=stdin_input, cwd=workdir)
        except (FileNotFoundError, OSError) as exc:
            r = receipt.Receipt(
                task_id=task_id,
                workdir=workdir,
                lane=lane,
                requested_model=spec.model,
                observed_model=None,
                argv=argv,
                dry_run=False,
                status="error",
                returncode=None,
                stdout="",
                stderr=str(exc),
                reason=f"subprocess invocation failed: {exc}",
            )
            path = receipt.write_receipt(receipt_dir, r)
            return DispatchOutcome(path, r)
        except subprocess.TimeoutExpired as exc:
            # TimeoutExpired.stdout/.output and .stderr are real stdlib
            # attributes carrying whatever was captured before the kill;
            # preserve them (decoded safely) rather than discarding them
            # into a stringified exception message.
            # exc.stdout is a property alias for exc.output (same stdlib
            # attribute under two names); read via .stdout for clarity.
            partial_stdout = _decode(exc.stdout)
            partial_stderr = _decode(exc.stderr)
            r = receipt.Receipt(
                task_id=task_id,
                workdir=workdir,
                lane=lane,
                requested_model=spec.model,
                observed_model=_parse_observed_model(partial_stdout),
                argv=argv,
                dry_run=False,
                status="error",
                returncode=None,
                stdout=partial_stdout,
                stderr=partial_stderr,
                reason=f"subprocess timed out after {exc.timeout}s; partial output preserved",
            )
            path = receipt.write_receipt(receipt_dir, r)
            return DispatchOutcome(path, r)

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        objects = _parse_structured_envelope(stdout)
        terminal = _terminal_result(objects)

        if terminal == "error":
            status = "error"
            reason = (
                "provider reported a structured terminal failure "
                "(turn.failed/type=error, or a result with is_error=true), "
                "even though the process exit code may be 0; no automatic "
                "retry or fallback"
            )
        elif terminal == "malformed":
            status = "error"
            reason = (
                "structured output contained an unparseable line or an "
                "untrustworthy event 'type' field; a malformed/truncated "
                "stream is never marked ok even if a valid completion event "
                "also appeared in it"
            )
        elif proc.returncode != 0:
            status = "error"
            reason = f"provider exited {proc.returncode}; no automatic retry or fallback"
        elif terminal == "success":
            status = "ok"
            reason = "dispatch completed"
        else:
            # Exit 0, but no recognised TERMINAL success event was found —
            # either nothing parsed at all, or only lifecycle/progress
            # events (e.g. thread.started/turn.started) with no
            # turn.completed / result-success to follow them. Structured
            # output was explicitly requested (--json / --output-format
            # json); an incomplete or unrecognised stream is unverified,
            # never claimed as "ok" just because the exit code was clean.
            status = "error"
            reason = (
                "provider exited 0 but no recognised terminal success event "
                "(turn.completed, or result with is_error=false) was found in "
                "the structured output; cannot verify success, so this is not "
                "marked ok"
            )

        r = receipt.Receipt(
            task_id=task_id,
            workdir=workdir,
            lane=lane,
            requested_model=spec.model,
            observed_model=_parse_observed_model(stdout),
            argv=argv,
            dry_run=False,
            status=status,
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            reason=reason,
        )
        path = receipt.write_receipt(receipt_dir, r)
        return DispatchOutcome(path, r)
    finally:
        task_lock.__exit__(None, None, None)
