"""Directory integration, metadata matching, and final cross-file checks."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import os
from pathlib import Path
import re
import shutil
import time
from typing import Callable
import xml.etree.ElementTree as ET

from .config import Config
from .models import Change, Issue, MergeResult


class MergeError(ValueError):
    pass


@dataclass
class DirectoryResult:
    output: str
    issues: list[Issue] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    files: int = 0
    seconds: float = 0.0
    input_hashes: dict[str, dict[str, str]] = field(default_factory=dict)
    file_sources: dict[str, dict[str, str | None]] = field(default_factory=dict)
    validation_issues: list[Issue] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return not any(issue.blocking for issue in self.issues)

    def to_dict(self) -> dict:
        return {
            "output": self.output,
            "resolved": self.resolved,
            "files": self.files,
            "seconds": round(self.seconds, 6),
            "issues": [asdict(item) for item in self.issues],
            "changes": [asdict(item) for item in self.changes],
        }


_GROUPS = {
    "BusinessProcesses", "Catalogs", "ChartsOfAccounts", "ChartsOfCalculationTypes",
    "ChartsOfCharacteristicTypes", "CommandGroups", "CommonAttributes", "CommonCommands",
    "CommonForms", "CommonModules", "CommonPictures", "CommonTemplates", "Constants",
    "DataProcessors", "DefinedTypes", "DocumentJournals", "Documents", "Enums",
    "EventSubscriptions", "ExchangePlans", "FilterCriteria", "FunctionalOptions",
    "FunctionalOptionsParameters", "HTTPServices", "InformationRegisters", "Languages",
    "Reports", "Roles", "ScheduledJobs", "SessionParameters", "SettingsStorages",
    "StyleItems", "Subsystems", "Tasks", "WebServices", "XDTOPackages",
    "AccumulationRegisters", "AccountingRegisters", "CalculationRegisters",
}


def file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_link_path(path: Path) -> bool:
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return True
    try:
        return getattr(path.lstat(), "st_reparse_tag", None) == 0xA0000003
    except FileNotFoundError:
        return False


def _check_path_collisions(paths) -> None:
    """Reject names that cannot coexist on a case-insensitive filesystem."""
    names: dict[str, tuple[str, bool]] = {}
    for path in sorted(paths):
        parts = path.split("/")
        for count in range(1, len(parts) + 1):
            name = "/".join(parts[:count])
            is_file = count == len(parts)
            previous = names.get(name.casefold())
            if previous is not None:
                if previous[0] != name:
                    raise MergeError(f"Имена различаются только регистром: {previous[0]} и {name}")
                if previous[1] != is_file:
                    raise MergeError(f"В результате одновременно требуются файл и каталог: {name}")
            names[name.casefold()] = (name, is_file)


def snapshot(root: str | Path) -> dict[str, str]:
    if is_link_path(Path(root)):
        raise MergeError(f"Символическая ссылка не поддерживается: {root}")
    root = Path(root).resolve()
    if not root.is_dir():
        raise MergeError(f"Каталог не найден: {root}")
    result: dict[str, str] = {}
    folded: dict[str, str] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name != ".git")
        files = [name for name in files if name != ".git"]
        for name in directories + files:
            if is_link_path(Path(current) / name):
                raise MergeError(f"Символическая ссылка не поддерживается: {Path(current) / name}")
        for name in sorted(files):
            path = Path(current) / name
            if not path.is_file():
                raise MergeError(f"Поддерживаются только обычные файлы: {path}")
            relative = path.relative_to(root).as_posix()
            if relative.casefold() in folded:
                raise MergeError(f"Имена различаются только регистром: {folded[relative.casefold()]} и {relative}")
            folded[relative.casefold()] = relative
            result[relative] = file_hash(path)
    _check_path_collisions(result)
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def metadata_identity(data: bytes, path: str) -> tuple[str, str, str] | None:
    """Read only the metadata object's own UUID, not UUID-looking descendants."""
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None
    try:
        root = ET.fromstring(data)
    except (ET.ParseError, ValueError):
        return None
    if _local(root.tag) != "MetaDataObject":
        return None
    children = list(root)
    if len(children) != 1 or "uuid" not in children[0].attrib:
        return None
    parts = path.split("/")
    group = next((i for i, part in enumerate(parts) if part in _GROUPS), len(parts) - 1)
    container = "/".join(parts[:group])
    obj = children[0]
    return container, obj.tag, obj.attrib["uuid"].casefold()


