"""Exercise the real CLI in child Python processes, with isolated directories."""

import json
from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from merge1c.cli import main
from merge1c.session import AlreadyIntegrated


_PROJECT = Path(__file__).resolve().parents[1]


def _module(a=1, b=1):
    return (f"Процедура A()\n    X = {a};\nКонецПроцедуры\n\n"
            f"Процедура B()\n    X = {b};\nКонецПроцедуры\n").encode("utf-8")


class CliTests(unittest.TestCase):
    def setUp(self):
        # Keep test artifacts in the authorized workspace, not a host temp area.
        scratch_parent = (_PROJECT / "tests").resolve()
        self.temp = tempfile.TemporaryDirectory(prefix="cli-scratch-", dir=scratch_parent)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(scratch_parent))
        self.addCleanup(self.temp.cleanup)
        self.environment = os.environ.copy()
        self.environment["PYTHONPATH"] = str(_PROJECT / "src")

    def cli(self, *arguments, cwd=None):
        process = subprocess.run(
            [sys.executable, "-m", "merge1c", *map(str, arguments)],
            cwd=cwd or self.root, env=self.environment, capture_output=True, timeout=30,
        )
        return process.returncode, process.stdout.decode("utf-8"), process.stderr.decode("utf-8")

    def config_file(self, text="", name="custom settings.toml"):
        result = self.root / name
        result.write_text(text, encoding="utf-8")
        return result

    def inputs(self, *, conflict=False):
        directories = [self.root / name for name in ("ancestor sources", "our sources", "vendor sources")]
        for directory in directories:
            directory.mkdir()
        variants = (_module(), _module(a=2), _module(a=3) if conflict else _module(b=3))
        for directory, content in zip(directories, variants, strict=True):
            (directory / "Module.bsl").write_bytes(content)
        return directories

    def prepared(self, *, conflict=False, config=None, json_progress=False):
        directories = self.inputs(conflict=conflict)
        session = self.root / "candidate session"
        arguments = ["merge", *directories, "--session", session, "--json"]
        if config is not None:
            arguments += ["--config", config]
        if json_progress:
            arguments.append("--json-progress")
        code, stdout, stderr = self.cli(*arguments)
        self.assertEqual(1 if conflict else 0, code, stderr)
        return directories, session, json.loads(stdout), stderr

    def test_help_and_version(self):
        code, stdout, stderr = self.cli("--help")
        self.assertEqual(0, code, stderr)
        for command in ("merge", "inspect", "config", "resolve", "check"):
            self.assertIn(command, stdout)
        code, stdout, stderr = self.cli("--version")
        self.assertEqual(0, code, stderr)
        self.assertRegex(stdout.strip(), r"^merge1c \d+\.\d+\.\d+$")

    def test_init_creates_readable_settings_and_never_overwrites_them(self):
        destination = self.root / "new project"
        code, stdout, stderr = self.cli("init", destination)
        self.assertEqual(0, code, stderr)
        paths = (destination / "merge1c.toml", destination / "merge1c.local.example.toml")
        before = {path: path.read_bytes() for path in paths}
        code, stdout, stderr = self.cli("config", cwd=destination)
        self.assertEqual(0, code, stderr)
        self.assertEqual("vendor_update", json.loads(stdout)["config"]["profile"])
        code, stdout, stderr = self.cli("init", destination)
        self.assertEqual(2, code)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(before, {path: path.read_bytes() for path in paths})

    def test_config_and_profile_arguments_work_after_command(self):
        config = self.config_file('[project]\nsource_roots = ["src/cf"]\n')
        code, stdout, stderr = self.cli("config", "--config", config, "--profile", "develop_sync")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("develop_sync", report["config"]["profile"])
        self.assertEqual("master", report["config"]["incoming_role"])
        self.assertEqual(["src/cf"], report["config"]["source_roots"])
        self.assertRegex(report["sha256"], r"^[0-9a-f]{64}$")

    def test_invalid_configuration_and_profile_are_clean_errors(self):
        invalid = self.config_file("unknown_setting = true\n")
        for arguments in (("config", "--config", invalid), ("config", "--profile", "unknown"),
                          ("config", "--config", self.root / "absent.toml")):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.cli(*arguments)
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertTrue(stderr.strip())
                self.assertNotIn("Traceback", stderr)

    def test_write_example_recovers_from_broken_config_without_overwriting_files(self):
        broken = self.config_file("[storage]\nsession_root = C:/Temp\n")
        original = broken.read_bytes()
        example = self.root / "Подсказки с пробелами" / "settings.toml"
        code, stdout, stderr = self.cli("config", "--config", broken, "--write-example", example)
        self.assertEqual(0, code, stderr)
        self.assertEqual(original, broken.read_bytes())
        self.assertIn("session_root", example.read_text(encoding="utf-8"))
        code, stdout, stderr = self.cli("config", "--config", example)
        self.assertEqual(0, code, stderr)
        self.assertEqual("", json.loads(stdout)["config"]["session_root"])
        generated = example.read_bytes()
        code, stdout, stderr = self.cli("config", "--config", broken, "--write-example", example)
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(generated, example.read_bytes())
        code, stdout, stderr = self.cli("config", "--config", broken, "--write-example", broken)
        self.assertEqual(2, code)
        self.assertEqual(original, broken.read_bytes())
        self.assertNotIn("Traceback", stderr)

    @unittest.skipUnless(shutil.which("git"), "Git executable is required for doctor")
    def test_doctor_reports_a_missing_required_tool_without_launching_gui(self):
        config = self.config_file('''[validation]
required_tools = ["required_check"]
[tools.required_check]
kind = "validator"
executable = "merge1c-nonexistent-checker"
''')
        code, stdout, stderr = self.cli("doctor", "--config", config)
        self.assertEqual(2, code, stderr)
        self.assertIn("git version", stdout.lower())
        self.assertIn("required_check", stdout)
        self.assertIn("8.3.27.2342", stdout)
        self.assertNotIn("Traceback", stderr)

    def test_inspect_malformed_xml_is_json_and_fails(self):
        source = self.root / "broken sources"
        source.mkdir()
        (source / "Broken.xml").write_bytes(b"<Form><Child></Form>")
        code, stdout, stderr = self.cli("inspect", source, "--json")
        self.assertEqual(1, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("not_run", report["platform"])
        self.assertTrue(report["issues"])
        self.assertTrue(any(issue["severity"] in {"error", "conflict"} for issue in report["issues"]))

    def test_inspect_valid_bsl_does_not_claim_platform_validation(self):
        source = self.root / "valid sources"
        source.mkdir()
        (source / "Module.bsl").write_bytes(_module())
        code, stdout, stderr = self.cli("inspect", source, "--json")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertEqual([], report["issues"])
        self.assertEqual("not_run", report["platform"])

    def test_merge_json_is_separate_from_json_progress_and_preserves_inputs(self):
        directories, session, report, stderr = self.prepared(json_progress=True)
        self.assertEqual("prepared", report["status"])
        self.assertFalse(report["ready"])
        self.assertEqual(_module(a=2, b=3), (session / "candidate" / "Module.bsl").read_bytes())
        events = [json.loads(line) for line in stderr.splitlines() if line.strip()]
        self.assertTrue(events)
        self.assertIn("merge", {event["phase"] for event in events})
        self.assertIn("complete", {event["phase"] for event in events})
        for directory, content in zip(directories, (_module(), _module(a=2), _module(b=3)), strict=True):
            self.assertEqual(content, (directory / "Module.bsl").read_bytes())
        self.assertTrue((session / "session.json").is_file())
        self.assertTrue((session / "report.json").is_file())

    def test_check_reports_pending_platform_with_exit_two(self):
        _, session, _, _ = self.prepared()
        code, stdout, stderr = self.cli("check", session, "--json")
        self.assertEqual(2, code, stderr)
        report = json.loads(stdout)
        self.assertEqual("prepared", report["status"])
        self.assertFalse(report["ready"])
        self.assertNotEqual("passed", report["platform"])

    def test_conflict_exit_one_and_resolve_take_incoming(self):
        _, session, report, _ = self.prepared(conflict=True)
        self.assertEqual("conflicts", report["status"])
        self.assertTrue(any(issue["code"] == "BSL_METHOD_CONFLICT" for issue in report["issues"]))
        code, stdout, stderr = self.cli("check", session, "--json")
        self.assertEqual(1, code, stderr)
        code, stdout, stderr = self.cli("resolve", session, "Module.bsl", "--take", "incoming", "--json")
        self.assertEqual(0, code, stderr)
        resolved = json.loads(stdout)
        self.assertEqual("prepared", resolved["status"])
        self.assertEqual(_module(a=3), (session / "candidate" / "Module.bsl").read_bytes())
        self.assertFalse(resolved["ready"])
        code, stdout, stderr = self.cli("check", session, "--json")
        self.assertEqual(2, code, stderr)

    def test_explicit_platform_optout_is_visible_and_allows_structural_ready(self):
        config = self.config_file("[validation]\nrequire_platform = false\n")
        _, session, _, _ = self.prepared(config=config)
        code, stdout, stderr = self.cli("check", session, "--json")
        self.assertEqual(0, code, stderr)
        report = json.loads(stdout)
        self.assertTrue(report["ready"])
        self.assertNotEqual("passed", report["platform"])

    def test_events_and_diff_are_usable_without_git(self):
        _, session, _, _ = self.prepared()
        code, stdout, stderr = self.cli("events", session, "--tail", "2")
        self.assertEqual(0, code, stderr)
        lines = stdout.splitlines()
        self.assertEqual(2, len(lines))
        self.assertTrue(all("phase" in json.loads(line) for line in lines))
        code, stdout, stderr = self.cli("diff", session, "Module.bsl")
        self.assertEqual(0, code, stderr)
        self.assertIn("--- incoming/Module.bsl", stdout)
        self.assertIn("+++ candidate/Module.bsl", stdout)
        self.assertIn("+    X = 2;", stdout)

    def test_resolve_rejects_path_traversal(self):
        _, session, _, _ = self.prepared(conflict=True)
        code, stdout, stderr = self.cli("resolve", session, "../outside.bsl", "--take", "incoming", "--json")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertNotIn("Traceback", stderr)
        self.assertFalse((self.root / "outside.bsl").exists())

    def test_existing_session_is_not_overwritten(self):
        directories, session, _, _ = self.prepared()
        marker = session / "session.json"
        before = marker.read_bytes()
        code, stdout, stderr = self.cli("merge", *directories, "--session", session, "--json")
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertEqual(before, marker.read_bytes())
        self.assertNotIn("Traceback", stderr)

    def test_quiet_merge_keeps_machine_output_and_saved_progress(self):
        directories = self.inputs()
        session = self.root / "Тихая сессия"
        code, stdout, stderr = self.cli("merge", *directories, "--session", session, "--quiet", "--json")
        self.assertEqual(0, code, stderr)
        self.assertEqual("", stderr)
        self.assertTrue(stdout.isascii())
        self.assertEqual(str(session), json.loads(stdout)["session"])
        journal = (session / "events.jsonl").read_text(encoding="utf-8")
        self.assertIn('"phase": "merge"', journal)
        code, stdout, stderr = self.cli("events", session)
        self.assertEqual(0, code, stderr)
        self.assertTrue(stdout.isascii())
        self.assertTrue(any(json.loads(line)["phase"] == "merge" for line in stdout.splitlines()))

    def test_already_integrated_git_prepare_still_returns_machine_json(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch("merge1c.cli.prepare_repository", side_effect=AlreadyIntegrated("Уже включено")):
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["prepare", "master", "--repo", str(self.root),
                             "--session", str(self.root / "unused"), "--json", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(json.loads(output.getvalue()), {"status": "already_integrated", "message": "Уже включено"})
        self.assertTrue(output.getvalue().isascii())

    def test_configured_storage_allows_merge_without_session_argument(self):
        directories = self.inputs()
        storage = self.root / "Хранение сессий"
        config = self.config_file(f"[storage]\nsession_root = '{storage.as_posix()}'\n[validation]\nrequire_platform = false\n")
        code, stdout, stderr = self.cli("merge", *directories, "--config", config, "--quiet", "--json")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(Path(json.loads(stdout)["session"]).parent, storage)

    def test_storage_only_doctor_checks_files_without_platform_or_git(self):
        storage = self.root / "Work directory"
        config = self.config_file(f"[storage]\nsession_root = '{storage.as_posix()}'\n[git]\nexecutable = 'not-installed-git'\n")
        code, stdout, stderr = self.cli("doctor", "--config", config, "--storage-only")
        self.assertEqual(code, 0, stderr)
        self.assertIn(str(storage), stdout)
        self.assertEqual(list(storage.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
