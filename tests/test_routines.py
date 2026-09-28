import importlib.util
import json
import os
import plistlib
import signal
import subprocess
import unittest
from tempfile import TemporaryDirectory
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import mock_open, patch


SCRIPT = Path(__file__).parents[1] / "scripts" / "routines.py"
SPEC = importlib.util.spec_from_file_location("routines", SCRIPT)
ROUTINES = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(ROUTINES)


class RoutinePromptTest(unittest.TestCase):
    def test_execution_context_precedes_unchanged_routine_body(self):
        body = "First line.\n\nSecond line."

        prompt = ROUTINES.execution_prompt(body)

        self.assertEqual(
            prompt,
            f"{ROUTINES.EXECUTION_CONTEXT}\n\n{body}\n",
        )
        self.assertIn("already-installed routine", prompt)
        self.assertIn("Do not load routine-management skills", prompt)


class RoutineTimeoutTest(unittest.TestCase):
    # These tests verify launcher wiring, not the installed OpenCode runtime.
    # Runtime fault tests were performed only on 1.18.31 (2026-09-21); repeat
    # them after upgrades, especially v2, before assuming the same behavior.
    def routine(self, fields):
        with patch.object(Path, "read_text", return_value=f"---\n{fields}\n---\nReport"):
            return ROUTINES.Routine(Path("/tmp/example.md"))

    def test_model_deadlines_merge_without_clobbering_inline_configuration(self):
        routine = self.routine(
            "model: openai/example-model\nmodel_header_timeout: 60s\n"
            "model_chunk_timeout: 2m\nmodel_request_timeout: 10m"
        )
        config = {"agent": {"build": {"steps": 12}}, "provider": {
            "openai": {"options": {"baseURL": "http://localhost:9999", "timeout": False}},
            "other": {"options": {"timeout": 123}},
        }}
        original = json.dumps(config)
        with patch.dict(ROUTINES.os.environ, {"OPENCODE_CONFIG_CONTENT": original}):
            env = ROUTINES.runtime_env(routine)
            self.assertEqual(ROUTINES.os.environ["OPENCODE_CONFIG_CONTENT"], original)
        merged = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(merged["provider"]["openai"]["options"], {
            "baseURL": "http://localhost:9999", "timeout": 600000,
            "headerTimeout": 60000, "chunkTimeout": 120000,
        })
        self.assertEqual(merged["agent"], config["agent"])
        self.assertEqual(merged["provider"]["other"], config["provider"]["other"])

    def test_model_deadlines_are_opt_in(self):
        routine = self.routine("model: openai/example-model")
        with patch.dict(ROUTINES.os.environ, {}, clear=True):
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", ROUTINES.runtime_env(routine))

    def test_model_deadlines_require_provider_and_valid_duration(self):
        for fields in (
            "model_request_timeout: 10m",
            "model: example-model\nmodel_request_timeout: 10m",
            "model: openai/x\nmodel_chunk_timeout: 0",
            "model: openai/x\nmodel_header_timeout: forever",
        ):
            with self.subTest(fields=fields), self.assertRaises(ROUTINES.RoutineError):
                self.routine(fields)

    def test_invalid_inline_configuration_fails_before_starting(self):
        routine = self.routine("model: openai/x\nmodel_request_timeout: 10m")
        for value in ("not-json", "[]", '{"provider":false}',
                      '{"provider":{"openai":null}}',
                      '{"provider":{"openai":{"options":[]}}}'):
            with (
                self.subTest(value=value),
                patch.dict(ROUTINES.os.environ, {"OPENCODE_CONFIG_CONTENT": value}),
                self.assertRaises(ROUTINES.RoutineError),
            ):
                ROUTINES.runtime_env(routine)

    def test_execute_passes_model_deadlines_to_child(self):
        routine = self.routine("model: openai/x\nmodel_request_timeout: 10m")
        process = SimpleNamespace(wait=unittest.mock.Mock(return_value=0))
        with (
            patch("builtins.open", mock_open()),
            patch.dict(ROUTINES.os.environ, {}, clear=True),
            patch.object(ROUTINES.subprocess, "Popen", return_value=process) as popen,
        ):
            self.assertEqual(ROUTINES.execute_opencode(routine, Path("/tmp/prompt.md")), 0)
        env = popen.call_args.kwargs["env"]
        self.assertEqual(json.loads(env["OPENCODE_CONFIG_CONTENT"])["provider"]["openai"]["options"]["timeout"], 600000)

    def test_parse_timeout(self):
        self.assertIsNone(ROUTINES.parse_timeout(""))
        self.assertEqual(ROUTINES.parse_timeout("90"), 90)
        self.assertEqual(ROUTINES.parse_timeout("45m"), 2700)
        self.assertEqual(ROUTINES.parse_timeout("2h"), 7200)

    def test_parse_timeout_rejects_invalid_value(self):
        with self.assertRaises(ROUTINES.RoutineError):
            ROUTINES.parse_timeout("forever")

    def test_execute_opencode_terminates_process_group_on_timeout(self):
        routine = SimpleNamespace(timeout="45m", timeout_seconds=2700)
        process = SimpleNamespace(pid=1234)
        process.wait = unittest.mock.Mock(
            side_effect=[subprocess.TimeoutExpired("opencode", 2700), 0]
        )

        with (
            patch("builtins.open", mock_open()),
            patch.object(ROUTINES, "opencode_args", return_value=["run"]),
            patch.object(ROUTINES, "runtime_env", return_value={}),
            patch.object(ROUTINES.subprocess, "Popen", return_value=process),
            patch.object(ROUTINES.os, "killpg") as killpg,
            patch("builtins.print"),
        ):
            result = ROUTINES.execute_opencode(routine, Path("/tmp/prompt.md"))

        self.assertEqual(result, 124)
        killpg.assert_called_once_with(1234, signal.SIGTERM)


