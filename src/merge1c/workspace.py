"""Select session storage and check the actual filesystem operations it needs."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .config import Config


class StorageError(ValueError):
    pass


def storage_root(config: Config) -> Path:
    if config.session_root:
        root = Path(config.session_root)
        if not root.is_absolute():
            root = Path(config.project_root) / root
        return root.resolve()
    try:
        return (Path(tempfile.gettempdir()) / "merge1c-sessions").resolve()
    except OSError as exc:
        raise StorageError("Нет доступного временного каталога. Задайте [storage] session_root, например 'C:/Temp/merge1c-sessions'") from exc


def session_location(config: Config, requested: str | Path | None, *, label: str = "merge") -> Path:
    if requested is not None:
        return Path(requested).resolve()
    slug = re.sub(r"[^\w.-]+", "-", label, flags=re.UNICODE).strip(".-")[:70] or "merge"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return storage_root(config) / f"merge-{slug}-{timestamp}-{uuid.uuid4().hex[:8]}"


def check_location(location: Path, forbidden_roots) -> None:
    for root in forbidden_roots:
        root = Path(root).resolve()
        if location == root or location.is_relative_to(root) or root.is_relative_to(location):
            raise StorageError(f"Сессия должна быть отдельно от исходников: {location}. Выберите соседний каталог в storage.session_root или --session")


@contextmanager
def _probe_directory(root: Path):
    # tempfile.mkdtemp retries PermissionError on Windows when os.access()
    # reports write access. That is not reliable for a CreateDirectories ACL.
    # A UUID name plus one exclusive mkdir reports the actual denial at once.
    scratch = root / f".merge1c-check-{uuid.uuid4().hex}"
    scratch.mkdir(mode=0o700)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch)


def check_storage(directory: str | Path, *, forbidden_roots=()) -> Path:
    """Probe in an owned scratch directory; never chmod or edit existing files."""
    root = Path(directory).resolve()
    for source in forbidden_roots:
        source = Path(source).resolve()
        if root == source or root.is_relative_to(source):
            raise StorageError(f"Каталог хранения находится внутри исходников: {root}. Задайте storage.session_root вне репозитория")
    operation = "создать каталог хранения"
    try:
        root.mkdir(parents=True, exist_ok=True)
        operation = "создать временный подкаталог"
        with _probe_directory(root) as work:
            original, renamed = work / "before", work / "after"
            original.mkdir()
            item = original / "test.bin"
            operation = "создать и записать файл"
            with item.open("xb") as stream:
                stream.write(b"merge1c storage check\n")
                stream.flush()
                os.fsync(stream.fileno())
            operation = "прочитать файл"
            if item.read_bytes() != b"merge1c storage check\n":
                raise OSError("проверочные данные изменились после записи")
            operation = "переименовать файл"
            item = item.rename(original / "renamed.bin")
            replacement = original / "replacement.bin"
            replacement.write_bytes(b"replacement")
            operation = "заменить файл"
            os.replace(replacement, item)
            operation = "переименовать каталог с файлом"
            original.rename(renamed)
            operation = "удалить проверочные файлы и каталоги"
            (renamed / item.name).unlink()
            renamed.rmdir()
    except OSError as exc:
        raise StorageError(
            f"Не удалось {operation} в {root}: {exc}. "
            "Нужны права создания, чтения, записи, переименования и удаления. "
            "Возможна также блокировка файла другой программой. "
            "Укажите доступный каталог в storage.session_root (например 'C:/REP/merge-sessions' или 'C:/Temp/merge-sessions') "
            "либо другой --session. Существующие права доступа программа не меняет."
        ) from exc
    return root
