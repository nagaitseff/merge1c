"""Command-line interface with human-readable output and JSON reports."""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import asdict
import difflib
import json
from importlib.resources import files
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from . import __version__
from .config import ConfigError, load_config
from .engine import MergeError, validate_directory
from .gitrepo import GitError, GitRepository
from .processes import find_program
from .session import AlreadyIntegrated, Session, SessionError, prepare_directories, prepare_repository
from .workspace import check_location, check_storage, storage_root


DEFAULT_CONFIG = files("merge1c").joinpath("templates/merge1c.toml").read_text(encoding="utf-8")
LOCAL_EXAMPLE = files("merge1c").joinpath("templates/merge1c.local.example.toml").read_text(encoding="utf-8")


def _config(arguments):
    default_root = Path(getattr(arguments, "repo", None) or Path.cwd()).resolve()
    if getattr(arguments, "repo", None):
        for parent in (default_root, *default_root.parents):
            if (parent / ".git").exists():
                default_root = parent
                break
    project = Path(arguments.config) if arguments.config else default_root / "merge1c.toml"
    if not project.exists() and arguments.config:
        raise ConfigError(f"Файл настроек не найден: {project}")
    local = Path(arguments.local_config) if arguments.local_config else project.with_name("merge1c.local.toml")
    if not local.exists() and arguments.local_config:
        raise ConfigError(f"Локальный файл не найден: {local}")
    return load_config(project if project.exists() else None, local_path=local if local.exists() else None,
                       profile=arguments.profile)


def _progress(json_lines: bool):
    last_phase = None
    def show(event: dict) -> None:
        nonlocal last_phase
        if json_lines:
            print(json.dumps(event, ensure_ascii=True), file=sys.stderr, flush=True)
            return
        phase = event["phase"]
        if phase == "inputs":
            print(f"Наша ветка: {event.get('local_branch') or event['local']}; входящая: {event['incoming_ref']}", file=sys.stderr, flush=True)
        elif phase == "export":
            side = {"base": "общее основание", "local": "наша ветка", "incoming": "входящая ветка"}.get(event.get("side"), "снимок")
            print(f"Выгрузка Git — {side}: {event['current']}/{event['total']} файлов, {event['bytes'] / (1024 * 1024):.1f} МиБ", file=sys.stderr, flush=True)
        elif phase == "cleanup_warning":
            print(f"Предупреждение: временный каталог остался занят: {event['directory']}", file=sys.stderr, flush=True)
        elif phase == "merge":
            current, total = event["current"], event["total"]
            if current == 1 or current == total or current % 100 == 0:
                print(f"Объединение: {current}/{total} — {event['path']}", file=sys.stderr, flush=True)
        elif phase != last_phase:
            if phase == "storage":
                print(f"Сессия будет создана: {event['session']}", file=sys.stderr, flush=True)
                last_phase = phase
                return
            message = {"scan": "Чтение исходников", "validate": "Проверка результата", "complete": "Кандидат подготовлен"}.get(phase, phase)
            print(message, file=sys.stderr, flush=True)
        last_phase = phase
    return show


def _show(report: dict, json_output: bool) -> None:
    if json_output:
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return
    states = {"ready": "Проверки по настройкам пройдены", "prepared": "Подготовлен, нужны проверки", "conflicts": "Есть нерешённые случаи"}
    print(states[report["status"]])
    print(f"Файлов: {report['files']}; объединение: {report['seconds']:.3f} с")
    print(f"Кандидат: {report['candidate']}")
    print(f"Отчёт: {Path(report['session']) / 'report.json'}")
    checks = {"passed": "успешно", "failed": "есть ошибки", "pending": "нужна проверка", "not_run": "не запускалась"}
    print(f"Структура: {checks[report['structural_checks']]}; платформа: {checks[report['platform']]}; инструменты: {checks[report['required_tools']]}")
    issues = report["issues"]
    for issue in issues[:20]:
        print(f"  {issue['path'] or 'Сессия'}: {issue['message']} ({issue['code']})")
    if len(issues) > 20:
        print(f"  Ещё {len(issues) - 20} замечаний — в report.json")
    for name, result in report.get("tools", {}).items():
        if result["status"] != "passed":
            print(f"  Инструмент {name}: {result['status']} — {result.get('stderr', '')[-1000:]}")
    if report.get("stale_tools"):
        print("Изменились проверяющие программы; повторите check --run-tools: " + ", ".join(report["stale_tools"]))


def _hint(*arguments: str) -> str | None:
    # Hints are for pasting in cmd or PowerShell. Do not turn unusual file or
    # branch names into shell substitutions in either shell.
    values = [str(value).replace("\\", "/") for value in arguments]
    if any(re.search(r'["%$`!\r\n]', value) for value in values):
        return None
    return " ".join('"' + value + '"' for value in values)