class RoutineSessionRoutingTest(unittest.TestCase):
    def test_session_id_of_finds_nested_pane_session(self):
        response = {
            "result": {
                "pane": {
                    "agent_session": {
                        "source": "herdr:opencode",
                        "agent": "opencode",
                        "kind": "id",
                        "value": "ses_routine_one",
                    },
                }
            }
        }

        self.assertEqual(ROUTINES.session_id_of(response), "ses_routine_one")

    def test_each_pane_resumes_its_own_run_session(self):
        routine = SimpleNamespace(name="Concurrent routine", path=Path("/tmp/concurrent.md"))

        with (
            patch.object(ROUTINES, "herdr_lock", side_effect=lambda: nullcontext()),
            patch.object(
                ROUTINES,
                "routine_tab",
                side_effect=[("tab-1", "pane-1"), ("tab-2", "pane-2")],
            ),
            patch.object(ROUTINES, "wait_for_shell"),
            patch.object(ROUTINES.subprocess, "run") as run,
        ):
            ROUTINES.run_in_herdr(routine, "RUN_ONE", 1000)
            ROUTINES.run_in_herdr(routine, "RUN_TWO", 2000)

        commands = [call.args[0][4] for call in run.call_args_list]
        self.assertIn("session-id /tmp/concurrent.md 1000 pane-1", commands[0])
        self.assertIn("session-id /tmp/concurrent.md 2000 pane-2", commands[1])
        for command in commands:
            self.assertIn('opencode --session "$SESSION_ID"', command)
            self.assertNotIn("opencode -c", command)
            self.assertNotIn("opencode --continue", command)

    def test_session_id_by_title_picks_newest_run_after_fire_time(self):
        sessions = [
            {"id": "ses_old", "title": "Nightly digest", "created": 500},
            {"id": "ses_other", "title": "Morning brief", "created": 1500},
            {"id": "ses_new", "title": "Nightly digest", "created": 1200},
            {"id": "ses_newest", "title": "Nightly digest", "created": 1400},
        ]
        completed = SimpleNamespace(stdout=json.dumps(sessions))

        with patch.object(ROUTINES.subprocess, "run", return_value=completed):
            self.assertEqual(
                ROUTINES.session_id_by_title("Nightly digest", 1000), "ses_newest"
            )
            self.assertIsNone(ROUTINES.session_id_by_title("Nightly digest", 2000))
            self.assertIsNone(ROUTINES.session_id_by_title("No such routine", 0))

    def test_session_id_falls_back_to_herdr_pane_record(self):
        routine = SimpleNamespace(name="Nightly digest")

        with (
            patch.object(ROUTINES, "resolve_file", return_value=Path("/tmp/nightly.md")),
            patch.object(ROUTINES, "Routine", return_value=routine),
            patch.object(ROUTINES, "session_id_by_title", return_value=None),
            patch.object(
                ROUTINES, "herdr_pane_session_id", return_value="ses_from_herdr"
            ) as herdr_lookup,
            patch("builtins.print") as printed,
        ):
            ROUTINES.cmd_session_id("/tmp/nightly.md", "1000", "pane-9")

        herdr_lookup.assert_called_once_with("pane-9")
        printed.assert_called_once_with("ses_from_herdr")

    def test_session_id_errors_when_nothing_resolves(self):
        routine = SimpleNamespace(name="Nightly digest")

        with (
            patch.object(ROUTINES, "resolve_file", return_value=Path("/tmp/nightly.md")),
            patch.object(ROUTINES, "Routine", return_value=routine),
            patch.object(ROUTINES, "session_id_by_title", return_value=None),
            patch.object(ROUTINES, "herdr_pane_session_id", return_value=None),
        ):
            with self.assertRaises(ROUTINES.RoutineError):
                ROUTINES.cmd_session_id("/tmp/nightly.md", "1000", "pane-9")


