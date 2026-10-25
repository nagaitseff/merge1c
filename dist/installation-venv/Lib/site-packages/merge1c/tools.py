"""External commands receive snapshots and return independently checked candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import errno
import json
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

from .config import Tool
from .processes import find_program


@dataclass
class ToolResult:
    status: str
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    report: dict[str, Any] = field(default_factory=dict)
    identity: dict[str, Any] = field(default_factory=dict)
    cwd: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def tool_identity(tool: Tool, values: dict[str, str], *, cwd: Path) -> dict:
    executable = find_program(tool.executable)
    if not executable:
        return {"executable": tool.executable, "missing": True}
    def digest(path: Path) -> str:
        result = sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                result.update(block)
        return result.hexdigest()
    identity = {"executable": str(Path(executable).resolve()), "sha256": digest(Path(executable)), "files": {}}
    for template in tool.args:
        if any("{" + name + "}" in template for name in ("base", "local", "incoming", "result", "candidate", "report", "path")):
            continue
        try:
            value = template.format_map(values)
        except (KeyError, ValueError):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            is_file = candidate.is_file()
        except OSError as exc:
            # Arguments such as Python -c source are not necessarily paths.
            # Invalid/overlong names do not identify a file; permission and
            # read failures for actual files must still invalidate the check.
            if exc.errno not in {errno.ENAMETOOLONG, errno.EINVAL, errno.ENOENT, errno.ENOTDIR} and getattr(exc, "winerror", None) not in {123, 206}:
                raise
            continue
        if is_file:
            identity["files"][str(candidate.resolve())] = digest(candidate)
    return identity


def run_tool(tool: Tool, values: dict[str, str], *, cwd: Path) -> ToolResult:
    try:
        executable = find_program(tool.executable)
    except ValueError as exc:
        return ToolResult("failed", stderr=str(exc))
    if not executable:
        return ToolResult("failed", stderr=f"Программа не найдена: {tool.executable or '(путь не задан)'}")
    if tool.adapter == "beyond_compare" and not tool.args:
        prefix = "/" if os.name == "nt" else "-"
        args = [prefix + "solo", prefix + "ro", values["local"], values["incoming"], values["base"], values["result"]]
    else:
        try:
            args = [arg.format_map(values) for arg in tool.args]
        except (KeyError, ValueError, AttributeError) as exc:
            return ToolResult("failed", stderr=f"Ошибка аргументов инструмента: {exc}")
    command = [executable, *args]
    identity = tool_identity(tool, values, cwd=cwd)
    if Path(values["report"]).exists():
        return ToolResult("failed", command=command, stderr="Файл отчёта уже существует; нужен новый каталог запуска")
    started = time.perf_counter()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" and tool.kind == "validator" else 0
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.run(command, cwd=cwd, shell=False, stdout=out, stderr=err, timeout=tool.timeout, creationflags=flags)
            def tail(stream):
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 65536))
                return stream.read().decode("utf-8", errors="replace")
            stdout, stderr = tail(out), tail(err)
    except subprocess.TimeoutExpired:
        return ToolResult("timeout", command=command, seconds=time.perf_counter() - started,
                          stderr=f"Истёк тайм-аут {tool.timeout} с")
    except OSError as exc:
        return ToolResult("failed", command=command, seconds=time.perf_counter() - started, stderr=str(exc))
    result = ToolResult(
        "passed" if process.returncode == 0 else "failed",
        command=command, returncode=process.returncode, seconds=time.perf_counter() - started,
        stdout=stdout, stderr=stderr, identity=identity, cwd=str(cwd),
    )
    report_file = Path(values["report"])
    if report_file.exists():
        try:
            decoded = json.loads(report_file.read_text(encoding="utf-8-sig"))
            if not isinstance(decoded, dict):
                raise ValueError("ожидается объект JSON")
            result.report = decoded
            if result.report.get("status") != "passed":
                result.status = "failed"
        except (OSError, ValueError) as exc:
            result.status = "failed"
            result.stderr += f"\nНекорректный отчёт инструмента: {exc}"
    elif tool.report_required:
        result.status = "failed"
        result.stderr += "\nИнструмент не создал обязательный JSON-отчёт"
    if identity != tool_identity(tool, values, cwd=cwd):
        result.status = "failed"
        result.stderr += "\nПрограмма или её входной скрипт изменились во время проверки"
    return result

