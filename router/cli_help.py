"""Inspect installed codex/claude CLI --help output for exact flags.

This never invokes a model. It only runs the CLI's own --help subcommand
(argv arrays, no shell) and checks that the flags this router intends to
use are documented in that output. If an expected flag is missing, this
module stops rather than guessing a flag name.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]


@dataclass(frozen=True)
class HelpCheckResult:
    ok: bool
    reason: str
    help_text: str = ""


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=20,
        shell=False,
    )


def check_flags_present(
    help_argv: Sequence[str],
    required_flags: Sequence[str],
    runner: Optional[Runner] = None,
) -> HelpCheckResult:
    """Run `help_argv` (e.g. ("codex", "exec", "--help"), not necessarily the
    top-level binary's own --help) and confirm every flag in required_flags
    appears in the output. No model call happens here.

    help_argv must be the exact subcommand whose help documents the flags
    this router intends to use — top-level `codex --help` does not list
    `codex exec`'s flags (-m/--model, -c/--config, -C/--cd live under
    `codex exec --help`, verified 2026-09-20).
    """
    runner = runner or _default_runner
    label = " ".join(help_argv)
    try:
        result = runner(list(help_argv))
    except (FileNotFoundError, OSError) as exc:
        return HelpCheckResult(False, f"could not run '{label}': {exc}")
    except subprocess.TimeoutExpired:
        return HelpCheckResult(False, f"'{label}' timed out")

    help_text = (result.stdout or "") + (result.stderr or "")

    # A nonzero exit means this CLI is not reliably reporting its own help,
    # even if the expected flag substrings happen to appear somewhere in
    # partial/error output (e.g. a crash traceback that happens to mention
    # "--model"). Do not trust flag substrings from a failed invocation.
    if result.returncode != 0:
        return HelpCheckResult(
            False,
            f"'{label}' exited {result.returncode}; help output is not trusted",
            help_text=help_text,
        )
    missing = [flag for flag in required_flags if flag not in help_text]
    if missing:
        return HelpCheckResult(
            False,
            f"'{label}' does not document expected flag(s): {missing}",
            help_text=help_text,
        )
    return HelpCheckResult(True, f"all expected flags present for '{label}'", help_text=help_text)