class RoutinePortabilityTest(unittest.TestCase):
    def test_runtime_preserves_path_without_injecting_private_tools(self):
        with patch.dict(os.environ, {"PATH": "/example/bin:/usr/bin"}, clear=True):
            self.assertEqual(ROUTINES.runtime_env(), {"PATH": "/example/bin:/usr/bin"})

    def test_configured_path_expands_home(self):
        with patch.dict(os.environ, {"ROUTINE_TEST_PATH": "~/example-routines"}):
            self.assertEqual(
                ROUTINES.configured_path("ROUTINE_TEST_PATH", Path("/unused")),
                Path.home() / "example-routines",
            )

    def test_install_discovers_tools_and_persists_storage_without_secrets(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            routine_path = root / "example.md"
            routine_path.write_text("---\nname: Example\nschedule: daily 09:00\n---\nReport")
            tools = {"uv": "/tools/uv", "opencode": "/agents/opencode"}
            with (
                patch.object(ROUTINES, "LAUNCH_DIR", root / "launch"),
                patch.object(ROUTINES, "DATA_DIR", root / "data"),
                patch.object(ROUTINES, "LOG_DIR", root / "data/logs"),
                patch.object(ROUTINES, "ROUTINES_DIR", root / "definitions"),
                patch.object(ROUTINES.shutil, "which", side_effect=tools.get),
                patch.object(ROUTINES, "launchctl") as launchctl,
                patch.dict(os.environ, {"EXAMPLE_SECRET": "must-not-be-persisted"}),
            ):
                routine = ROUTINES.Routine(routine_path)
                ROUTINES.install_one(routine)
                plist = plistlib.loads(routine.plist_path.read_bytes())
                self.assertEqual(plist["ProgramArguments"][:4], ["/tools/uv", "run", "--no-project", "python"])
                self.assertEqual(plist["WorkingDirectory"], str(Path.home()))
                env = plist["EnvironmentVariables"]
                self.assertTrue(env["PATH"].startswith("/tools:/agents:"))
                self.assertEqual(env["ROUTINES_DIR"], str(root / "definitions"))
                self.assertEqual(env["ROUTINES_DATA_DIR"], str(root / "data"))
                self.assertEqual(env["ROUTINES_LOG_DIR"], str(root / "data/logs"))
                self.assertNotIn("EXAMPLE_SECRET", env)
                self.assertNotIn("must-not-be-persisted", str(plist))
                self.assertEqual((root / "data").stat().st_mode & 0o777, 0o700)
                self.assertEqual(launchctl.call_count, 2)

    def test_missing_uv_has_actionable_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "example.md"
            path.write_text("---\nname: Example\nschedule: daily 09:00\n---\nReport")
            with (
                patch.object(ROUTINES, "LAUNCH_DIR", root / "launch"),
                patch.object(ROUTINES, "DATA_DIR", root / "data"),
                patch.object(ROUTINES, "LOG_DIR", root / "logs"),
                patch.object(ROUTINES.shutil, "which", return_value=None),
                self.assertRaisesRegex(ROUTINES.RoutineError, "uv is required"),
            ):
                ROUTINES.install_one(ROUTINES.Routine(path))

    def test_launchctl_rejects_unsupported_platform(self):
        with patch.object(ROUTINES.sys, "platform", "linux"):
            with self.assertRaisesRegex(ROUTINES.RoutineError, "requires macOS"):
                ROUTINES.launchctl(["list"])


if __name__ == "__main__":
    unittest.main()