def _metadata_index(root: Path, files: dict[str, str], issues: list[Issue], side: str) -> dict:
    index = {}
    seen_uuids = {}
    for path in sorted(files):
        if not path.lower().endswith(".xml"):
            continue
        identity = metadata_identity((root / path).read_bytes(), path)
        if identity is None:
            continue
        short_key = identity[0], identity[2]
        if short_key in seen_uuids:
            issues.append(Issue("metadata.duplicate_uuid", f"В {side} UUID принадлежит нескольким объектам", path=path,
                                details={"other": seen_uuids[short_key], "uuid": identity[2]}))
        else:
            seen_uuids[short_key] = path
        if identity not in index:
            index[identity] = path
    return index


def _remapped_files(files: dict[str, str], rules: list[tuple[str, str]], issues: list[Issue], side: str) -> dict[str, str]:
    # Exact paths and owned directories have distinct matching rules. Keep
    # identity rules too: a nested object can be an intentional barrier to its
    # parent's relocation. A linear rule scan per file is quadratic on a large
    # unchanged configuration, so inspect only the file's own ancestor paths.
    exact: dict[str, tuple[str, str]] = {}
    owned: dict[str, tuple[str, str]] = {}
    for source, target in rules:
        exact.setdefault(source, (source, target))
        if source.endswith(".xml"):
            owned.setdefault(source[:-4], (source, target))
    mapped: dict[str, str] = {}
    for old in sorted(files):
        new = old
        match = exact.get(old)
        parent = old.rpartition("/")[0]
        while parent:
            ancestor = owned.get(parent)
            if ancestor is not None:
                if match is None or len(ancestor[0]) > len(match[0]):
                    match = ancestor
                break
            parent = parent.rpartition("/")[0]
        if match is not None:
            source, target = match
            new = target if old == source else target[:-4] + old[len(source) - 4:]
        if new in mapped:
            issues.append(Issue("metadata.path_collision", f"Несколько файлов {side} попадают в {new}", path=new,
                                details={"sources": [mapped[new], old]}))
        else:
            mapped[new] = old
    return mapped


def _selected(path: str, config: Config) -> bool:
    return not config.source_roots or any(path == root or path.startswith(root + "/") for root in config.source_roots)


def validate_directory(directory: str | Path, config: Config | None = None) -> list[Issue]:
    """Validate each known source and the static form-to-module bindings."""
    from .bslmerge import inspect_bsl, method_names
    from .xmlmerge import inspect_xml

    config = config or Config()
    root = Path(directory).resolve()
    files = snapshot(root)
    issues: list[Issue] = []
    _metadata_index(root, {p: h for p, h in files.items() if _selected(p, config)}, issues, "результате")
    for path in sorted(files):
        if not _selected(path, config):
            continue
        suffix = Path(path).suffix.lower()
        if suffix not in {".bsl", ".xml"}:
            continue
        data = (root / path).read_bytes()
        if suffix == ".bsl":
            issues.extend(inspect_bsl(data, path=path))
        elif suffix == ".xml":
            issues.extend(inspect_xml(data, path=path))
        else:
            continue
        if suffix != ".xml" or not path.endswith("/Ext/Form.xml"):
            continue
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            continue
        try:
            form = ET.fromstring(data)
        except (ET.ParseError, ValueError):
            continue
        module_path = path[:-4] + "/Module.bsl"
        names = method_names((root / module_path).read_bytes()) if module_path in files else set()
        for node in form.iter():
            if _local(node.tag) not in {"Event", "Action"}:
                continue
            handler = (node.text or "").strip()
            if not handler:
                continue
            if re.fullmatch(r"[^\W\d]\w*", handler, flags=re.UNICODE):
                if handler.casefold() not in names:
                    issues.append(Issue("form.missing_handler", f"Не найден обработчик {handler}", path=path,
                                        details={"module": module_path, "handler": handler}))
            else:
                issues.append(Issue("form.dynamic_handler", f"Статически не проверено обращение к обработчику {handler}",
                                    severity="warning", path=path))
    return _unique_issues(issues)


