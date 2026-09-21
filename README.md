# codex-task-router

Deterministic local router for a **new** task: it classifies the task text
into a lane using a fixed keyword table — never a model's judgement — and,
if every safety gate passes, dispatches it to the installed `codex` or
`claude` CLI. Python standard library only, no runtime dependencies.

Source: [charlie947/codex-task-router](https://github.com/charlie947/codex-task-router).

**What this is not:** installing this package does not make any AI
assistant, agent, or "composer" session start calling it automatically.
This is a standalone CLI you (or a script) invoke explicitly, one task at
a time. Some environments separately maintain their own instruction/skill
files that tell an assistant *when* to invoke a command like
`codex-route` — that kind of skill is not part of this package, is not
installed by it, and is not described here; this repository is the router
CLI only.

## Scope

- Routes **new** tasks only. There is no pre-turn hook and no automatic
  interception of a running composer session — this is an explicit CLI
  entrypoint you run yourself.
- Never makes a model call to classify a task. Classification is
  keyword-based string matching.
- Never calls a provider to check usage. It reads a JSON file you (or an
  existing native usage tool) keep current, and fails closed if that file
  is missing, stale, or reports the lane as exhausted/unsafe/unavailable.
  See `router/usage.py` for the exact trust boundary and freshness rule.
- Never calls a provider to check machine load. It reads the real
  charlie-session-guard resource-state file
  (`~/.local/state/charlie-session-guard/status.json` by default,
  overridable with `--guard-state`) and fails closed for **every** lane,
  including an approved `astra`, if that file is missing, stale,
  malformed, timestamped in the future, or reports
  `defer_new_heavy_work: true`. See `router/guard.py`.
- Never falls back to an API key, a base-URL override, a cloud-provider
  override (Bedrock/Vertex/Foundry), or an auth-token override, for any
  lane. If any of `ANTHROPIC_API_KEY`, `CLAUDE_API_KEY`,
  `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, `CLAUDE_CODE_USE_BEDROCK`,
  `CLAUDE_CODE_USE_VERTEX`, `CLAUDE_CODE_USE_FOUNDRY`,
  `ANTHROPIC_VERTEX_PROJECT_ID`, `CLOUD_ML_REGION`,
  `AWS_BEARER_TOKEN_BEDROCK`, `OPENAI_API_KEY`, `OPENAI_BASE_URL` or
  `CODEX_API_KEY` is set, dispatch is blocked outright. See
  `router/auth.py`. A provider override configured through a *config
  file* rather than an env var is not introspectable from here and is a
  real, documented remaining trust boundary — this router does not claim
  a hard cost cap.
- Dry-run by default everywhere. Nothing is actually executed, and no
  auth/CLI-help subprocess is spawned either, unless you pass `--execute`
  (those two checks are real subprocess calls, so dry-run genuinely never
  spawns a subprocess of any kind).
- Every dispatch attempt — dry-run, blocked, error, or ok — writes an
  immutable (read-only) JSON receipt recording the requested model, the
  observed model (if one could be parsed back), argv, stdout/stderr,
  status and reason.

## Lanes

| Lane | Trigger | Model |
| --- | --- | --- |
| `luna` | routine quick question / small edit (default fallback) | `gpt-5.6-luna`, effort medium |
| `terra` | difficult but bounded build/debug/review | `gpt-5.6-terra`, effort medium |
| `claude` | bulk research, drafting or implementation | subscription Claude, `sonnet`, effort medium |
| `astra` | unusual high-risk reasoning | `gpt-6-astra`, requires an explicit `--astra-approved-by NAME` AND passes the identical usage-state check every other lane passes — named approval is an additional gate, never a usage-check exemption |
| `manual_graphic` | any graphic/infographic/Figma-shaped request | never dispatched — always returns a manual-route instruction |

Classification precedence (first match wins, checked in this order):
graphic keywords → high-risk keywords → bulk keywords → difficult-build
keywords → default (`luna`). A graphic classification can never be
overridden into a worker dispatch; `--override` is checked after the
graphic check.

## Install

From a clean checkout of the repository, in a virtual environment:

```bash
git clone https://github.com/charlie947/codex-task-router.git
cd codex-task-router
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
codex-route --help
```

No-install alternative — run the module directly from a checkout, no pip
step required (stdlib-only package):
```bash
cd codex-task-router
python3 -m router.cli --help
```

The console-script name is `codex-route`, not `route` — `route` collides
with the system network `route` command on macOS/Linux and would shadow
or be shadowed by it. Every example below uses `codex-route`; a bare
`route` in a shell invokes the network tool, not this router.

The no-install path is exercised by
`tests/test_router.py::InstallFromCleanDirTests`, which copies `router/`
into a fresh temporary directory and runs `python3 -m router.cli --help`
there — a module-level smoke check. The editable-install path's actual
package installability (pyproject.toml metadata, `[project.scripts]`
entry point) is exercised for real by `InstalledInIsolatedVenvTests`,
which builds a throwaway venv and runs a genuine `pip install -e .` in
it, not just a directory copy.

## Prerequisite: you supply the usage and guard state files

Before dispatch will pass its gates, you (or your own existing tooling)
must create and keep current the `--usage-state` and `--guard-state` JSON
files described below. This router does not fetch, generate, or refresh
either file itself, and does not integrate with any provider's usage
dashboard automatically — there is no hidden native integration doing
this for you. Without a fresh, valid file at each path, dispatch fails
closed (blocked), by design.

## Usage

```bash
# Dry-run (default): classifies, checks every non-subprocess gate
# (workdir exists, active-task lock, resource-state guard, usage state)
# and writes a receipt, but never spawns a subprocess — auth and CLI-flag
# checks are real subprocess calls and are deferred to --execute.
codex-route "quick question about this function" \
  --task-id demo-1 \
  --workdir /path/to/project \
  --usage-state ~/.codex-task-router/usage-state.json \
  --guard-state ~/.local/state/charlie-session-guard/status.json

# Actually dispatch (requires --execute; this is the only mode that runs
# the auth check, the CLI-help check, and the real subprocess):
codex-route "debug the bounded build failure" \
  --task-id demo-2 \
  --workdir /path/to/project \
  --usage-state ~/.codex-task-router/usage-state.json \
  --guard-state ~/.local/state/charlie-session-guard/status.json \
  --execute

# Force a lane explicitly (never overrides a graphic classification):
codex-route "some task" --task-id demo-3 --workdir . --override terra

# High-risk lane requires a named approver (approval does not exempt any
# other gate — guard, usage, auth, and help checks all still apply):
codex-route "this touches the production database" \
  --task-id demo-4 --workdir . \
  --astra-approved-by "Charlie Hills"
```

### Usage-state file

`--usage-state` points at a JSON file this router treats as the only
source of truth for whether ordinary usage is available per lane. Example:

```json
{
  "generated_at": "2026-09-21T09:00:00Z",
  "paid_fallback_allowed": false,
  "lanes": {
    "luna": {"available": true, "exhausted": false, "unsafe": false},
    "terra": {"available": true, "exhausted": false, "unsafe": false},
    "claude": {"available": true, "exhausted": false, "unsafe": false},
    "astra": {"available": true, "exhausted": false, "unsafe": false}
  }
}
```

Only the `lanes` entry for the lane actually being dispatched is read and
validated — `check_usage(lane, ...)` looks up `lanes[lane]` alone, so you
do not need every lane present in the file, only the one(s) you actually
route to. `astra`'s old usage-check exemption was deleted: if you dispatch
to `astra`, its entry is checked exactly like any other lane's, and named
human approval on `astra` is an *additional* gate on top of this check,
never a substitute for it. The example above shows all four lanes for
completeness, not because all four are required in every file.
`generated_at` must
be a non-empty string timestamp; `paid_fallback_allowed` must be present
and a strict JSON `false` (missing, `true`, or a non-boolean value all
block — this is a **host-maintained attestation**, not something this
router can verify against a provider account; it never claims a real
cost cap). `available`/`exhausted`/`unsafe` must each be a strict JSON
boolean — a string `"false"`, a `0`/`1`, or `null` is rejected as
malformed rather than coerced by truthiness (Python's `bool("false")` is
`True`, which is exactly the mistake this router does not make). A
non-object top-level JSON body (e.g. `[]`) fails closed instead of
crashing. If the file is missing, unreadable, invalid JSON, older than
**5 minutes** (default; tightened from 6 hours), or reports a lane as
`exhausted`/`unsafe`/not `available`, dispatch to that lane is blocked.
This router does not fetch this file from any provider API and does not
invent a cost cap; populating and refreshing it is the caller's
responsibility.

### Guard-state file

`--guard-state` points at the real charlie-session-guard resource-state
file (default `~/.local/state/charlie-session-guard/status.json`, an
existing file this router does not create or maintain). Only two fields
are required and validated; the file may carry other informational
fields, which are ignored:

```json
{
  "at": 1789912909.80599,
  "defer_new_heavy_work": false
}
```

`at` must be a finite numeric unix timestamp (a JSON boolean or string is
rejected, not coerced) and not older than **120 seconds** (default;
tightened from 15 minutes) or in the future. `defer_new_heavy_work` must
be a strict JSON boolean; if it is `true`, or missing, or the wrong type,
dispatch is blocked. This check runs for every lane, including an
already-approved `astra` — the human approval on `astra` never exempts
any other gate, guard or usage.

### Auth checks

Before dispatching under `--execute`, the router checks authentication
using the selected CLI:

- **`claude` lane:** runs `claude auth status --json` and requires
  `loggedIn: true`, `authMethod: "claude.ai"`,
  `subscriptionType: "max"`, `apiProvider: "firstParty"` in the parsed
  JSON.
- **`luna`/`terra`/`astra` lanes (all `codex`):** runs
  `codex login status` (this subcommand has no `--json` flag, verified
  via `codex login status --help`) and requires the text to report a
  ChatGPT login, not an API-key login.

Neither check proves paid overage is disabled — that is a separate,
unverified condition this tool does not claim to check, and a config-file
override (rather than an env var) is not introspectable from here at
all; see the Scope section above. No secret value is ever printed in a
failure reason. These are real subprocess calls, so they run under
`--execute` only; a dry-run receipt does not verify auth.

Also under `--execute`, both binaries are asked for **structured output**
(`claude --output-format json`, `codex --json`) and the response is
parsed as either a single JSON object or JSONL. Status is only ever `ok`
when a genuine TERMINAL completion event is found (`type: "turn.completed"`
for codex, or `type: "result"` with `is_error: false` for claude) — a
lifecycle event alone (`thread.started`, `turn.started`), a `turn.failed`
or `type: "error"` event, a line that fails to parse as JSON at all, or an
untrustworthy `type` field (e.g. not a string) all mark the dispatch as
`error`, even at exit code `0`, and never crash the router. A real
successful stream that also happens to carry an unrelated, valid warning
line (codex's own `item.error` lifecycle events are one observed example)
is still read correctly as a success — only a line the parser genuinely
cannot make sense of, or the absence of any real completion event,
downgrades the result.

**`observed_model` is currently `null`.** The router does not extract model
identity from provider output. The pilots were corroborated separately:
Codex's persisted local `turn_context` records the selected model and
medium effort; Claude's real JSON result contains
`modelUsage["claude-sonnet-5"].canonicalModel` and `provider: "firstParty"`.
Those sources are recorded in [PILOT-EVIDENCE.md](PILOT-EVIDENCE.md).
The Codex record is local CLI metadata, not server-side attestation.
Do not interpret a null receipt field as an independently verified model.

## Development

```bash
cd codex-task-router
python3 -m unittest tests.test_router -v
```

128 tests, stdlib `unittest`, fully offline (no network, no real
`codex`/`claude` binary call) — CLI and subprocess calls are injected via
fake runner callables in the test file itself.

### Release validation (separate, needs network)

`InstalledInIsolatedVenvTests` does a real `pip install -e .` in a
throwaway venv to verify actual package installability. It is skipped by
default so the fast run above never touches the network or fetches
tooling; run it explicitly for release validation:

```bash
CODEX_ROUTER_RUN_INSTALL_TEST=1 python3 -m unittest tests.test_router.InstalledInIsolatedVenvTests -v
```

It needs network only to bootstrap a modern pip/setuptools/wheel into
the throwaway venv, and self-skips with a clear reason if that bootstrap
step is unavailable rather than reporting a false pass or fail.

## Active-task lock caveat

The lock in `router/lock.py` is keyed by **canonical workdir only**, not
by task_id — two different task_ids racing on the identical workdir now
correctly conflict (an earlier version kept task_id in the lock filename
itself, so two different task_ids on the same workdir computed two
different lock paths and both succeeded concurrently; that was a real
bug, fixed). `task_id` is still recorded inside the lock file's contents
for diagnostics. It only protects concurrent dispatches made *through
this router*. It has no visibility into, and does not discover, any
other worker — a different terminal, a different tool, a manually run
`codex`/`claude` session — writing to the same workdir outside this
router; detecting that remains a manual host responsibility this module
does not and cannot automate. It also never clears a stale lock
automatically: a lock left behind by a crashed process must be removed
by a human on purpose, not guessed safe by this tool. The lock path
itself is derived with a stable hash (`hashlib.sha256`), not Python's
per-process-randomized `hash()`, so two processes racing on the
identical workdir cannot compute two different lock paths and both
succeed — verified both by a same-process equal-path check and by a real
two-OS-process contention test
(`LockTests.test_real_cross_process_contention`).

## Non-goals (out of scope for this package)

- No subagents, no automatic model dispatch beyond the single lane chosen
  per invocation, no graphics/design worker.
- No cost-savings or performance claim of any kind. Three small, real
  dispatch pilots (documented in [PILOT-EVIDENCE.md](PILOT-EVIDENCE.md))
  prove that a `luna`, a `terra`, and a `claude` dispatch each ran, with
  the requested model and effort, and produced a working output — nothing
  about speed, quality, or price relative to any alternative.
- No git push, no Notion, no messaging, no billing changes.
