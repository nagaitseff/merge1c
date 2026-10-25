"""External process adapters are exercised with isolated Python stubs."""

import json
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from merge1c.config import ConfigError, Tool
from merge1c.tools import run_tool, tool_identity


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="merge1c-tool test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.values = {name: str(self.root / name) for name in ("base", "local", "incoming", "result", "candidate")}
        self.values.update(report=str(self.root / "report.json"), path="Module with spaces.bsl")
        for name in ("base", "local", "incoming"):
            Path(self.values[name]).write_text(name, encoding="utf-8")
        Path(self.values["candidate"]).mkdir()

    def tool(self, code, *args, **options):
        script = self.root / "script with spaces.py"
        script.write_text(code, encoding="utf-8")
        return Tool(executable=sys.executable, args=(str(script), *args), **options)

    def test_validator_report_passes_and_stdout_is_recorded(self):
        tool = self.tool('''import json, pathlib, sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({"status":"passed","checks":["syntax"]}), encoding="utf-8")
print("validator done")
''', "{report}", report_required=True)
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("passed", result.status)
        self.assertEqual(0, result.returncode)
        self.assertEqual(["syntax"], result.report["checks"])
        self.assertIn("validator done", result.stdout)

    def test_resolver_can_write_candidate_without_mutating_input_snapshots(self):
        tool = self.tool('''import pathlib, sys
pathlib.Path(sys.argv[2]).write_bytes(pathlib.Path(sys.argv[1]).read_bytes() + b"-resolved")
''', "{local}", "{result}", kind="resolver")
        before = {name: Path(self.values[name]).read_bytes() for name in ("base", "local", "incoming")}
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("passed", result.status)
        self.assertEqual(b"local-resolved", Path(self.values["result"]).read_bytes())
        self.assertEqual(before, {name: Path(self.values[name]).read_bytes() for name in before})

    def test_arguments_with_spaces_and_shell_characters_are_literal(self):
        arguments = ("a b", "x&y", "$(whoami)", "%PATH%", "x|y", 'a"b', "--label={path}")
        tool = self.tool("import json, sys\nprint(json.dumps(sys.argv[1:]))\n", *arguments)
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("passed", result.status)
        self.assertEqual([*arguments[:-1], "--label=" + self.values["path"]], json.loads(result.stdout))
        self.assertIn("script with spaces.py", result.command[1])

    def test_nonzero_exit_cannot_be_overridden_by_passed_report(self):
        tool = self.tool('''import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('{"status":"passed"}', encoding="utf-8")
sys.exit(7)
''', "{report}", report_required=True)
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("failed", result.status)
        self.assertEqual(7, result.returncode)

    def test_failed_report_cannot_be_overridden_by_zero_exit(self):
        tool = self.tool('''import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('{"status":"failed","errors":["bad syntax"]}', encoding="utf-8")
''', "{report}", report_required=True)
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("failed", result.status)
        self.assertEqual(["bad syntax"], result.report["errors"])

    def test_missing_and_malformed_reports_fail(self):
        missing = self.tool("pass\n", report_required=True)
        self.assertEqual("failed", run_tool(missing, self.values, cwd=self.root).status)
        malformed = self.tool('''import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("{", encoding="utf-8")
''', "{report}", report_required=True)
        result = run_tool(malformed, self.values, cwd=self.root)
        self.assertEqual("failed", result.status)
        self.assertIsInstance(result.report, dict)

    def test_non_object_report_does_not_escape_as_an_invalid_report_type(self):
        tool = self.tool('''import pathlib, sys
pathlib.Path(sys.argv[1]).write_text("[]", encoding="utf-8")
''', "{report}", adapter="platform", report_required=True)
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("failed", result.status)
        self.assertIsInstance(result.report, dict)

    def test_report_with_wrong_schema_status_fails(self):
        tool = self.tool('''import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('{"status":true}', encoding="utf-8")
''', "{report}", report_required=True)
        self.assertEqual("failed", run_tool(tool, self.values, cwd=self.root).status)

    def test_timeout_is_explicit(self):
        result = run_tool(self.tool("import time\ntime.sleep(3)\n", timeout=1), self.values, cwd=self.root)
        self.assertEqual("timeout", result.status)
        self.assertIsNone(result.returncode)
        self.assertGreaterEqual(result.seconds, 0.5)

    def test_missing_executable_and_placeholder_fail_without_traceback(self):
        result = run_tool(Tool(executable=str(self.root / "missing-program.exe")), self.values, cwd=self.root)
        self.assertEqual("failed", result.status)
        with self.assertRaises(ConfigError):
            self.tool("pass\n", "{missing}")

    def test_long_python_source_argument_is_not_treated_as_a_path(self):
        code = "#" + "x" * 4000 + "\nprint('long argument passed')"
        tool = Tool(executable=sys.executable, args=("-c", code))
        result = run_tool(tool, self.values, cwd=self.root)
        self.assertEqual("passed", result.status, result.stderr)
        self.assertEqual("long argument passed", result.stdout.strip())

    def test_invalid_path_probe_is_ignored_but_actual_file_still_hashed(self):
        tool = self.tool("pass\n", "arbitrary argument")
        original = Path.is_file
        def is_file(path):
            if path.name == "arbitrary argument":
                raise OSError(errno.ENAMETOOLONG, "not a filesystem name")
            return original(path)
        with patch.object(Path, "is_file", is_file):
            identity = tool_identity(tool, self.values, cwd=self.root)
        script = self.root / "script with spaces.py"
        self.assertIn(str(script.resolve()), identity["files"])
        script.write_text("# changed\n", encoding="utf-8")
        self.assertNotEqual(identity, tool_identity(tool, self.values, cwd=self.root))

    def test_inaccessible_file_probe_is_not_silently_ignored(self):
        tool = self.tool("pass\n")
        with patch.object(Path, "is_file", side_effect=PermissionError(errno.EACCES, "access denied")):
            with self.assertRaises(PermissionError):
                tool_identity(tool, self.values, cwd=self.root)

    @unittest.skipUnless(os.name == "nt", "Windows batch-file dispatch")
    def test_batch_executable_is_rejected_before_shell_parses_arguments(self):
        script = self.root / "wrapper.cmd"
        marker = self.root / "executed.txt"
        script.write_text('@echo off\r\necho executed>executed.txt\r\n', encoding="ascii")
        result = run_tool(Tool(executable=str(script), args=("x&y",)), self.values, cwd=self.root)
        self.assertEqual("failed", result.status)
        self.assertIn(".bat/.cmd", result.stderr)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