def _unique_issues(issues: list[Issue]) -> list[Issue]:
    import json
    found = set()
    result = []
    for issue in issues:
        key = (issue.code, issue.path, issue.message, json.dumps(issue.details, sort_keys=True, ensure_ascii=False))
        if key not in found:
            found.add(key)
            result.append(issue)
    return result


def merge_directories(
    base: str | Path,
    local: str | Path,
    incoming: str | Path,
    output: str | Path,
    *,
    config: Config | None = None,
    progress: Callable[[dict], None] | None = None,
) -> DirectoryResult:
    """Create a new candidate directory; never modify any input directory."""
    from .bslmerge import merge_bsl
    from .xmlmerge import merge_xml

    config = config or Config()
    started = time.perf_counter()
    roots = [Path(value).resolve() for value in (base, local, incoming)]
    out = Path(output).resolve()
    for root in roots:
        if out == root or out.is_relative_to(root) or root.is_relative_to(out):
            raise MergeError("Каталог результата не должен содержать входные каталоги или находиться внутри них")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise MergeError(f"Каталог результата должен быть новым или пустым: {out}")
    report = DirectoryResult(output=str(out))
    def emit(phase: str, **values) -> None:
        if progress:
            progress({"phase": phase, **values})
    emit("scan")
    hashes = [snapshot(root) for root in roots]
    report.input_hashes = dict(zip(("base", "local", "incoming"), hashes, strict=True))
    indexes = [_metadata_index(root, {p: h for p, h in files.items() if _selected(p, config)}, report.issues, side)
               for root, files, side in zip(roots, hashes, ("базе", "нашей версии", "входящей версии"), strict=True)]
    rules: list[list[tuple[str, str]]] = [[], [], []]
    rename_delete: list[str] = []
    for identity, old_path in sorted(indexes[0].items()):
        left = indexes[1].get(identity)
        right = indexes[2].get(identity)
        if left is None and right is None:
            continue
        if left is None or right is None:
            survivor = right if right is not None else left
            if survivor != old_path:
                rename_delete.append(survivor)
                rules[0].append((old_path, survivor))
                if left is not None:
                    rules[1].append((left, survivor))
                if right is not None:
                    rules[2].append((right, survivor))
            continue
        if left != old_path and right != old_path and left != right:
            report.issues.append(Issue("metadata.rename_rename", "Объект переименован в разные пути", path=right,
                                       details={"base": old_path, "local": left, "incoming": right}))
        target = right if right != old_path else left
        if target != old_path:
            report.changes.append(Change("metadata.rename", "Перемещение сопоставлено по типу и UUID", path=target,
                                         details={"base": old_path, "local": left, "incoming": right}))
        for side, source in enumerate((old_path, left, right)):
            rules[side].append((source, target))
    mapped = [_remapped_files(files, side_rules, report.issues, side)
              for files, side_rules, side in zip(hashes, rules, ("базы", "нашей версии", "входящей версии"), strict=True)]
    paths = sorted(set().union(*(set(side) for side in mapped)))
    def has_rename_delete(path: str) -> bool:
        return any(path == prefix or (prefix.endswith(".xml") and path.startswith(prefix[:-4] + "/")) for prefix in rename_delete)
    predicted = set()
    for path in paths:
        sums = [files.get(side.get(path)) for files, side in zip(hashes, mapped, strict=True)]
        base_hash, local_hash, incoming_hash = sums
        exists = ((local_hash is not None) if local_hash == incoming_hash else
                  (incoming_hash is not None) if local_hash == base_hash else
                  (local_hash is not None) if incoming_hash == base_hash else
                  (local_hash is not None or incoming_hash is not None))
        if has_rename_delete(path):
            exists = local_hash is not None or incoming_hash is not None
        if exists:
            predicted.add(path)
    _check_path_collisions(predicted)
    out.mkdir(parents=True, exist_ok=True)
    for index, path in enumerate(paths, 1):
        emit("merge", path=path, current=index, total=len(paths))
        originals = [side.get(path) for side in mapped]
        report.file_sources[path] = dict(zip(("base", "local", "incoming"), originals, strict=True))
        sums = [files.get(original) if original is not None else None for files, original in zip(hashes, originals, strict=True)]
        base_hash, local_hash, incoming_hash = sums
        chosen: int | None = None
        merged: MergeResult | None = None
        if has_rename_delete(path) and ((local_hash is None) != (incoming_hash is None)):
            chosen = 2 if incoming_hash is not None else 1
            report.issues.append(Issue("metadata.rename_delete", "Одна сторона удалила объект, другая переместила", path=path))
        elif base_hash is not None and local_hash is not None and incoming_hash is not None and not (base_hash == local_hash == incoming_hash) and _selected(path, config) and Path(path).suffix.lower() in {".xml", ".bsl"}:
            data = [(root / original).read_bytes() for root, original in zip(roots, originals, strict=True)]
            merged = (merge_xml if Path(path).suffix.lower() == ".xml" else merge_bsl)(*data, path=path)
        elif local_hash == incoming_hash:
            chosen = 2 if incoming_hash is not None else None
        elif local_hash == base_hash:
            chosen = 2 if incoming_hash is not None else None
        elif incoming_hash == base_hash:
            chosen = 1 if local_hash is not None else None
        elif base_hash is None:
            chosen = 2
            report.issues.append(Issue("file.add_add", "Обе стороны добавили разные версии файла", path=path))
        elif local_hash is None or incoming_hash is None:
            chosen = 2 if incoming_hash is not None else 1
            report.issues.append(Issue("file.delete_edit", "Одна сторона удалила файл, другая изменила", path=path))
        elif not _selected(path, config):
            chosen = 2
            report.issues.append(Issue("file.outside_roots", "Обе стороны изменили файл вне настроенных корней", path=path))
        else:
            data = [(root / original).read_bytes() for root, original in zip(roots, originals, strict=True)]
            suffix = Path(path).suffix.lower()
            if suffix == ".xml":
                merged = merge_xml(*data, path=path)
            elif suffix == ".bsl":
                merged = merge_bsl(*data, path=path)
            else:
                chosen = 2
                report.issues.append(Issue("file.concurrent_change", "Нужен выбор содержимого файла", path=path))
        destination = out / path
        if merged is not None:
            report.issues.extend(merged.issues)
            report.changes.extend(merged.changes)
            if merged.content is not None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(merged.content)
                if originals[2] is not None:
                    shutil.copymode(roots[2] / originals[2], destination)
        elif chosen is not None and originals[chosen] is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(roots[chosen] / originals[chosen], destination)
        if local_hash != base_hash and chosen == 1:
            report.changes.append(Change("file.local", "Перенесена наша версия файла", path=path))
        if path in mapped[2] and not destination.exists():
            report.changes.append(Change("file.deleted", "Принято удаление файла", path=path))
    emit("validate", total=len(paths))
    report.validation_issues = validate_directory(out, config)
    report.issues.extend(report.validation_issues)
    for side, root, expected in zip(("base", "local", "incoming"), roots, hashes, strict=True):
        if snapshot(root) != expected:
            report.issues.append(Issue("input.changed", f"Вход {side} изменился во время объединения", severity="error"))
    report.issues = _unique_issues(report.issues)
    report.files = len(paths)
    report.seconds = time.perf_counter() - started
    emit("complete", resolved=report.resolved, files=report.files, seconds=report.seconds,
         unresolved=sum(item.blocking for item in report.issues))
    return report

