"""Subscription-auth validation for both lanes' binaries.

No API key, base-URL override, cloud-provider override, or auth-token
override is permitted for ANY lane, codex or claude. A subscription/ChatGPT
login alone is not proof overage is disabled, so these checks only confirm
the CLI reports the expected first-party subscription session; they never
claim to prove billing overage is off. No secret values are ever printed;
only field names and lane-relevant status are included in failure reasons.

REMAINING TRUST BOUNDARY: this module can only see and block environment
variables. A provider override configured through a *config file* instead
(e.g. a `model_provider`/base-URL entry in `~/.codex/config.toml`, or an
equivalent Claude settings file) is not introspectable from here and is
NOT detected or blocked by this module. That gap is a real, documented
host trust boundary, not something this router claims to close — do not
read the presence of `check_claude_auth`/`check_codex_auth` as a hard
cost cap; it isn't one.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess"]

# Applies to every lane, regardless of binary: API keys, base-URL overrides,
# cloud-provider overrides (Bedrock/Vertex), and auth-token overrides are all
# blocked. This is a superset of the old claude-only list.
BLOCKED_ENV_VARS = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
    "AWS_BEARER_TOKEN_BEDROCK",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX_API_KEY",
)


@dataclass(frozen=True)
class AuthCheckResult:
    ok: bool
    reason: str


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=20,
        shell=False,
    )


def _env_override_reason(env: dict) -> Optional[str]:
    for var in BLOCKED_ENV_VARS:
        if env.get(var):
            return f"blocked: {var} is set; API-key/provider overrides are not permitted"
    return None


def check_claude_auth(
    runner: Optional[Runner] = None,
    env: Optional[dict] = None,
) -> AuthCheckResult:
    """Block dispatch unless the CLI reports a first-party claude.ai/max
    subscription session (parsed from `claude auth status --json`, not a
    free-form substring match) and no override env var is present.
    """
    env = os.environ if env is None else env
    override_reason = _env_override_reason(env)
    if override_reason:
        return AuthCheckResult(False, override_reason)

    runner = runner or _default_runner
    try:
        result = runner(["claude", "auth", "status", "--json"])
    except (FileNotFoundError, OSError) as exc:
        return AuthCheckResult(False, f"could not run 'claude auth status --json': {exc}")
    except subprocess.TimeoutExpired:
        return AuthCheckResult(False, "'claude auth status --json' timed out")

    if result.returncode != 0:
        return AuthCheckResult(
            False, f"'claude auth status --json' exited {result.returncode}"
        )

    try:
        data = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        return AuthCheckResult(False, f"'claude auth status --json' output is not valid JSON: {exc}")

    if not isinstance(data, dict):
        return AuthCheckResult(False, "'claude auth status --json' did not return a JSON object")

    if data.get("loggedIn") is not True:
        return AuthCheckResult(False, "claude auth status reports loggedIn is not true")
    if data.get("authMethod") != "claude.ai":
        return AuthCheckResult(False, "claude auth status authMethod is not 'claude.ai'")
    if data.get("subscriptionType") != "max":
        return AuthCheckResult(False, "claude auth status subscriptionType is not 'max'")
    if data.get("apiProvider") != "firstParty":
        return AuthCheckResult(False, "claude auth status apiProvider is not 'firstParty'")

    return AuthCheckResult(True, "claude.ai/max first-party subscription session confirmed")


def check_codex_auth(
    runner: Optional[Runner] = None,
    env: Optional[dict] = None,
) -> AuthCheckResult:
    """Block dispatch unless `codex login status` reports a ChatGPT login
    and no override env var is present.

    `codex login status` has no --json flag (verified via `codex login
    status --help`); it prints one of: "Logged in using ChatGPT", "Logged
    in using an API key", or a not-logged-in message. This checks that
    exact text rather than guessing a structured shape that does not exist.
    """
    env = os.environ if env is None else env
    override_reason = _env_override_reason(env)
    if override_reason:
        return AuthCheckResult(False, override_reason)

    runner = runner or _default_runner
    try:
        result = runner(["codex", "login", "status"])
    except (FileNotFoundError, OSError) as exc:
        return AuthCheckResult(False, f"could not run 'codex login status': {exc}")
    except subprocess.TimeoutExpired:
        return AuthCheckResult(False, "'codex login status' timed out")

    if result.returncode != 0:
        return AuthCheckResult(
            False, f"'codex login status' exited {result.returncode}"
        )

    output = (result.stdout or "") + (result.stderr or "")
    lowered = output.lower()
    if "api key" in lowered:
        return AuthCheckResult(
            False, "'codex login status' reports an API-key login; ChatGPT login is required"
        )
    if "logged in using chatgpt" not in lowered:
        return AuthCheckResult(
            False, "'codex login status' did not report a ChatGPT login"
        )

    return AuthCheckResult(True, "ChatGPT login confirmed")
