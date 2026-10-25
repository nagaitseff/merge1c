"""Exercise the same public CLI through the available Windows shells."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


_PROJECT = Path(__file__).resolve().parents[1]
_WINDOWS_POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
_POWERSHELL = shutil.which("pwsh.exe") if os.name == "nt" else None
_CMD = shutil.which("cmd.exe") if os.name == "nt" else None


def _ps_quote(value) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class WindowsShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="merge1c-shell-")
        self.addCleanup(self.temporary.cleanup)
        self.scratch = Path(self.temporary.name).resolve()
        self.root = self.scratch / "Проверка оболочек & пробелы"
        self.root.mkdir()
        self.config = self.root / "настройки проекта.toml"
        validator = self.root / "Проверка аргументов & пробелы.py"
        validator.write_text(
            "import json, pathlib, sys\n"
            "assert pathlib.Path(sys.argv[1]).is_dir()\n"
            "assert 'Проверка оболочек & пробелы' in sys.argv[1]\n"
            "pathlib.Path(sys.argv[2]).write_text(json.dumps(dict(status='passed', candidate=sys.argv[1]), ensure_ascii=False), encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.config.write_text(
            "[validation]\nrequire_platform = false\nrequired_tools = ['wrapper']\n"
            "[tools.wrapper]\nreport_required = true\n"
            "executable = " + json.dumps(sys.executable, ensure_ascii=False) + "\n"
            "args = [" + json.dumps(str(validator), ensure_ascii=False) + ', "{candidate}", "{report}"]\n',
            encoding="utf-8",
        )
        self.base = b'\xef\xbb\xbf<Form version="2.20"><A>old</A><B>old</B></Form>\r\n'
        self.local = self.base.replace(b"<A>old", "<A>наше".encode())
        self.incoming = self.base.replace(b"<B>old", "<B>типовое".encode())
        for name, content in (("base", self.base), ("local", self.local), ("incoming", self.incoming)):
            location = self.root / name
            location.mkdir()
            (location / "Основная форма.xml").write_bytes(content)
        (self.root / "broken").mkdir()
        (self.root / "broken" / "Неверная форма.xml").write_bytes(b"<Form>")
        self.environment = os.environ.copy()
        self.environment["PYTHONPATH"] = str(_PROJECT / "src")
        self.environment["PYTHONUTF8"] = "1"
        self.session = self.root / "Результат объединения"

    def _assert_workflow(self, records):
        self.assertEqual([record["code"] for record in records], [0, 0, 0, 1, 2], records)
        configuration = json.loads(records[0]["output"])
        self.assertEqual(configuration["config"]["project_root"], str(self.root))
        report = json.loads(records[1]["output"])
        self.assertEqual(report["candidate"], str(self.session / "candidate"))
        self.assertFalse(report["ready"], "The configured external check has not run yet")
        checked = json.loads(records[2]["output"])
        self.assertTrue(checked["ready"], checked)
        self.assertEqual(checked["tools"]["wrapper"]["status"], "passed")
        self.assertEqual(checked["tools"]["wrapper"]["report"]["candidate"], str(self.session / "candidate"))
        expected = self.local.replace(b"<B>old", "<B>типовое".encode())
        self.assertEqual((self.session / "candidate" / "Основная форма.xml").read_bytes(), expected)
        self.assertEqual((self.root / "base" / "Основная форма.xml").read_bytes(), self.base)
        self.assertEqual((self.root / "local" / "Основная форма.xml").read_bytes(), self.local)
        self.assertEqual((self.root / "incoming" / "Основная форма.xml").read_bytes(), self.incoming)
        self.assertEqual(json.loads((self.session / "report.json").read_text(encoding="utf-8"))["ready"], True)

    def _arguments(self):
        return [
            ("config", ["config", "--config", self.config]),
            ("merge", ["merge", self.root / "base", self.root / "local", self.root / "incoming",
                       "--config", self.config, "--session", self.session, "--json"]),
            ("check", ["check", self.session, "--run-tools", "--json"]),
            ("invalid", ["inspect", self.root / "broken", "--json"]),
            ("missing", ["inspect", self.root / "отсутствует", "--json"]),
        ]

    def _powershell_workflow(self, executable, *, quiet_merge=False):
        result_file = self.scratch / "shell-results.json"
        lines = [
            "$previousEncoding = [Console]::OutputEncoding",
            "try {",
            # Native JSON should remain correct even with the traditional OEM
            # decoder. Test the pipe, rather than just capturing native bytes.
            "[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(866)",
            "$records = @()",
        ]
        for name, arguments in self._arguments():
            if quiet_merge and name == "merge":
                arguments = [*arguments, "--quiet"]
                lines.append("$ErrorActionPreference = 'Stop'")
            command = "& " + _ps_quote(sys.executable) + " -m merge1c " + " ".join(map(_ps_quote, arguments))
            lines += [
                "$raw = " + command + " 2> " + _ps_quote(self.scratch / (name + ".err")),
                "$code = $LASTEXITCODE",
                "$records += [ordered]@{ command = " + _ps_quote(name) + '; code = $code; output = ($raw -join "`n") }',
            ]
            if quiet_merge and name == "merge":
                lines.append("$ErrorActionPreference = 'Continue'")
        lines += [
            "[IO.File]::WriteAllText(" + _ps_quote(result_file) + ", ($records | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))",
            "} finally { [Console]::OutputEncoding = $previousEncoding }",
        ]
        encoded = base64.b64encode("\n".join(lines).encode("utf-16-le")).decode("ascii")
        process = subprocess.run(
            [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env=self.environment, capture_output=True, timeout=60,
        )
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        self._assert_workflow(json.loads(result_file.read_text(encoding="utf-8")))
        if quiet_merge:
            self.assertEqual((self.scratch / "merge.err").read_bytes(), b"")
        else:
            self.assertTrue((self.scratch / "merge.err").read_bytes(), "Progress belongs to stderr")

    @unittest.skipUnless(_WINDOWS_POWERSHELL, "Windows PowerShell is unavailable")
    def test_windows_powershell_paths_json_pipe_and_exit_codes(self):
        self._powershell_workflow(_WINDOWS_POWERSHELL)

    @unittest.skipUnless(_WINDOWS_POWERSHELL, "Windows PowerShell is unavailable")
    def test_windows_powershell_stop_preference_with_quiet_json(self):
        self._powershell_workflow(_WINDOWS_POWERSHELL, quiet_merge=True)

    @unittest.skipUnless(_POWERSHELL, "PowerShell 7 is unavailable")
    def test_powershell_7_paths_json_pipe_and_exit_codes(self):
        self._powershell_workflow(_POWERSHELL)

    @unittest.skipUnless(_CMD, "cmd.exe is unavailable")
    def test_cmd_paths_redirection_and_exit_codes(self):
        # Keep the batch itself ASCII. Unicode values arrive through the
        # environment, so script-file code pages do not affect the test.
        lines = ["@echo off"]
        environment = self.environment.copy()
        environment["MERGE1C_SHELL_PYTHON"] = sys.executable
        records = []
        for index, (name, arguments) in enumerate(self._arguments()):
            tokens = []
            for argument_index, value in enumerate(arguments):
                variable = f"MERGE1C_SHELL_ARG_{index}_{argument_index}"
                environment[variable] = str(value)
                tokens.append('"%' + variable + '%"')
            environment[f"MERGE1C_SHELL_OUT_{index}"] = str(self.scratch / (name + ".json"))
            environment[f"MERGE1C_SHELL_ERR_{index}"] = str(self.scratch / (name + ".err"))
            environment[f"MERGE1C_SHELL_CODE_{index}"] = str(self.scratch / (name + ".code"))
            lines += [
                '"%MERGE1C_SHELL_PYTHON%" -m merge1c ' + " ".join(tokens)
                + f' > "%MERGE1C_SHELL_OUT_{index}%" 2> "%MERGE1C_SHELL_ERR_{index}%"',
                f'echo %ERRORLEVEL% > "%MERGE1C_SHELL_CODE_{index}%"',
            ]
        batch = self.scratch / "workflow.cmd"
        batch.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))
        process = subprocess.run([_CMD, "/d", "/v:off", "/c", str(batch)], env=environment, capture_output=True, timeout=60)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))
        for name, _ in self._arguments():
            records.append({"command": name, "code": int((self.scratch / (name + ".code")).read_text()),
                            "output": (self.scratch / (name + ".json")).read_text(encoding="utf-8")})
        self._assert_workflow(records)
        self.assertTrue((self.scratch / "merge.err").read_bytes(), "Progress belongs to stderr")


if __name__ == "__main__":
    unittest.main()
