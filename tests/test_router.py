"""Unittest suite for the codex-task-router package.

Offline by default: no real codex/claude binary call, no model invocation,
no network access. Subprocess behaviour is injected via fake runner
callables.

RELEASE VALIDATION vs FAST UNIT RUN: `InstalledInIsolatedVenvTests` does a
real `pip install -e .` in a throwaway venv and needs network to bootstrap
a modern pip/setuptools/wheel into it. It is skipped by default so the
ordinary fast run (`python3 -m unittest tests.test_router`) never touches
the network. Opt in for release validation with:
    CODEX_ROUTER_RUN_INSTALL_TEST=1 python3 -m unittest tests.test_router -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from router import auth, classify, cli_help, dispatch, guard, lock, receipt, usage  # noqa: E402


def fake_proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def make_usage_state(tmp: Path, lanes=None, generated_at=None, paid_fallback_allowed=False, filename="usage-state.json"):
    lanes = lanes or {
        "luna": {"available": True, "exhausted": False, "unsafe": False},
        "terra": {"available": True, "exhausted": False, "unsafe": False},
        "claude": {"available": True, "exhausted": False, "unsafe": False},
        "astra": {"available": True, "exhausted": False, "unsafe": False},
    }
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    body = {"generated_at": generated_at, "paid_fallback_allowed": paid_fallback_allowed, "lanes": lanes}
    path = tmp / filename
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def make_guard_state(tmp: Path, at=None, defer=False, filename="guard-state.json", extra=None):
    at = at if at is not None else datetime.now(timezone.utc).timestamp()
    body = {"at": at, "level": 1, "defer_new_heavy_work": defer}
    if extra:
        body.update(extra)
    path = tmp / filename
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def structured_ok_json(model="gpt-5.6-luna"):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "model": model})


# A single fake help blob containing every flag token any lane's
# required_flags checks for, so one runner works for every lane in tests
# that don't care about the exact help text.
FAKE_HELP_TEXT = (
    "usage: fake\n"
    "  -m, --model MODEL\n"
    "  -c, --config KEY=VALUE\n"
    "  -C, --cd DIR\n"
    "  --json\n"
    "  -p, --print\n"
    "  --effort LEVEL\n"
    "  --output-format FORMAT\n"
)


def ok_help_runner(argv):
    return fake_proc(0, stdout=FAKE_HELP_TEXT)


def ok_auth_runner(argv):
    """Handles both `claude auth status --json` and `codex login status`."""
    if argv[:2] == ["claude", "auth"]:
        payload = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        }
        return fake_proc(0, stdout=json.dumps(payload))
    if argv[:2] == ["codex", "login"]:
        return fake_proc(0, stdout="Logged in using ChatGPT\n")
    return fake_proc(1, stderr="unrecognized auth probe in test\n")


class ClassifyTests(unittest.TestCase):
    def test_routine_default(self):
        result = classify.classify("quick question about this function")
        self.assertEqual(result.lane, classify.LANE_LUNA)

    def test_routine_small_edit(self):
        result = classify.classify("small edit: rename this variable")
        self.assertEqual(result.lane, classify.LANE_LUNA)

    def test_default_fallback_is_luna(self):
        result = classify.classify("look at this thing please")
        self.assertEqual(result.lane, classify.LANE_LUNA)

    def test_difficult_bounded_build(self):
        result = classify.classify("debug this bounded build failure")
        self.assertEqual(result.lane, classify.LANE_TERRA)

    def test_bulk_research(self):
        result = classify.classify("bulk research on competitor pricing")
        self.assertEqual(result.lane, classify.LANE_CLAUDE)

    def test_bulk_implementation(self):
        result = classify.classify("full implementation of the new pipeline")
        self.assertEqual(result.lane, classify.LANE_CLAUDE)

    def test_high_risk_astra(self):
        result = classify.classify("this touches the production database directly")
        self.assertEqual(result.lane, classify.LANE_ASTRA)

    def test_graphic_hard_stop(self):
        result = classify.classify("build an infographic for LinkedIn")
        self.assertEqual(result.lane, classify.LANE_MANUAL_GRAPHIC)

    def test_precedence_graphic_over_bulk(self):
        result = classify.classify("bulk research then build an infographic")
        self.assertEqual(result.lane, classify.LANE_MANUAL_GRAPHIC)

    def test_precedence_high_risk_over_bulk(self):
        result = classify.classify("bulk research on how to drop table safely")
        self.assertEqual(result.lane, classify.LANE_ASTRA)

    def test_empty_task_raises(self):
        with self.assertRaises(ValueError):
            classify.classify("   ")

    def test_explicit_override(self):
        result = classify.classify("quick question", override=classify.LANE_CLAUDE)
        self.assertEqual(result.lane, classify.LANE_CLAUDE)
        self.assertTrue(result.overridden)

    def test_override_cannot_beat_graphic(self):
        result = classify.classify("make an infographic", override=classify.LANE_CLAUDE)
        self.assertEqual(result.lane, classify.LANE_MANUAL_GRAPHIC)

    def test_invalid_override_rejected(self):
        with self.assertRaises(ValueError):
            classify.classify("quick question", override="not-a-lane")


class UsageTests(unittest.TestCase):
    def test_missing_file_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.json"
            result = usage.check_usage("luna", missing)
            self.assertFalse(result.ok)

    def test_stale_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
            path = make_usage_state(Path(td), generated_at=old)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("stale", result.reason)

    def test_five_minute_freshness_window(self):
        # Correction #2: max age reduced to 300s. A state 6 minutes old
        # must block; one just inside 300s must not.
        with tempfile.TemporaryDirectory() as td:
            too_old = (datetime.now(timezone.utc) - timedelta(seconds=360)).isoformat()
            path = make_usage_state(Path(td), generated_at=too_old, filename="too-old.json")
            self.assertFalse(usage.check_usage("luna", path).ok)

            fresh_enough = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
            path2 = make_usage_state(Path(td), generated_at=fresh_enough, filename="fresh.json")
            self.assertTrue(usage.check_usage("luna", path2).ok)

    def test_exhausted_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = {"luna": {"available": True, "exhausted": True, "unsafe": False}}
            path = make_usage_state(Path(td), lanes=lanes)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("exhausted", result.reason)

    def test_unsafe_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = {"luna": {"available": True, "exhausted": False, "unsafe": True}}
            path = make_usage_state(Path(td), lanes=lanes)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("unsafe", result.reason)

    def test_unavailable_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = {"luna": {"available": False, "exhausted": False, "unsafe": False}}
            path = make_usage_state(Path(td), lanes=lanes)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)

    def test_missing_lane_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_usage_state(Path(td), lanes={"terra": {"available": True, "exhausted": False, "unsafe": False}})
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)

    def test_astra_lane_validated_like_any_other(self):
        # Correction #1: astra's usage exemption is deleted entirely.
        with tempfile.TemporaryDirectory() as td:
            path = make_usage_state(
                Path(td),
                lanes={"astra": {"available": True, "exhausted": True, "unsafe": False}},
            )
            result = usage.check_usage("astra", path)
            self.assertFalse(result.ok)
            self.assertIn("exhausted", result.reason)

            path2 = make_usage_state(
                Path(td),
                lanes={"astra": {"available": True, "exhausted": False, "unsafe": False}},
                filename="astra-ok.json",
            )
            self.assertTrue(usage.check_usage("astra", path2).ok)

    def test_invalid_json_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)

    def test_non_object_top_level_blocks_without_crash(self):
        # Correction #2: a JSON array (or any non-object) must fail closed,
        # not raise AttributeError on the first .get() call.
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "list.json"
            path.write_text("[]", encoding="utf-8")
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)

            path2 = Path(td) / "scalar.json"
            path2.write_text("42", encoding="utf-8")
            result2 = usage.check_usage("luna", path2)
            self.assertFalse(result2.ok)

    def test_fresh_available_ok(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_usage_state(Path(td))
            result = usage.check_usage("luna", path)
            self.assertTrue(result.ok)

    def test_string_boolean_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = {"luna": {"available": True, "exhausted": "false", "unsafe": False}}
            path = make_usage_state(Path(td), lanes=lanes)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("boolean", result.reason)

    def test_list_value_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = {"luna": {"available": [True], "exhausted": False, "unsafe": False}}
            path = make_usage_state(Path(td), lanes=lanes)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("boolean", result.reason)

    def test_null_value_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            lanes = {"luna": {"available": True, "exhausted": None, "unsafe": False}}
            path = make_usage_state(Path(td), lanes=lanes)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("boolean", result.reason)

    def test_generated_at_as_number_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "num.json"
            path.write_text(
                json.dumps({
                    "generated_at": 1234567890,
                    "paid_fallback_allowed": False,
                    "lanes": {"luna": {"available": True, "exhausted": False, "unsafe": False}},
                }),
                encoding="utf-8",
            )
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("string", result.reason)

    def test_paid_fallback_allowed_missing_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "no-pfa.json"
            path.write_text(
                json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "lanes": {"luna": {"available": True, "exhausted": False, "unsafe": False}},
                }),
                encoding="utf-8",
            )
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)
            self.assertIn("paid_fallback_allowed", result.reason)

    def test_paid_fallback_allowed_true_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_usage_state(Path(td), paid_fallback_allowed=True)
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)

    def test_paid_fallback_allowed_wrong_type_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "str-pfa.json"
            path.write_text(
                json.dumps({
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "paid_fallback_allowed": "false",
                    "lanes": {"luna": {"available": True, "exhausted": False, "unsafe": False}},
                }),
                encoding="utf-8",
            )
            result = usage.check_usage("luna", path)
            self.assertFalse(result.ok)

    def test_paid_fallback_allowed_false_ok(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_usage_state(Path(td), paid_fallback_allowed=False)
            self.assertTrue(usage.check_usage("luna", path).ok)


class GuardTests(unittest.TestCase):
    def test_missing_file_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.json"
            result = guard.check_guard(missing)
            self.assertFalse(result.ok)

    def test_fresh_safe_ok(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_guard_state(Path(td), defer=False)
            result = guard.check_guard(path)
            self.assertTrue(result.ok)

    def test_defer_true_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_guard_state(Path(td), defer=True)
            result = guard.check_guard(path)
            self.assertFalse(result.ok)
            self.assertIn("defer_new_heavy_work", result.reason)

    def test_120_second_freshness_window(self):
        # Correction #2: guard freshness reduced to 120s.
        with tempfile.TemporaryDirectory() as td:
            too_old = (datetime.now(timezone.utc) - timedelta(seconds=180)).timestamp()
            path = make_guard_state(Path(td), at=too_old, filename="too-old.json")
            self.assertFalse(guard.check_guard(path).ok)

            fresh_enough = (datetime.now(timezone.utc) - timedelta(seconds=30)).timestamp()
            path2 = make_guard_state(Path(td), at=fresh_enough, filename="fresh.json")
            self.assertTrue(guard.check_guard(path2).ok)

    def test_future_at_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            future_at = (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()
            path = make_guard_state(Path(td), at=future_at)
            result = guard.check_guard(path)
            self.assertFalse(result.ok)
            self.assertIn("future", result.reason)

    def test_bool_at_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bool-at.json"
            path.write_text(json.dumps({"at": True, "defer_new_heavy_work": False}), encoding="utf-8")
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_string_at_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "str-at.json"
            path.write_text(json.dumps({"at": "now", "defer_new_heavy_work": False}), encoding="utf-8")
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_nonfinite_at_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "nan-at.json"
            path.write_text('{"at": NaN, "defer_new_heavy_work": false}', encoding="utf-8")
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_missing_defer_field_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "no-defer.json"
            path.write_text(json.dumps({"at": datetime.now(timezone.utc).timestamp()}), encoding="utf-8")
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_string_defer_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "str-defer.json"
            path.write_text(
                json.dumps({"at": datetime.now(timezone.utc).timestamp(), "defer_new_heavy_work": "false"}),
                encoding="utf-8",
            )
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_invalid_json_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_non_object_blocks(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "list.json"
            path.write_text("[1,2,3]", encoding="utf-8")
            result = guard.check_guard(path)
            self.assertFalse(result.ok)

    def test_real_schema_shape_with_extra_fields_ok(self):
        with tempfile.TemporaryDirectory() as td:
            path = make_guard_state(
                Path(td),
                extra={
                    "pressure_since": None,
                    "busy_since": None,
                    "load_1m": 7.7,
                    "cpus": 14,
                    "sample_source": "manual_or_hook",
                    "largest_processes": [{"pid": 1, "rss_mb": 10.0, "executable": "x"}],
                },
            )
            result = guard.check_guard(path)
            self.assertTrue(result.ok)


class AuthTests(unittest.TestCase):
    def test_claude_ok_when_full_first_party_max(self):
        result = auth.check_claude_auth(runner=ok_auth_runner, env={})
        self.assertTrue(result.ok)

    def test_claude_blocks_on_wrong_subscription_type(self):
        def bad_runner(argv):
            payload = {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "firstParty",
                "subscriptionType": "pro",
            }
            return fake_proc(0, stdout=json.dumps(payload))

        result = auth.check_claude_auth(runner=bad_runner, env={})
        self.assertFalse(result.ok)

    def test_claude_blocks_on_non_first_party_provider(self):
        def bad_runner(argv):
            payload = {
                "loggedIn": True,
                "authMethod": "claude.ai",
                "apiProvider": "bedrock",
                "subscriptionType": "max",
            }
            return fake_proc(0, stdout=json.dumps(payload))

        result = auth.check_claude_auth(runner=bad_runner, env={})
        self.assertFalse(result.ok)

    def test_claude_blocks_on_invalid_json(self):
        def bad_runner(argv):
            return fake_proc(0, stdout="not json")

        result = auth.check_claude_auth(runner=bad_runner, env={})
        self.assertFalse(result.ok)

    def test_claude_blocks_on_api_key_env(self):
        result = auth.check_claude_auth(runner=ok_auth_runner, env={"ANTHROPIC_API_KEY": "x"})
        self.assertFalse(result.ok)
        self.assertIn("ANTHROPIC_API_KEY", result.reason)

    def test_claude_blocks_on_base_url_override(self):
        result = auth.check_claude_auth(runner=ok_auth_runner, env={"ANTHROPIC_BASE_URL": "https://example.com"})
        self.assertFalse(result.ok)

    def test_claude_blocks_on_foundry_override(self):
        result = auth.check_claude_auth(runner=ok_auth_runner, env={"CLAUDE_CODE_USE_FOUNDRY": "1"})
        self.assertFalse(result.ok)
        self.assertIn("CLAUDE_CODE_USE_FOUNDRY", result.reason)

    def test_claude_blocks_on_missing_binary(self):
        def missing_runner(argv):
            raise FileNotFoundError("no such file")

        result = auth.check_claude_auth(runner=missing_runner, env={})
        self.assertFalse(result.ok)

    def test_claude_blocks_on_nonzero_exit(self):
        def bad_runner(argv):
            return fake_proc(1, stderr="not logged in\n")

        result = auth.check_claude_auth(runner=bad_runner, env={})
        self.assertFalse(result.ok)

    def test_codex_ok_when_chatgpt(self):
        result = auth.check_codex_auth(runner=ok_auth_runner, env={})
        self.assertTrue(result.ok)

    def test_codex_blocks_on_api_key_login(self):
        def bad_runner(argv):
            return fake_proc(0, stdout="Logged in using an API key\n")

        result = auth.check_codex_auth(runner=bad_runner, env={})
        self.assertFalse(result.ok)

    def test_codex_blocks_on_not_logged_in(self):
        def bad_runner(argv):
            return fake_proc(0, stdout="Not logged in\n")

        result = auth.check_codex_auth(runner=bad_runner, env={})
        self.assertFalse(result.ok)

    def test_codex_blocks_on_openai_api_key_env(self):
        result = auth.check_codex_auth(runner=ok_auth_runner, env={"OPENAI_API_KEY": "x"})
        self.assertFalse(result.ok)
        self.assertIn("OPENAI_API_KEY", result.reason)

    def test_codex_blocks_on_openai_base_url_override(self):
        result = auth.check_codex_auth(runner=ok_auth_runner, env={"OPENAI_BASE_URL": "https://example.com"})
        self.assertFalse(result.ok)

    def test_codex_blocks_on_missing_binary(self):
        def missing_runner(argv):
            raise FileNotFoundError("no such file")

        result = auth.check_codex_auth(runner=missing_runner, env={})
        self.assertFalse(result.ok)


class CliHelpTests(unittest.TestCase):
    def test_flags_present(self):
        result = cli_help.check_flags_present(("codex", "exec", "--help"), ("--model", "-m"), runner=ok_help_runner)
        self.assertTrue(result.ok)

    def test_missing_flag_blocks(self):
        def runner(argv):
            return fake_proc(0, stdout="usage: only --model here\n")

        result = cli_help.check_flags_present(("codex", "exec", "--help"), ("--model", "--effort"), runner=runner)
        self.assertFalse(result.ok)
        self.assertIn("--effort", result.reason)

    def test_missing_binary_blocks(self):
        def runner(argv):
            raise FileNotFoundError("nope")

        result = cli_help.check_flags_present(("codex", "exec", "--help"), ("--model",), runner=runner)
        self.assertFalse(result.ok)

    def test_uses_exact_help_argv_not_just_binary(self):
        seen = []

        def runner(argv):
            seen.append(list(argv))
            return fake_proc(0, stdout=FAKE_HELP_TEXT)

        cli_help.check_flags_present(("codex", "exec", "--help"), ("-m",), runner=runner)
        self.assertEqual(seen, [["codex", "exec", "--help"]])

    def test_nonzero_exit_blocks_even_when_flags_mentioned(self):
        # Correction #5: a crash/error exit must not be trusted just
        # because the required flag substrings happen to appear in
        # whatever partial output came out (e.g. an error message that
        # happens to mention "--model").
        def runner(argv):
            return fake_proc(1, stdout="--model", stderr="error")

        result = cli_help.check_flags_present(("codex", "exec", "--help"), ("--model",), runner=runner)
        self.assertFalse(result.ok)
        self.assertIn("exited 1", result.reason)


class LockTests(unittest.TestCase):
    def test_lock_then_conflict(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            with lock.TaskLock(lock_dir, "task-1", "/tmp/work-a"):
                with self.assertRaises(lock.ActiveTaskError):
                    with lock.TaskLock(lock_dir, "task-1", "/tmp/work-a"):
                        pass

    def test_lock_released_after_context(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            with lock.TaskLock(lock_dir, "task-2", "/tmp/work-b"):
                pass
            with lock.TaskLock(lock_dir, "task-2", "/tmp/work-b"):
                pass

    def test_different_task_ids_same_workdir_conflict(self):
        # Reversed from the earlier (incorrect) assertion that different
        # task_ids on the same workdir do not collide. Correction #4: the
        # lock protects the WORKDIR; two different task_ids racing on the
        # identical workdir must conflict, not silently coexist.
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            with lock.TaskLock(lock_dir, "task-a", "/tmp/work-c"):
                with self.assertRaises(lock.ActiveTaskError):
                    with lock.TaskLock(lock_dir, "task-b", "/tmp/work-c"):
                        pass

    def test_different_workdirs_do_not_collide(self):
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            with lock.TaskLock(lock_dir, "task-a", "/tmp/work-e"):
                with lock.TaskLock(lock_dir, "task-a", "/tmp/work-f"):
                    pass

    def test_empty_task_id_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                lock.TaskLock(Path(td), "", "/tmp/work-d")

    def test_lock_path_stable_across_hash_seeds(self):
        # router.lock previously derived the lock filename with Python's
        # builtin hash(), which is randomized per-process (PYTHONHASHSEED)
        # unless explicitly disabled. Two processes with different seeds
        # must compute the identical lock path for the same workdir, or
        # the O_CREAT|O_EXCL lock can be silently bypassed.
        helper = (
            "import sys; sys.path.insert(0, %r); "
            "from router.lock import _lock_path; "
            "from pathlib import Path; "
            "print(_lock_path(Path('/tmp/lockdir'), '/tmp/some-workdir'))"
        ) % str(REPO_ROOT)

        def run_with_seed(seed: str) -> str:
            proc = subprocess.run(
                [sys.executable, "-c", helper],
                capture_output=True,
                text=True,
                timeout=20,
                env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return proc.stdout.strip()

        path_seed_1 = run_with_seed("1")
        path_seed_2 = run_with_seed("2")
        self.assertEqual(path_seed_1, path_seed_2)

    def test_real_cross_process_contention(self):
        # Correction #4: a real two-process contention test, not just an
        # equal-hash-path assertion. A separate OS process acquires the
        # lock on a shared workdir and holds it; this process, running
        # concurrently, must fail to acquire the same workdir under a
        # different task_id.
        with tempfile.TemporaryDirectory() as td:
            lock_dir = Path(td) / "locks"
            workdir = Path(td) / "shared-workdir"
            workdir.mkdir()
            ready_file = Path(td) / "ready"
            stop_file = Path(td) / "stop"

            helper_script = (
                "import sys, time\n"
                "sys.path.insert(0, %r)\n"
                "from pathlib import Path\n"
                "from router.lock import TaskLock\n"
                "lock_dir, task_id, workdir, ready_file, stop_file = sys.argv[1:6]\n"
                "with TaskLock(Path(lock_dir), task_id, workdir):\n"
                "    Path(ready_file).write_text('ready')\n"
                "    deadline = time.time() + 10\n"
                "    while not Path(stop_file).exists() and time.time() < deadline:\n"
                "        time.sleep(0.02)\n"
            ) % str(REPO_ROOT)

            proc = subprocess.Popen(
                [
                    sys.executable, "-c", helper_script,
                    str(lock_dir), "holder-process", str(workdir), str(ready_file), str(stop_file),
                ],
            )
            try:
                deadline = time.time() + 5
                while not ready_file.exists() and time.time() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready_file.exists(), "holder process never acquired the lock")

                with self.assertRaises(lock.ActiveTaskError):
                    with lock.TaskLock(lock_dir, "contender-in-this-process", str(workdir)):
                        pass
            finally:
                stop_file.write_text("stop")
                proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0)


class ReceiptTests(unittest.TestCase):
    def test_receipt_is_immutable_and_separates_requested_observed(self):
        with tempfile.TemporaryDirectory() as td:
            r = receipt.Receipt(
                task_id="t1",
                workdir="/tmp/x",
                lane="luna",
                requested_model="gpt-5.6-luna",
                observed_model=None,
                argv=["codex", "exec"],
                dry_run=True,
                status="dry_run",
                returncode=None,
                stdout="",
                stderr="",
                reason="dry-run",
            )
            path = receipt.write_receipt(Path(td), r)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["requested_model"], "gpt-5.6-luna")
            self.assertIsNone(data["observed_model"])
            self.assertFalse(path.stat().st_mode & 0o222)
            with self.assertRaises(OSError):
                path.write_text("tampered", encoding="utf-8")


class ObservedModelTests(unittest.TestCase):
    def test_model_field_never_extracted_even_from_valid_structured_json(self):
        # Reversed from the prior (incorrect) assertion that a bare
        # top-level "model" field on ANY parsed JSON object was trustworthy
        # observed_model evidence. Correction (envelope pass): local,
        # offline inspection of the installed Claude Code binary confirms
        # the terminal "result" envelope carries no "model" field at all
        # (model lives on an earlier, non-terminal "system" init message);
        # codex's Turn schema (dumped locally via
        # `codex app-server generate-json-schema`, no model call) showed no
        # trustworthy per-turn model field either. A bare top-level "model"
        # key on an arbitrary object is therefore never validated evidence,
        # even inside an otherwise well-formed terminal-success envelope —
        # observed_model is always None until a real pilot invocation
        # confirms an actual trustworthy field to read.
        stdout = json.dumps({"type": "result", "is_error": False, "model": "gpt-5.6-luna"})
        self.assertIsNone(dispatch._parse_observed_model(stdout))

        stdout2 = "\n".join([
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "turn.completed", "model": "gpt-5.6-terra"}),
        ])
        self.assertIsNone(dispatch._parse_observed_model(stdout2))

    def test_free_text_never_spoofs_observed_model(self):
        # Correction #3: generated answer text must never be treated as
        # provider evidence, even if it contains a "model:" marker.
        self.assertIsNone(dispatch._parse_observed_model("The answer says model: gpt-6-astra"))
        self.assertIsNone(dispatch._parse_observed_model("model: gpt-5.6-luna\nok\n"))

    def test_empty_stdout_returns_none(self):
        self.assertIsNone(dispatch._parse_observed_model(""))

    def test_terminal_result_claude_error_is_error_true(self):
        objs = dispatch._parse_structured_envelope(
            json.dumps({"type": "result", "is_error": True, "subtype": "error_during_execution"})
        )
        self.assertEqual(dispatch._terminal_result(objs), "error")

    def test_terminal_result_generic_type_error(self):
        objs = dispatch._parse_structured_envelope(json.dumps({"type": "error", "message": "boom"}))
        self.assertEqual(dispatch._terminal_result(objs), "error")

    def test_terminal_result_claude_success(self):
        objs = dispatch._parse_structured_envelope(
            json.dumps({"type": "result", "subtype": "success", "is_error": False})
        )
        self.assertEqual(dispatch._terminal_result(objs), "success")

    def test_terminal_result_codex_turn_completed_success(self):
        objs = dispatch._parse_structured_envelope(json.dumps({"type": "turn.completed", "threadId": "t1"}))
        self.assertEqual(dispatch._terminal_result(objs), "success")

    def test_terminal_result_codex_turn_failed(self):
        objs = dispatch._parse_structured_envelope(
            json.dumps({"type": "turn.failed", "error": {"message": "limit reached"}})
        )
        self.assertEqual(dispatch._terminal_result(objs), "error")

    def test_terminal_result_lifecycle_only_is_not_success(self):
        # A thread.started / turn.started event alone (no completion event
        # ever arrives) must NOT be read as success just because it is
        # valid, error-free JSON.
        objs = dispatch._parse_structured_envelope(json.dumps({"type": "thread.started", "thread_id": "t1"}))
        self.assertIsNone(dispatch._terminal_result(objs))

        objs2 = dispatch._parse_structured_envelope(json.dumps({"type": "turn.started"}))
        self.assertIsNone(dispatch._terminal_result(objs2))

    def test_terminal_result_arbitrary_json_is_not_success(self):
        objs = dispatch._parse_structured_envelope(json.dumps({"foo": "bar"}))
        self.assertIsNone(dispatch._terminal_result(objs))

    def test_terminal_result_empty_objects_is_none(self):
        self.assertIsNone(dispatch._terminal_result([]))

    def test_terminal_result_failure_anywhere_in_stream_wins(self):
        # A failure earlier in a JSONL stream must not be overridden by a
        # later line that happens to claim success.
        objs = dispatch._parse_structured_envelope("\n".join([
            json.dumps({"type": "turn.failed", "error": {"message": "boom"}}),
            json.dumps({"type": "turn.completed"}),
        ]))
        self.assertEqual(dispatch._terminal_result(objs), "error")

    def test_non_json_stdout_parses_to_malformed_marker(self):
        # A single unparseable line is recorded as a malformed marker, not
        # silently dropped to an empty list — _terminal_result needs it to
        # fail closed rather than see "no evidence, but also no problem".
        objs = dispatch._parse_structured_envelope("not json at all")
        self.assertEqual(objs, [{"__malformed_line__": True}])
        self.assertEqual(dispatch._terminal_result(objs), "malformed")

    def test_malformed_trailing_line_after_valid_completion_is_not_success(self):
        # Reversed from the earlier (incorrect) assertion that a malformed
        # line could be silently skipped while a real completion event
        # elsewhere in the same stream still counted as success. A
        # truncated/corrupted tail after a genuine turn.completed must not
        # be read as a clean success (matches the parent release check's
        # exact reproduction: '{"type":"turn.completed"}\nBROKEN').
        stdout = "\n".join([
            json.dumps({"type": "turn.completed"}),
            "BROKEN",
        ])
        objs = dispatch._parse_structured_envelope(stdout)
        result = dispatch._terminal_result(objs)
        self.assertNotEqual(result, "success")
        self.assertEqual(result, "malformed")

    def test_wrong_type_discriminator_does_not_crash(self):
        # type: [] is valid JSON but an untrustworthy discriminator (also
        # unhashable, so a naive `in FAILURE_TYPES` set check crashes with
        # TypeError). Must fail closed, never crash.
        objs = dispatch._parse_structured_envelope(json.dumps({"type": []}))
        result = dispatch._terminal_result(objs)
        self.assertNotEqual(result, "success")
        self.assertEqual(result, "malformed")

    def test_codex_item_error_warning_coexists_with_real_success(self):
        # Real codex pilot streams carry item.type=error warning lines
        # alongside a genuine turn.completed — a valid-but-unrecognised
        # "type" string must NOT be treated as malformed or as a failure;
        # only a line that fails to parse at all, or has a non-string
        # "type", downgrades the result.
        stdout = "\n".join([
            json.dumps({"type": "item.error", "message": "a tool warning, not a fatal error"}),
            json.dumps({"type": "turn.completed"}),
        ])
        objs = dispatch._parse_structured_envelope(stdout)
        self.assertEqual(dispatch._terminal_result(objs), "success")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.receipt_dir = self.tmp / "receipts"
        self.lock_dir = self.tmp / "locks"
        self.usage_state = make_usage_state(self.tmp)
        self.guard_state = make_guard_state(self.tmp)
        self.calls = []
        self.help_calls = []
        self.auth_calls = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def counting_runner(self, argv, input_text=None, cwd=None):
        self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
        return fake_proc(0, stdout=structured_ok_json())

    def counting_help_runner(self, argv):
        self.help_calls.append(list(argv))
        return fake_proc(0, stdout=FAKE_HELP_TEXT)

    def counting_auth_runner(self, argv):
        self.auth_calls.append(list(argv))
        return ok_auth_runner(argv)

    def dispatch_kwargs(self, **overrides):
        base = dict(
            receipt_dir=self.receipt_dir,
            lock_dir=self.lock_dir,
            usage_state_path=self.usage_state,
            guard_state_path=self.guard_state,
            runner=self.counting_runner,
            help_runner=self.counting_help_runner,
            auth_runner=self.counting_auth_runner,
        )
        base.update(overrides)
        return base

    def test_dry_run_never_calls_any_subprocess(self):
        outcome = dispatch.dispatch(
            task_text="quick question about x",
            task_id="dr-1",
            workdir=str(self.tmp),
            execute=False,
            **self.dispatch_kwargs(),
        )
        self.assertEqual(outcome.receipt.status, "dry_run")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.help_calls, [])
        self.assertEqual(self.auth_calls, [])

    def test_exact_argv_and_stdin_luna(self):
        argv, stdin_input = dispatch.build_argv(classify.LANE_LUNA, "do a small thing", "/tmp/work")
        self.assertEqual(
            argv,
            ["codex", "exec", "-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="medium"', "-C", "/tmp/work", "--json", "-"],
        )
        self.assertEqual(stdin_input, "do a small thing")

    def test_exact_argv_and_stdin_terra(self):
        argv, stdin_input = dispatch.build_argv(classify.LANE_TERRA, "debug the bounded build", "/tmp/work")
        self.assertEqual(
            argv,
            ["codex", "exec", "-m", "gpt-5.6-terra", "-c", 'model_reasoning_effort="medium"', "-C", "/tmp/work", "--json", "-"],
        )
        self.assertEqual(stdin_input, "debug the bounded build")

    def test_exact_argv_and_stdin_astra(self):
        argv, stdin_input = dispatch.build_argv(classify.LANE_ASTRA, "touch prod db", "/tmp/work")
        self.assertEqual(
            argv,
            ["codex", "exec", "-m", "gpt-6-astra", "-c", 'model_reasoning_effort="medium"', "-C", "/tmp/work", "--json", "-"],
        )
        self.assertEqual(stdin_input, "touch prod db")

    def test_exact_argv_claude_double_dash_separated(self):
        argv, stdin_input = dispatch.build_argv(classify.LANE_CLAUDE, "bulk research task", "/tmp/work")
        self.assertEqual(
            argv,
            ["claude", "-p", "--model", "sonnet", "--effort", "medium", "--output-format", "json", "--", "bulk research task"],
        )
        self.assertIsNone(stdin_input)

    def test_claude_argv_task_starting_with_dash_cannot_be_parsed_as_option(self):
        argv, _ = dispatch.build_argv(classify.LANE_CLAUDE, "--dangerously-skip-permissions", "/tmp/work")
        self.assertEqual(argv[-2], "--")
        self.assertEqual(argv[-1], "--dangerously-skip-permissions")

    def test_codex_task_starting_with_dash_goes_over_stdin_not_argv(self):
        argv, stdin_input = dispatch.build_argv(classify.LANE_LUNA, "--dangerously-bypass-approvals-and-sandbox", "/tmp/work")
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertEqual(stdin_input, "--dangerously-bypass-approvals-and-sandbox")

    def test_graphic_never_dispatches(self):
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="build an infographic",
                task_id="g-1",
                workdir=str(self.tmp),
                execute=True,
                **self.dispatch_kwargs(),
            )
        self.assertIn("graphic", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_astra_without_approval_blocks(self):
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="this touches the production database",
                task_id="a-1",
                workdir=str(self.tmp),
                execute=True,
                **self.dispatch_kwargs(),
            )
        self.assertIn("astra", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_astra_with_approval_dry_run_ok(self):
        outcome = dispatch.dispatch(
            task_text="this touches the production database",
            task_id="a-2",
            workdir=str(self.tmp),
            execute=False,
            astra_approved_by="Charlie Hills",
            **self.dispatch_kwargs(),
        )
        self.assertEqual(outcome.receipt.lane, classify.LANE_ASTRA)
        self.assertEqual(outcome.receipt.status, "dry_run")

    def test_astra_approval_does_not_exempt_guard(self):
        stale_guard = make_guard_state(
            self.tmp,
            at=(datetime.now(timezone.utc) - timedelta(hours=2)).timestamp(),
            filename="stale-guard.json",
        )
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="this touches the production database",
                task_id="a-3",
                workdir=str(self.tmp),
                execute=False,
                astra_approved_by="Charlie Hills",
                **self.dispatch_kwargs(guard_state_path=stale_guard),
            )
        self.assertIn("guard", str(ctx.exception))

    def test_astra_approval_does_not_exempt_usage(self):
        # Correction #1: the old astra-skips-usage exemption is deleted.
        # Approval is checked, usage is checked separately, and a missing
        # astra usage entry must block even with a named approver.
        exhausted_astra = make_usage_state(
            self.tmp,
            lanes={"astra": {"available": True, "exhausted": True, "unsafe": False}},
            filename="astra-exhausted.json",
        )
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="this touches the production database",
                task_id="a-4",
                workdir=str(self.tmp),
                execute=False,
                astra_approved_by="Charlie Hills",
                **self.dispatch_kwargs(usage_state_path=exhausted_astra),
            )
        self.assertIn("usage", str(ctx.exception))

    def test_override_explicit(self):
        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="o-1",
            workdir=str(self.tmp),
            override=classify.LANE_TERRA,
            execute=False,
            **self.dispatch_kwargs(),
        )
        self.assertEqual(outcome.receipt.lane, classify.LANE_TERRA)

    def test_nonexistent_workdir_blocks(self):
        missing_workdir = str(self.tmp / "does-not-exist")
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question",
                task_id="w-1",
                workdir=missing_workdir,
                execute=False,
                **self.dispatch_kwargs(),
            )
        self.assertIn("workdir", str(ctx.exception))

    def test_execute_runs_in_workdir_cwd(self):
        outcome = dispatch.dispatch(
            task_text="quick question about y",
            task_id="cwd-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(),
        )
        self.assertEqual(outcome.receipt.status, "ok")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["cwd"], str(self.tmp))

    def test_active_task_protection_blocks_second_dispatch(self):
        held = lock.TaskLock(self.lock_dir, "dup-task", str(self.tmp))
        held.__enter__()
        try:
            with self.assertRaises(dispatch.DispatchBlocked) as ctx:
                dispatch.dispatch(
                    task_text="quick question",
                    task_id="different-task-id-same-workdir",
                    workdir=str(self.tmp),
                    execute=False,
                    **self.dispatch_kwargs(),
                )
            self.assertIn("lock", str(ctx.exception))
        finally:
            held.__exit__(None, None, None)

    def test_missing_usage_state_blocks(self):
        missing = self.tmp / "does-not-exist.json"
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question",
                task_id="u-1",
                workdir=str(self.tmp),
                execute=False,
                **self.dispatch_kwargs(usage_state_path=missing),
            )
        self.assertIn("usage", str(ctx.exception))

    def test_exhausted_usage_blocks(self):
        exhausted_state = make_usage_state(
            self.tmp,
            lanes={"luna": {"available": True, "exhausted": True, "unsafe": False}},
            filename="exhausted.json",
        )
        with self.assertRaises(dispatch.DispatchBlocked):
            dispatch.dispatch(
                task_text="quick question",
                task_id="u-2",
                workdir=str(self.tmp),
                execute=False,
                **self.dispatch_kwargs(usage_state_path=exhausted_state),
            )

    def test_missing_guard_state_blocks(self):
        missing = self.tmp / "no-guard.json"
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question",
                task_id="gd-1",
                workdir=str(self.tmp),
                execute=False,
                **self.dispatch_kwargs(guard_state_path=missing),
            )
        self.assertIn("guard", str(ctx.exception))

    def test_defer_new_heavy_work_blocks(self):
        deferring = make_guard_state(self.tmp, defer=True, filename="defer.json")
        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question",
                task_id="gd-2",
                workdir=str(self.tmp),
                execute=False,
                **self.dispatch_kwargs(guard_state_path=deferring),
            )
        self.assertIn("guard", str(ctx.exception))

    def test_bad_claude_auth_blocks_claude_lane_under_execute(self):
        def bad_auth_runner(argv):
            return fake_proc(0, stdout=json.dumps({"loggedIn": False}))

        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="bulk research the market",
                task_id="au-1",
                workdir=str(self.tmp),
                execute=True,
                **self.dispatch_kwargs(auth_runner=bad_auth_runner),
            )
        self.assertIn("auth", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_bad_codex_auth_blocks_codex_lane_under_execute(self):
        def bad_auth_runner(argv):
            return fake_proc(0, stdout="Logged in using an API key\n")

        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question about auth",
                task_id="au-2",
                workdir=str(self.tmp),
                execute=True,
                **self.dispatch_kwargs(auth_runner=bad_auth_runner),
            )
        self.assertIn("auth", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_auth_not_checked_in_dry_run(self):
        def exploding_auth_runner(argv):
            raise AssertionError("auth runner must not be called on dry-run")

        outcome = dispatch.dispatch(
            task_text="bulk research the market",
            task_id="au-3",
            workdir=str(self.tmp),
            execute=False,
            **self.dispatch_kwargs(auth_runner=exploding_auth_runner),
        )
        self.assertEqual(outcome.receipt.status, "dry_run")

    def test_execute_true_runs_injected_runner_and_writes_receipt(self):
        outcome = dispatch.dispatch(
            task_text="quick question about y",
            task_id="ex-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(),
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(outcome.receipt.status, "ok")
        # observed_model is always None: no field either CLI's terminal
        # envelope carries is trustworthy provider evidence of the actual
        # model that ran (see _parse_observed_model docstring).
        self.assertIsNone(outcome.receipt.observed_model)
        self.assertEqual(outcome.receipt.requested_model, "gpt-5.6-luna")
        self.assertEqual(self.calls[0]["input_text"], "quick question about y")

    def test_provider_error_no_retry(self):
        def failing_runner(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            return fake_proc(1, stderr="rate limit exceeded\n")

        outcome = dispatch.dispatch(
            task_text="quick question about z",
            task_id="err-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=failing_runner),
        )
        self.assertEqual(outcome.receipt.status, "error")
        self.assertEqual(outcome.receipt.returncode, 1)
        self.assertEqual(len(self.calls), 1)

    def test_structured_error_marks_status_error_even_at_exit_0(self):
        # Correction #6, matching the parent regression's
        # zero_exit_provider_error check: a `type: result, is_error: true`
        # (or `type: error`) structured event must be a real error even
        # when the process exit code is 0.
        def zero_exit_but_structured_error(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            return fake_proc(0, stdout=json.dumps({
                "type": "result", "is_error": True, "subtype": "error_during_execution",
            }))

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="serr-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=zero_exit_but_structured_error),
        )
        self.assertEqual(outcome.receipt.status, "error")

    def test_malformed_output_at_exit_0_is_not_claimed_ok(self):
        # Correction #6: unknown malformed execute output must not claim
        # verified successful output just because the exit code was 0.
        def zero_exit_garbage_output(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            return fake_proc(0, stdout="not structured output at all")

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="garbage-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=zero_exit_garbage_output),
        )
        self.assertEqual(outcome.receipt.status, "error")

    def test_incomplete_stream_not_success_end_to_end(self):
        # Matches the parent v2 regression's incomplete_stream_not_success
        # check exactly: a lone thread.started event, valid JSON, no error
        # flagged anywhere — must NOT be read as success.
        def only_lifecycle_event(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            return fake_proc(0, stdout=json.dumps({"type": "thread.started", "thread_id": "test"}))

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="incomplete-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=only_lifecycle_event),
        )
        self.assertEqual(outcome.receipt.status, "error")

    def test_codex_turn_failed_not_success_end_to_end(self):
        # Matches the parent v2 regression's failed_turn_not_success check
        # exactly: a real-shaped codex turn.failed event at exit 0.
        def turn_failed(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            return fake_proc(0, stdout=json.dumps({"type": "turn.failed", "error": {"message": "limit reached"}}))

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="turnfailed-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=turn_failed),
        )
        self.assertEqual(outcome.receipt.status, "error")

    def test_codex_turn_completed_is_success_end_to_end(self):
        def turn_completed(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            return fake_proc(0, stdout=json.dumps({"type": "turn.completed", "threadId": "t1"}))

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="turncompleted-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=turn_completed),
        )
        self.assertEqual(outcome.receipt.status, "ok")
        self.assertIsNone(outcome.receipt.observed_model)
        self.assertIsNone(outcome.receipt.observed_model)

    def test_timeout_preserves_partial_output(self):
        def timing_out_runner(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            raise subprocess.TimeoutExpired(
                cmd=list(argv), timeout=600,
                output="partial stdout before kill",
                stderr="partial stderr before kill",
            )

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="to-1",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=timing_out_runner),
        )
        self.assertEqual(outcome.receipt.status, "error")
        self.assertEqual(outcome.receipt.stdout, "partial stdout before kill")
        self.assertEqual(outcome.receipt.stderr, "partial stderr before kill")
        self.assertIn("timed out", outcome.receipt.reason)

    def test_timeout_decodes_bytes_output_safely(self):
        def timing_out_bytes_runner(argv, input_text=None, cwd=None):
            self.calls.append({"argv": list(argv), "input_text": input_text, "cwd": cwd})
            raise subprocess.TimeoutExpired(
                cmd=list(argv), timeout=600,
                output=b"partial bytes stdout",
                stderr=b"partial bytes stderr",
            )

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="to-2",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(runner=timing_out_bytes_runner),
        )
        self.assertEqual(outcome.receipt.status, "error")
        self.assertEqual(outcome.receipt.stdout, "partial bytes stdout")
        self.assertEqual(outcome.receipt.stderr, "partial bytes stderr")

    def test_missing_flag_in_help_blocks_under_execute(self):
        def thin_help_runner(argv):
            return fake_proc(0, stdout="usage: only --model\n")

        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question",
                task_id="hf-1",
                workdir=str(self.tmp),
                execute=True,
                **self.dispatch_kwargs(help_runner=thin_help_runner),
            )
        self.assertIn("CLI help", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_help_nonzero_exit_blocks_under_execute(self):
        def crashing_help_runner(argv):
            return fake_proc(1, stdout=FAKE_HELP_TEXT, stderr="boom")

        with self.assertRaises(dispatch.DispatchBlocked) as ctx:
            dispatch.dispatch(
                task_text="quick question",
                task_id="hf-4",
                workdir=str(self.tmp),
                execute=True,
                **self.dispatch_kwargs(help_runner=crashing_help_runner),
            )
        self.assertIn("CLI help", str(ctx.exception))
        self.assertEqual(self.calls, [])

    def test_help_not_checked_in_dry_run(self):
        def exploding_help_runner(argv):
            raise AssertionError("help runner must not be called on dry-run")

        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="hf-2",
            workdir=str(self.tmp),
            execute=False,
            **self.dispatch_kwargs(help_runner=exploding_help_runner),
        )
        self.assertEqual(outcome.receipt.status, "dry_run")

    def test_codex_help_check_uses_exec_subcommand(self):
        outcome = dispatch.dispatch(
            task_text="quick question",
            task_id="hf-3",
            workdir=str(self.tmp),
            execute=True,
            **self.dispatch_kwargs(),
        )
        self.assertEqual(outcome.receipt.status, "ok")
        self.assertEqual(self.help_calls, [["codex", "exec", "--help"]])


class InstallFromCleanDirTests(unittest.TestCase):
    def test_module_runs_from_a_copied_clean_directory(self):
        with tempfile.TemporaryDirectory() as td:
            import shutil

            dest = Path(td) / "codex-task-router"
            shutil.copytree(REPO_ROOT / "router", dest / "router")
            proc = subprocess.run(
                [sys.executable, "-m", "router.cli", "--help"],
                cwd=str(dest),
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("route", proc.stdout.lower())


@unittest.skipUnless(
    os.environ.get("CODEX_ROUTER_RUN_INSTALL_TEST") == "1",
    "release-validation only; needs network to bootstrap a modern pip/setuptools/wheel "
    "into a throwaway venv. Opt in with CODEX_ROUTER_RUN_INSTALL_TEST=1 so the ordinary "
    "fast unit run never touches the network.",
)
class InstalledInIsolatedVenvTests(unittest.TestCase):
    """A real package-install check, not just a module copy + --help.

    Creates a throwaway venv, actually runs `pip install -e .` against
    this repo inside it, and confirms the `codex-route` console script
    both exists and runs. Gated behind CODEX_ROUTER_RUN_INSTALL_TEST=1
    (see module docstring) so it is release-validation, not part of the
    fast offline unit run.
    """

    def test_editable_install_in_fresh_venv(self):
        with tempfile.TemporaryDirectory() as td:
            venv_dir = Path(td) / "venv"
            proc = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            venv_python = venv_dir / "bin" / "python"
            bootstrap = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if bootstrap.returncode != 0:
                self.skipTest(
                    "could not bootstrap a modern pip/setuptools/wheel into the "
                    "throwaway venv (likely no network in this environment); "
                    "not a router defect. pip output:\n"
                    + bootstrap.stdout[-2000:]
                    + bootstrap.stderr[-2000:]
                )

            install = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "-e", str(REPO_ROOT)],
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(install.returncode, 0, install.stdout[-2000:] + install.stderr[-2000:])

            script = venv_dir / "bin" / "codex-route"
            self.assertTrue(script.exists(), f"codex-route console script missing at {script}")

            run = subprocess.run(
                [str(script), "--help"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("route", run.stdout.lower())


if __name__ == "__main__":
    unittest.main()