def _next_steps(session: Session, report: dict) -> None:
    git = report.get("git")
    if git:
        print(f"Наша ветка: {git.get('local_branch') or git['local']}; входящая: {git['incoming_ref']}")
        print("Результат пока в candidate. Исходная ветка и её рабочие файлы не изменены.")
    if session.config.require_platform and report["platform"] != "passed":
        print("До создания ветки требуется проверка 1С настроенным адаптером (require_platform = true).")
        print("Ручной порядок проверки описан в docs/guide.md. Изменение TOML не меняет правила уже созданной сессии.")
    if report["ready"]:
        if report["platform"] != "passed":
            print("Встроенные проверки пройдены; 1С библиотекой не запускалась. Проверьте кандидат в тестовой базе.")
        if git:
            name = "review/" + (git.get("local_branch") or session.data["id"][:8])
            command = _hint("branch", str(session.path), name)
            if command:
                print("После проверки сохраните результат в новой ветке:")
                print("  python -m merge1c " + command)
    else:
        command = _hint("status" if report["status"] == "conflicts" else "check", str(session.path))
        if command:
            print("Продолжить работу с этой сессией:")
            print("  python -m merge1c " + command + (" --run-tools" if report["status"] != "conflicts" else ""))
        if report["status"] == "conflicts":
            print("Исправляйте указанные файлы в candidate; для нерешённого файла используйте resolve --tool beyond_compare или подтвердите ручную правку через resolve --accept.")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="merge1c", description="Объединение XML и BSL из выгрузки Конфигуратора 1С")
    result.add_argument("--version", action="version", version=f"merge1c {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="Общие настройки проекта (merge1c.toml)")
    common.add_argument("--local-config", help="Локальные пути инструментов")
    common.add_argument("--profile", default="vendor_update", help="Профиль: vendor_update или develop_sync")
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Создать начальные настройки")
    init.add_argument("directory", nargs="?", default=".")
    configuration = commands.add_parser("config", parents=[common], help="Показать итоговые настройки")
    configuration.add_argument("--write-example", metavar="FILE", help="Сохранить образец со всеми пояснениями; существующий файл не заменяется")
    doctor = commands.add_parser("doctor", parents=[common], help="Проверить Git и наличие инструментов")
    doctor.add_argument("--repo")
    doctor.add_argument("--session", help="Проверить доступ к конкретной сессии вместо storage.session_root")
    doctor.add_argument("--storage-only", action="store_true", help="Проверить только каталог хранения, без Git и внешних программ")
    inspect = commands.add_parser("inspect", parents=[common], help="Проверить каталог исходников без объединения")
    inspect.add_argument("directory")
    inspect.add_argument("--json", action="store_true")
    merge = commands.add_parser("merge", parents=[common], help="Объединить три каталога в отдельную сессию")
    for side in ("base", "local", "incoming"):
        merge.add_argument(side)
    merge.add_argument("--session", help="Новый каталог сессии; без параметра создаётся в storage.session_root")
    merge.add_argument("--json", action="store_true")
    merge_progress = merge.add_mutually_exclusive_group()
    merge_progress.add_argument("--json-progress", action="store_true")
    merge_progress.add_argument("--quiet", "-q", action="store_true", help="Не выводить прогресс; итоговый отчёт остаётся")
    prepare = commands.add_parser("prepare", parents=[common], help="Подготовить слияние текущего HEAD с веткой Git")
    prepare.add_argument("incoming")
    prepare.add_argument("--repo", default=".")
    prepare.add_argument("--session", help="Новый каталог сессии; без параметра создаётся в storage.session_root")
    prepare.add_argument("--json", action="store_true")
    prepare_progress = prepare.add_mutually_exclusive_group()
    prepare_progress.add_argument("--json-progress", action="store_true")
    prepare_progress.add_argument("--quiet", "-q", action="store_true", help="Не выводить прогресс; итоговый отчёт остаётся")
    preview = commands.add_parser("git-preview", parents=[common], help="Посмотреть предварительное слияние штатным Git без изменения рабочих файлов")
    preview.add_argument("incoming")
    preview.add_argument("--repo", default=".")
    for name, text in (("status", "Показать состояние сессии"), ("check", "Повторить проверки кандидата")):
        command = commands.add_parser(name, help=text)
        command.add_argument("session")
        command.add_argument("--json", action="store_true")
        if name == "check":
            command.add_argument("--run-tools", action="store_true", help="Запустить настроенные внешние проверки")
    resolve = commands.add_parser("resolve", help="Принять сторону, ручную правку или результат инструмента")
    resolve.add_argument("session")
    resolve.add_argument("path")
    choice = resolve.add_mutually_exclusive_group(required=True)
    choice.add_argument("--take", choices=("base", "local", "incoming"))
    choice.add_argument("--tool", nargs="?", const="", help="Инструмент из настроек; без имени используется manual_tool")
    choice.add_argument("--accept", action="store_true", help="Принять уже отредактированный файл кандидата")
    resolve.add_argument("--json", action="store_true")
    branch = commands.add_parser("branch", help="Создать новую ветку из проверенного кандидата")
    branch.add_argument("session")
    branch.add_argument("name")
    branch.add_argument("--message", default="Объединение исходников 1С")
    release = commands.add_parser("release-pin", help="Освободить служебную ссылку Git после завершения работы с сессией")
    release.add_argument("session")
    diff = commands.add_parser("diff", help="Показать изменения файла относительно входящей версии")
    diff.add_argument("session")
    diff.add_argument("path")
    events = commands.add_parser("events", help="Показать последние события сессии")
    events.add_argument("session")
    events.add_argument("--tail", type=int, default=20)
    unlock = commands.add_parser("unlock", help="Снять оставшуюся после сбоя блокировку")
    unlock.add_argument("session")
    unlock.add_argument("--force", action="store_true", required=True, help="Подтвердить, что другой процесс уже завершён")
    return result


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    arguments = parser().parse_args(argv)
    try:
        if arguments.command == "init":
            root = Path(arguments.directory).resolve()
            paths = [root / "merge1c.toml", root / "merge1c.local.example.toml"]
            if any(path.exists() for path in paths):
                raise ConfigError("Файлы настроек уже существуют; init их не перезаписывает")
            root.mkdir(parents=True, exist_ok=True)
            for path, content in zip(paths, (DEFAULT_CONFIG, LOCAL_EXAMPLE), strict=True):
                with path.open("x", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                print(path)
            return 0
        if arguments.command == "config" and arguments.write_example:
            target = Path(arguments.write_example).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(DEFAULT_CONFIG)
            print(f"Образец настроек: {target}")
            return 0
        if arguments.command in {"config", "doctor", "inspect", "merge", "prepare", "git-preview"}:
            config = _config(arguments)
        if arguments.command == "config":
            print(json.dumps({"config": config.to_dict(), "sha256": config.fingerprint}, ensure_ascii=True, indent=2))
            return 0
        if arguments.command == "doctor":
            forbidden = []
            if arguments.repo:
                source = Path(arguments.repo).resolve()
                source = next((parent for parent in (source, *source.parents) if (parent / ".git").exists()), source)
                forbidden.append(source)
            if arguments.session:
                location = Path(arguments.session).resolve()
                check_location(location, forbidden)
                if location.exists() and not location.is_dir():
                    raise ConfigError(f"Путь сессии занят файлом: {location}")
                directory = location if location.is_dir() else location.parent
            else:
                directory = storage_root(config)
            checked = check_storage(directory, forbidden_roots=forbidden)
            print(f"Каталог хранения: {checked}")
            print("Создание, чтение, запись, замена, переименование и удаление: доступны")
            if arguments.storage_only:
                return 0
            command = find_program(config.git_executable)
            if not command:
                raise ConfigError("Git не найден; задайте git.executable в локальных настройках")
            output = subprocess.run([command, "--version"], capture_output=True, check=True).stdout.decode("utf-8", errors="replace").strip()
            match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
            required = tuple(int(n) for n in config.git_minimum.split("."))
            if match is None or tuple(map(int, match.groups())) < required:
                raise ConfigError(f"Требуется Git {config.git_minimum} или новее; найдено {output}")
            print(output)
            if arguments.repo:
                repo = GitRepository(arguments.repo, executable=command, minimum_version=config.git_minimum)
                print(f"Репозиторий: {repo.path}; HEAD: {repo.head()}")
                print("Есть незакоммиченные изменения" if repo.status() else "Рабочие файлы не изменены")
                if repo.operation_in_progress():
                    print("В репозитории уже выполняется операция Git")
            missing = []
            platform_found = False
            for name, tool in config.tools.items():
                try:
                    found = find_program(tool.executable) if tool.executable else None
                except ValueError:
                    found = None
                print(f"{name}: {found or 'путь не задан или программа не найдена'}")
                if not found and name in config.required_validators:
                    missing.append(name)
                if found and tool.adapter == "platform":
                    platform_found = True
            print(f"Профиль: {config.profile}; платформа: {config.platform_version}")
            if config.require_platform and not platform_found:
                print("Проверка платформой обязательна; задайте доступную программу с adapter = 'platform'")
                missing.append("platform")
            return 2 if missing else 0
        if arguments.command == "inspect":
            issues = validate_directory(arguments.directory, config)
            if arguments.json:
                print(json.dumps({"issues": [asdict(issue) for issue in issues], "platform": "not_run"}, ensure_ascii=True, indent=2))
            else:
                for issue in issues:
                    print(f"{issue.path}: {issue.message} ({issue.code})")
                print(f"Проверка структуры завершена. Замечаний: {len(issues)}. Платформа не запускалась.")
            return int(any(issue.blocking for issue in issues))
        if arguments.command == "git-preview":
            repo = GitRepository(arguments.repo, executable=config.git_executable, minimum_version=config.git_minimum)
            result = repo.preview(repo.head(), arguments.incoming)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0 if result["clean"] else 1
        if arguments.command in {"merge", "prepare"}:
            progress = None if arguments.quiet else _progress(arguments.json_progress)
            if arguments.command == "merge":
                session = prepare_directories(arguments.base, arguments.local, arguments.incoming, arguments.session,
                                              config=config, progress=progress)
            else:
                session = prepare_repository(arguments.repo, arguments.incoming, arguments.session, config=config, progress=progress)
            report = session.report()
            _show(report, arguments.json)
            if not arguments.json:
                _next_steps(session, report)
            return int(report["status"] == "conflicts")
        session = Session(arguments.session)
        if arguments.command in {"status", "check"}:
            report = session.check(run_tools=arguments.command == "check" and arguments.run_tools)
            _show(report, arguments.json)
            if not arguments.json:
                _next_steps(session, report)
            return (0 if report["ready"] else 1 if report["status"] == "conflicts" else 2) if arguments.command == "check" else 0
        if arguments.command == "resolve":
            tool_name = (arguments.tool or session.config.manual_tool) if arguments.tool is not None else None
            if arguments.tool is not None and not tool_name:
                raise ConfigError("manual_tool не настроен; укажите --tool NAME")
            report = session.resolve(arguments.path, take="candidate" if arguments.accept else arguments.take, tool_name=tool_name)
            _show(report, arguments.json)
            return int(report["status"] == "conflicts")
        if arguments.command == "branch":
            oid = session.create_branch(arguments.name, message=arguments.message)
            print(f"Создана ветка {arguments.name}: {oid}")
            print("Исходная ветка и рабочие файлы не изменены.")
            git = session.data.get("git") or {}
            if git.get("local_branch"):
                print(f"После проверки включите результат в {git['local_branch']}. Сначала убедитесь, что именно эта ветка выбрана:")
                for arguments_to_git in (("switch", git["local_branch"]), ("merge", "--ff-only", arguments.name)):
                    command = _hint("-C", git["repository"], *arguments_to_git)
                    if command:
                        print("  git " + command)
            return 0
        if arguments.command == "release-pin":
            session.release_pin()
            print("Входные коммиты больше не закреплены служебной ссылкой этой сессии.")
            return 0
        if arguments.command == "events":
            if arguments.tail < 1:
                raise SessionError("tail должен быть положительным")
            with (session.path / "events.jsonl").open(encoding="utf-8") as stream:
                for line in deque(stream, maxlen=arguments.tail):
                    print(json.dumps(json.loads(line), ensure_ascii=True))
            return 0
        if arguments.command == "unlock":
            (session.path / "session.lock").unlink(missing_ok=True)
            print("Блокировка снята. Выполните check перед продолжением.")
            return 0
        if arguments.command == "diff":
            relative = PurePosixPath(arguments.path.replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts or ":" in str(relative):
                raise SessionError("Нужен относительный путь файла")
            path = relative.as_posix()
            source = session.data.get("file_sources", {}).get(path, {}).get("incoming", path)
            original = session.path / "inputs/incoming" / source if source else None
            current = session.candidate / path
            try:
                left = original.read_bytes().decode("utf-8-sig") if original and original.exists() else ""
                right = current.read_bytes().decode("utf-8-sig") if current.exists() else ""
            except UnicodeDecodeError as exc:
                raise SessionError("Бинарный файл; используйте внешний инструмент") from exc
            print("".join(difflib.unified_diff(left.splitlines(keepends=True), right.splitlines(keepends=True),
                                             fromfile=f"incoming/{path}", tofile=f"candidate/{path}")), end="")
            return 0
        return 0
    except AlreadyIntegrated as exc:
        if getattr(arguments, "json", False):
            print(json.dumps({"status": "already_integrated", "message": str(exc)}, ensure_ascii=True))
        else:
            print(str(exc))
        return 0
    except (ConfigError, SessionError, MergeError, GitError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
