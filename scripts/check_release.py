"""Проверить готовую поставку: python scripts/check_release.py.

Нужны Windows, Python x64, pip и Git. Проверяет архивы из dist, установку
wheel в чистое окружение, переносимый EXE и слияние временного репозитория.
Исходники и готовые артефакты не изменяются; проверки идут в build.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from build_release import ROOT, version, windows_arch


def run(command, cwd: Path, *, environment: dict[str, str], expected: int = 0) -> str:
    result = subprocess.run(
        list(map(str, command)), cwd=cwd, env=environment, capture_output=True,
        encoding="utf-8", errors="replace", timeout=120,
    )
    if result.returncode != expected:
        raise AssertionError((command, result.returncode, result.stdout, result.stderr))
    return result.stdout


def check_archives(release: str, folder_name: str) -> Path:
    output = ROOT / "dist"
    wheel = output / f"merge1c-{release}-py3-none-any.whl"
    source_archive = output / f"merge1c-{release}.tar.gz"
    portable_archive = output / f"{folder_name}.zip"
    with zipfile.ZipFile(wheel) as archive:
        sources = list((ROOT / "src/merge1c").glob("*.py"))
        sources += list((ROOT / "src/merge1c/templates").glob("*.toml"))
        sources.append(ROOT / "src/merge1c/py.typed")
        expected_sources = {source.relative_to(ROOT / "src").as_posix() for source in sources}
        actual_sources = {name for name in archive.namelist() if name.startswith("merge1c/") and not name.endswith("/")}
        assert actual_sources == expected_sources, actual_sources ^ expected_sources
        for source in sources:
            name = source.relative_to(ROOT / "src").as_posix()
            assert archive.read(name) == source.read_bytes(), source
    with tarfile.open(source_archive) as archive:
        sources = [ROOT / "pyproject.toml", ROOT / "README.md", ROOT / "CHANGELOG.md"]
        sources += list((ROOT / "docs").glob("*.md"))
        sources += list((ROOT / "scripts").glob("*.py"))
        sources += list((ROOT / "scripts").glob("*.txt"))
        sources += list((ROOT / "src/merge1c").glob("*.py"))
        sources += list((ROOT / "src/merge1c/templates").glob("*.toml"))
        for source in sources:
            name = f"merge1c-{release}/" + source.relative_to(ROOT).as_posix()
            member = archive.extractfile(name)
            assert member is not None, name
            with member:
                assert member.read() == source.read_bytes(), source
        names = archive.getnames()
        assert not any("/docs/windows.md" in name or "/docs/usage.md" in name for name in names)
    checksums = {}
    for line in (output / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        digest, filename = line.split("  ", 1)
        assert filename not in checksums, filename
        checksums[filename] = digest
    assert set(checksums) == {wheel.name, source_archive.name, portable_archive.name}, checksums
    for filename, expected in checksums.items():
        with (output / filename).open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, filename
    # The immediately usable folder must be the same distribution as the ZIP.
    with zipfile.ZipFile(portable_archive) as archive:
        expected_files = {member.filename for member in archive.infolist() if not member.is_dir()}
        actual_files = {path.relative_to(output).as_posix()
                        for path in (output / folder_name).rglob("*") if path.is_file()}
        assert actual_files == expected_files, actual_files ^ expected_files
        for member in archive.infolist():
            if not member.is_dir():
                path = output / member.filename
                assert path.resolve().is_relative_to((output / folder_name).resolve()), member.filename
                assert path.read_bytes() == archive.read(member), path
    return wheel


def check_git_workflow(executable: Path, python: Path, git: str, work: Path,
                       environment: dict[str, str], release: str) -> None:
    repo = work / "Репозиторий"
    run([git, "init", "-b", "master", repo], work, environment=environment)

    def gitrun(*arguments):
        return run([git, "-C", repo, *arguments], work, environment=environment)

    gitrun("config", "user.name", "Release test")
    gitrun("config", "user.email", "release@example.invalid")
    gitrun("config", "commit.gpgsign", "false")
    gitrun("config", "core.hooksPath", "")
    gitrun("config", "core.autocrlf", "false")
    module = repo / "src/cf/CommonModules/Проверка/Ext/Module.bsl"
    module.parent.mkdir(parents=True)
    base = ("Процедура Первая()\n    Значение = 1;\nКонецПроцедуры\n\n"
            "Процедура Вторая()\n    Значение = 2;\nКонецПроцедуры\n")
    module.write_bytes(base.encode("utf-8"))
    gitrun("add", ".")
    gitrun("commit", "-m", "base")
    gitrun("branch", "vendor/2.5.27.101")
    gitrun("switch", "-c", "update/2.5.27.101")
    module.write_bytes(base.replace("= 1;", "= 10;").encode("utf-8"))
    gitrun("add", ".")
    gitrun("commit", "-m", "local")
    local_head = gitrun("rev-parse", "HEAD").strip()
    gitrun("checkout", "vendor/2.5.27.101")
    module.write_bytes(base.replace("= 2;", "= 20;").encode("utf-8"))
    gitrun("add", ".")
    gitrun("commit", "-m", "vendor")
    incoming = gitrun("rev-parse", "HEAD").strip()
    gitrun("checkout", "update/2.5.27.101")
    run([executable, "init", repo], work, environment=environment)
    config = repo / "merge1c.toml"
    content = config.read_text(encoding="utf-8")
    content = content.replace("session_root = ''", f"session_root = '{(work / 'Сессии').as_posix()}'")
    content = content.replace("require_platform = true", "require_platform = false")
    content = content.replace("required_tools = []", "required_tools = ['python_check']")
    # A real external Python process catches frozen-app DLL search-path leakage.
    content += ("\n[tools.python_check]\nkind = 'validator'\n"
                f"executable = '{python.as_posix()}'\n"
                "args = ['-c', 'import ssl; print(ssl.OPENSSL_VERSION)']\n")
    config.write_text(content, encoding="utf-8")
    before_index = (repo / ".git/index").read_bytes()
    before_source = module.read_bytes()
    run([executable, "doctor", "--repo", repo, "--storage-only"], work, environment=environment)
    report = json.loads(run([executable, "prepare", "vendor/2.5.27.101", "--repo", repo, "--json", "--quiet"],
                            work, environment=environment))
    assert report["git"]["local_branch"] == "update/2.5.27.101", report
    session = Path(report["session"])
    checked = json.loads(run([executable, "check", session, "--run-tools", "--json"], work, environment=environment))
    assert checked["ready"], checked
    assert checked["tools"]["python_check"]["status"] == "passed", checked
    output = run([executable, "branch", session, "review/update/2.5.27.101"], work, environment=environment)
    assert "--ff-only" in output and "update/2.5.27.101" in output, output
    parents = gitrun("rev-list", "--parents", "-1", "review/update/2.5.27.101").split()[1:]
    assert parents == [local_head, incoming], parents
    merged = gitrun("show", "review/update/2.5.27.101:" + module.relative_to(repo).as_posix())
    assert "= 10;" in merged and "= 20;" in merged, merged
    assert gitrun("rev-parse", "HEAD").strip() == local_head
    assert module.read_bytes() == before_source
    assert (repo / ".git/index").read_bytes() == before_index
    gitrun("merge", "--ff-only", "review/update/2.5.27.101")
    assert gitrun("rev-parse", "HEAD") == gitrun("rev-parse", "review/update/2.5.27.101")
    gitrun("merge-base", "--is-ancestor", incoming, "HEAD")
    assert "= 10;" in module.read_text(encoding="utf-8") and "= 20;" in module.read_text(encoding="utf-8")
    run([executable, "release-pin", session], work, environment=environment)
    assert run([executable, "--version"], work, environment=environment).strip() == f"merge1c {release}"


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Эта проверка поставки поддерживает только Windows с Python x64")
    arch = windows_arch()
    if arch != "x64":
        raise SystemExit("Эта проверка поставки поддерживает только Windows с Python x64")
    if not __debug__:
        raise SystemExit("Запускайте проверку без -O: проверки assert должны быть включены")
    release = version()
    folder_name = f"merge1c-{release}-windows-{arch}"
    wheel = check_archives(release, folder_name)
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    git = shutil.which("git")
    if not git:
        raise SystemExit("Для проверки нужен Git 2.50 или новее в PATH")
    work_root = ROOT / "build"
    work_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="installation-", dir=work_root) as scratch:
        work = Path(scratch)
        venv = work / "clean-venv"
        run([sys.executable, "-m", "venv", "--without-pip", venv], work, environment=environment)
        python = venv / "Scripts/python.exe"
        run([sys.executable, "-m", "pip", "--python", python, "install", "--no-index", "--no-deps", wheel],
            work, environment=environment)
        for number, command in enumerate(([python, "-I", "-m", "merge1c"], [venv / "Scripts/merge1c.exe"])):
            assert run([*command, "--version"], work, environment=environment).strip() == f"merge1c {release}"
            configdir = work / f"wheel-{number}"
            run([*command, "init", configdir], work, environment=environment)
            assert (configdir / "merge1c.toml").is_file()
        portable = work / "Поставка с пробелами & символом"
        with zipfile.ZipFile(ROOT / "dist" / f"{folder_name}.zip") as archive:
            archive.extractall(portable)
        executable = portable / folder_name / "merge1c.exe"
        frozen_env = environment.copy()
        system = Path(os.environ["SystemRoot"]) / "System32"
        frozen_env["PATH"] = str(Path(git).parent) + os.pathsep + str(system)
        frozen_env["TEMP"] = frozen_env["TMP"] = str(work / "unavailable-system-temp")
        for number, program in enumerate((executable, ROOT / "dist" / folder_name / "merge1c.exe")):
            assert run([program, "--version"], work, environment=frozen_env).strip() == f"merge1c {release}"
            run([program, "init", work / f"portable-{number}"], work, environment=frozen_env)
        command = "& '" + str(executable).replace("'", "''") + "' --version; exit $LASTEXITCODE"
        encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
        powershell = system / "WindowsPowerShell/v1.0/powershell.exe"
        assert f"merge1c {release}" in run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], work, environment=frozen_env,
        )
        script = work / "verify.cmd"
        script.write_text('@echo off\n"%RELEASE_EXE%" --version\nexit /b %ERRORLEVEL%\n', encoding="ascii")
        frozen_env["RELEASE_EXE"] = str(executable)
        assert f"merge1c {release}" in run([system / "cmd.exe", "/d", "/c", script], work, environment=frozen_env)
        check_git_workflow(executable, python, git, work, frozen_env, release)
    print(f"Поставка merge1c {release} проверена: архивы, SHA256, установка wheel, EXE, cmd, PowerShell и Git-сценарий")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
