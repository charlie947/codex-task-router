"""Explicit entrypoint for routing NEW tasks. No pre-turn hook, no automatic
composer interception. This only runs when invoked directly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import classify, dispatch


DEFAULT_HOME = Path.home() / ".codex-task-router"
DEFAULT_GUARD_STATE = Path.home() / ".local/state/charlie-session-guard/status.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="route",
        description=(
            "Deterministic local router for a new task: classify it into a "
            "lane (luna/terra/claude/astra/manual_graphic) and, if allowed, "
            "dispatch it via the installed codex/claude CLI. Dry-run by "
            "default; pass --execute to actually run a subprocess."
        ),
    )
    parser.add_argument("task", help="task description text")
    parser.add_argument(
        "--task-id",
        required=True,
        help="explicit task ID, recorded as metadata inside the lock file for diagnostics only "
        "(it does not scope the lock itself; see --workdir)",
    )
    parser.add_argument(
        "--workdir",
        required=True,
        help="workdir this task owns; scopes the active-task lock on its own "
        "(canonical workdir path, not task-id) so two different task-ids racing "
        "on the same workdir correctly conflict",
    )
    parser.add_argument(
        "--override",
        choices=classify.OVERRIDABLE_LANES,
        default=None,
        help="explicitly force a lane (never overrides a graphic classification)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually run the subprocess (default is dry-run only)",
    )
    parser.add_argument(
        "--astra-approved-by",
        default=None,
        help="name of the person who explicitly approved astra-lane dispatch",
    )
    parser.add_argument(
        "--usage-state",
        type=Path,
        default=DEFAULT_HOME / "usage-state.json",
        help="path to the native usage-state JSON file (see router.usage docstring)",
    )
    parser.add_argument(
        "--guard-state",
        type=Path,
        default=DEFAULT_GUARD_STATE,
        help=(
            "path to the charlie-session-guard resource-state JSON file "
            "(see router.guard docstring); portable, override for a different host/layout"
        ),
    )
    parser.add_argument(
        "--receipt-dir",
        type=Path,
        default=DEFAULT_HOME / "receipts",
        help="directory to write immutable dispatch receipts into",
    )
    parser.add_argument(
        "--lock-dir",
        type=Path,
        default=DEFAULT_HOME / "locks",
        help="directory to hold active-task lock files",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        outcome = dispatch.dispatch(
            task_text=args.task,
            task_id=args.task_id,
            workdir=args.workdir,
            receipt_dir=args.receipt_dir,
            lock_dir=args.lock_dir,
            usage_state_path=args.usage_state,
            guard_state_path=args.guard_state,
            override=args.override,
            execute=args.execute,
            astra_approved_by=args.astra_approved_by,
        )
    except dispatch.DispatchBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    print(f"lane={outcome.receipt.lane} status={outcome.receipt.status}")
    print(f"receipt={outcome.receipt_path}")
    return 0 if outcome.receipt.status in ("ok", "dry_run") else 1


if __name__ == "__main__":
    raise SystemExit(main())
