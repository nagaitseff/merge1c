"""Durable candidate workspaces; inputs and existing Git branches stay untouched."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import threading
from typing import Callable
import uuid

from .config import Config, config_from_snapshot
from .engine import MergeError, file_hash, is_link_path, merge_directories, snapshot, validate_directory
from .models import Issue
from .tools import run_tool, tool_identity


class SessionError(ValueError):
    pass


class AlreadyIntegrated(SessionError):
    """The incoming commit is already an ancestor of the current target."""


def _locked(method):
    @wraps(method)
    def invoke(self, *args, **kwargs):
        with self._thread_lock:
            if self._lock_depth:
                return method(self, *args, **kwargs)
            lock = self.path / "session.lock"
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise SessionError("Сессия занята другим процессом. После аварийного завершения используйте unlock --force") from exc
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump({"pid": os.getpid(), "started": _now()}, stream)
                self._reload()
                self._lock_depth = 1
                return method(self, *args, **kwargs)
            finally:
                self._lock_depth = 0
                lock.unlink(missing_ok=True)
    return invoke


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, data: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Session:
    def __init__(self, directory: str | Path):
        self.path = Path(directory).resolve()
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._reload()

    def _reload(self) -> None:
        try:
            self.data = json.loads((self.path / "session.json").read_text(encoding="utf-8"))
            if self.data["schema_version"] != 1:
                raise ValueError("неподдержанная версия сессии")
            self.config = config_from_snapshot(self.data["config"])
            if self.config.fingerprint != self.data["config_hash"]:
                raise ValueError("изменён снимок настроек")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SessionError(f"Не удалось открыть сессию {self.path}: {exc}") from exc

    @property
    def candidate(self) -> Path:
        return self.path / "candidate"

    def _save(self) -> dict:
        self.data["updated_at"] = _now()
        report = self.report()
        _atomic_json(self.path / "session.json", self.data)
        _atomic_json(self.path / "report.json", report)
        return report

    def event(self, phase: str, **values) -> None:
        with (self.path / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"time": _now(), "phase": phase, **values}, ensure_ascii=False) + "\n")

    def _verify_sources(self) -> None:
        for side in ("base", "local", "incoming"):
            if snapshot(self.path / "inputs" / side) != self.data["input_hashes"][side]:
                raise SessionError(f"Изменён сохранённый вход {side}; создайте новую сессию")
        if is_link_path(self.candidate):
            raise SessionError("Каталог кандидата не может быть символической ссылкой")

    def report(self) -> dict:
        issues = self.data.get("merge_issues", []) + self.data.get("validation_issues", [])
        try:
            self._verify_sources()
            inputs_current = True
        except (SessionError, MergeError):
            inputs_current = False
            issues = issues + [asdict(Issue("session.inputs_changed", "Сохранённые входы изменены; нужна новая сессия", severity="error"))]
        structural = not any(issue.get("severity", "conflict") in {"conflict", "error"} for issue in issues)
        tools = self.data.get("tool_results", {})
        stale_tools = []
        for name, outcome in tools.items():
            tool = self.config.tools.get(name)
            try:
                current_identity = tool_identity(tool, {"project": self.config.project_root}, cwd=Path(outcome.get("cwd", self.path))) if tool else None
            except (OSError, ValueError):
                current_identity = None
            if outcome.get("identity") != current_identity:
                stale_tools.append(name)
        try:
            actual_hashes = snapshot(self.candidate)
        except MergeError:
            actual_hashes = None
        checks_current = self.data.get("checked_hashes") == actual_hashes and actual_hashes is not None
        required_ok = checks_current and all(tools.get(name, {}).get("status") == "passed" and name not in stale_tools for name in self.config.required_validators)
        platform_ok = checks_current and any(
            result.get("status") == "passed" and name not in stale_tools and self.config.tools.get(name) is not None
            and self.config.tools[name].adapter == "platform"
            and result.get("report", {}).get("platform_version") == self.config.platform_version
            for name, result in tools.items()
        )
        ready = inputs_current and structural and required_ok and (platform_ok or not self.config.require_platform)
        return {
            "schema_version": 1, "session": str(self.path), "profile": self.config.profile,
            "incoming_role": self.config.incoming_role,
            "status": "ready" if ready else "conflicts" if not structural else "prepared",
            "structural_checks": "pending" if not checks_current else "passed" if structural else "failed",
            "candidate_current": checks_current,
            "inputs_current": inputs_current,
            "platform": "passed" if platform_ok else "not_run",
            "required_tools": "passed" if required_ok else "pending",
            "ready": ready,
            "stale_tools": stale_tools,
            "files": self.data.get("files", 0), "seconds": self.data.get("seconds", 0),
            "issues": issues, "changes": self.data.get("changes", []),
            "resolutions": self.data.get("resolutions", []), "tools": tools,
            "git": self.data.get("git"), "candidate": str(self.candidate),
        }

    @_locked
    def check(self, *, run_tools: bool = False) -> dict:
        self._verify_sources()
        current = snapshot(self.candidate)
        if current != self.data.get("candidate_hashes"):
            self.data["tool_results"] = {}
        self.data["candidate_hashes"] = current
        self.data["validation_issues"] = [asdict(issue) for issue in validate_directory(self.candidate, self.config)]
        if (self.data.get("git") or {}).get("uncommitted_state"):
            self.data["validation_issues"].append(asdict(Issue(
                "git.uncommitted_state", "Использованы только коммиты Git; незакоммиченные файлы и текущие ручные решения не включены", severity="warning")))
        if run_tools:
            names = list(self.config.required_validators)
            if self.config.require_platform:
                names += [name for name, tool in self.config.tools.items() if tool.adapter == "platform" and name not in names]
            for name in names:
                tool = self.config.tools[name]
                run_dir = self.path / "checks" / uuid.uuid4().hex
                run_dir.mkdir(parents=True)
                values = {"project": self.config.project_root, "candidate": str(self.candidate), "report": str(run_dir / "result.json"), "path": "",
                          "base": str(self.path / "inputs" / "base"), "local": str(self.path / "inputs" / "local"),
                          "incoming": str(self.path / "inputs" / "incoming"), "result": str(run_dir / "unused")}
                self.event("tool_started", tool=name)
                outcome = run_tool(tool, values, cwd=run_dir)
                if tool.adapter == "platform" and outcome.report.get("platform_version") != self.config.platform_version:
                    outcome.status = "failed"
                    outcome.stderr += "\nВерсия платформы в отчёте не совпадает с настройкой проекта"
                self.data.setdefault("tool_results", {})[name] = outcome.to_dict()
                self.event("tool_finished", tool=name, status=outcome.status)
                if snapshot(self.candidate) != current:
                    self.data["validation_issues"].append(asdict(Issue("tool.modified_candidate", "Проверяющая программа изменила кандидат", severity="error")))
                    self.data["tool_results"][name]["status"] = "failed"
                    break
        self._verify_sources()
        self.data["checked_hashes"] = current
        self.event("checked", structural_errors=sum(i["severity"] in {"error", "conflict"} for i in self.data["validation_issues"]))
        return self._save()

    @_locked
    def resolve(self, relative_path: str, *, take: str | None = None, tool_name: str | None = None) -> dict:
        self._verify_sources()
        snapshot(self.candidate)
        relative = PurePosixPath(relative_path.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts or ":" in str(relative) or relative.as_posix() in {"", "."}:
            raise SessionError("Нужен относительный путь файла внутри кандидата")
        path = relative.as_posix()
        unresolved = [issue for issue in self.data.get("merge_issues", []) if issue.get("path") == path]
        if not unresolved:
            raise SessionError(f"Нет нерешённого файлового конфликта: {path}")
        if take not in {None, "base", "local", "incoming", "candidate"} or bool(take) == bool(tool_name):
            raise SessionError("Выберите одну сторону --take или инструмент --tool")
        work = self.path / "resolutions" / uuid.uuid4().hex
        work.mkdir(parents=True)
        values: dict[str, str] = {"project": self.config.project_root, "path": path, "candidate": str(self.candidate), "report": str(work / "report.json"), "result": str(work / "result" / relative.name)}
        Path(values["result"]).parent.mkdir()
        sources = self.data.get("file_sources", {}).get(path, {})
        exists = {}
        for side in ("base", "local", "incoming"):
            source_name = sources.get(side, path)
            source = self.path / "inputs" / side / source_name if source_name is not None else None
            saved = work / side / relative.name
            saved.parent.mkdir()
            exists[side] = source is not None and source.is_file()
            saved.write_bytes(source.read_bytes() if exists[side] else b"")
            values[side] = str(saved)
        target = self.candidate / path
        previous = target.read_bytes() if target.exists() else None
        if previous is not None:
            (work / "previous").write_bytes(previous)
        tool_result = None
        if take == "candidate":
            content = previous
        elif take:
            content = Path(values[take]).read_bytes() if exists[take] else None
        else:
            tool = self.config.tools.get(tool_name)
            if tool is None or tool.kind != "resolver":
                raise SessionError(f"Инструмент разрешения не настроен: {tool_name}")
            before = {side: file_hash(Path(values[side])) for side in ("base", "local", "incoming")}
            outcome = run_tool(tool, values, cwd=work)
            if before != {side: file_hash(Path(values[side])) for side in before}:
                raise SessionError("Инструмент изменил входные снимки; результат не принят")
            tool_result = outcome.to_dict()
            if outcome.status != "passed" or not Path(values["result"]).is_file():
                raise SessionError(f"Инструмент не подготовил результат: {outcome.stderr or outcome.status}")
            content = Path(values["result"]).read_bytes()
        self._verify_sources()
        snapshot(self.candidate)
        if not target.resolve().is_relative_to(self.candidate.resolve()):
            raise SessionError("Путь результата выходит за каталог кандидата")
        if content is None:
            target.unlink(missing_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.data["merge_issues"] = [issue for issue in self.data["merge_issues"] if issue.get("path") != path]
        self.data.setdefault("manually_resolved", []).append(path)
        self.data.setdefault("resolutions", []).append({
            "time": _now(), "path": path, "take": take, "tool": tool_name,
            "before": sha256(previous).hexdigest() if previous is not None else None,
            "after": sha256(content).hexdigest() if content is not None else None,
            "workspace": str(work.relative_to(self.path)), "tool_result": tool_result,
        })
        self.data["tool_results"] = {}
        self.event("resolved", path=path, take=take, tool=tool_name)
        return self.check()

    @_locked
    def create_branch(self, branch: str, *, message: str = "Объединение исходников 1С") -> str:
        from .gitrepo import GitRepository

        git_data = self.data.get("git")
        if git_data is None:
            raise SessionError("Эта сессия создана из каталогов, а не из Git")
        report = self.check()
        if not report["ready"]:
            raise SessionError("Кандидат не готов: завершите решения и обязательные проверки")
        repo = GitRepository(git_data["repository"], executable=self.config.git_executable, minimum_version=self.config.git_minimum)
        if repo.head() != git_data["local"]:
            raise SessionError("Исходная вершина HEAD изменилась; создайте новую сессию")
        oid = repo.create_candidate(self.candidate, git_data["local"], git_data["incoming"], branch, message,
                                    expected_hashes=self.data["checked_hashes"])
        self.data.setdefault("branches", []).append({"name": branch, "commit": oid, "time": _now()})
        self.event("branch_created", branch=branch, commit=oid)
        self._save()
        return oid

    @_locked
    def release_pin(self) -> None:
        from .gitrepo import GitRepository

        git_data = self.data.get("git") or {}
        pin = git_data.get("pin")
        if not pin:
            raise SessionError("У сессии нет закреплённых входных коммитов Git")
        repo = GitRepository(git_data["repository"], executable=self.config.git_executable, minimum_version=self.config.git_minimum)
        repo.release_session(pin["ref"], pin["commit"])
        git_data["released_pin"] = {**pin, "time": _now()}
        del git_data["pin"]
        self.event("pin_released", ref=pin["ref"])
        self._save()


def prepare_directories(
    base: str | Path, local: str | Path, incoming: str | Path, session: str | Path,
    *, config: Config | None = None, progress: Callable[[dict], None] | None = None,
) -> Session:
    config = config or Config()
    location = Path(session).resolve()
    roots = [Path(value).resolve() for value in (base, local, incoming)]
    if any(location == root or location.is_relative_to(root) or root.is_relative_to(location) for root in roots):
        raise SessionError("Сессия должна находиться отдельно от входных каталогов")
    expected = [snapshot(root) for root in roots]
    if location.exists():
        raise SessionError(f"Каталог сессии уже существует: {location}")
    location.mkdir(parents=True)
    for side, root in zip(("base", "local", "incoming"), roots, strict=True):
        shutil.copytree(root, location / "inputs" / side, ignore=shutil.ignore_patterns(".git"))
    for side, root, hashes in zip(("base", "local", "incoming"), roots, expected, strict=True):
        if snapshot(root) != hashes or snapshot(location / "inputs" / side) != hashes:
            raise SessionError(f"Вход {side} изменился во время создания снимка")
    def emit(event: dict) -> None:
        with (location / "events.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"time": _now(), **event}, ensure_ascii=False) + "\n")
        if progress:
            progress(event)
    result = merge_directories(*(location / "inputs" / side for side in ("base", "local", "incoming")),
                               location / "candidate", config=config, progress=emit)
    candidate_hashes = snapshot(location / "candidate")
    data = {
        "schema_version": 1, "id": uuid.uuid4().hex, "created_at": _now(), "updated_at": _now(),
        "config": config.to_dict(), "config_hash": config.fingerprint,
        "input_hashes": result.input_hashes,
        "origins": dict(zip(("base", "local", "incoming"), (str(root) for root in roots), strict=True)),
        "candidate_hashes": candidate_hashes,
        "checked_hashes": dict(candidate_hashes),
        "merge_issues": [asdict(issue) for issue in result.issues if issue not in result.validation_issues],
        "validation_issues": [asdict(issue) for issue in result.validation_issues],
        "changes": [asdict(change) for change in result.changes],
        "file_sources": getattr(result, "file_sources", {}),
        "files": result.files, "seconds": result.seconds, "resolutions": [], "tool_results": {},
    }
    _atomic_json(location / "session.json", data)
    ready = Session(location)
    ready._save()
    return ready


def prepare_repository(
    repository: str | Path, incoming: str, session: str | Path,
    *, config: Config | None = None, progress: Callable[[dict], None] | None = None,
) -> Session:
    from .gitrepo import GitRepository

    config = config or Config()
    repo = GitRepository(repository, executable=config.git_executable, minimum_version=config.git_minimum)
    local_oid, incoming_oid = repo.head(), repo.resolve(incoming)
    bases = repo.merge_bases(local_oid, incoming_oid)
    if incoming_oid in bases:
        raise AlreadyIntegrated("Входящая версия уже включена в текущую ветку; новая сессия не нужна")
    if len(bases) != 1:
        raise SessionError("Нужно ровно одно подтверждённое основание Git; истории без общего основания или с несколькими основаниями пока не поддержаны")
    destination = Path(session).resolve()
    repo_root = repo.path
    if destination == repo_root or destination.is_relative_to(repo_root):
        raise SessionError("Сессия Git должна находиться вне исходного репозитория")
    if destination.exists():
        raise SessionError(f"Каталог сессии уже существует: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="merge1c-inputs-", dir=destination.parent) as temporary:
        roots = [Path(temporary) / side for side in ("base", "local", "incoming")]
        for revision, root in zip((bases[0], local_oid, incoming_oid), roots, strict=True):
            repo.export_tree(revision, root)
        result = prepare_directories(*roots, destination, config=config, progress=progress)
    uncommitted_state = bool(repo.status() or repo.operation_in_progress())
    result.data["git"] = {"repository": str(repo_root), "base": bases[0], "local": local_oid,
                           "incoming": incoming_oid, "incoming_ref": incoming, "version": repo.version,
                           "uncommitted_state": uncommitted_state}
    if uncommitted_state:
        result.data["validation_issues"].append(asdict(Issue(
            "git.uncommitted_state", "Использованы только коммиты Git; незакоммиченные файлы и текущие ручные решения не включены", severity="warning")))
    result.data["origins"] = {"base": bases[0], "local": local_oid, "incoming": incoming_oid}
    result._save()
    payload = json.dumps(result.data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    result.data["git"]["pin"] = repo.pin_session(local_oid, incoming_oid, result.data["id"], payload)
    result._save()
    return result

