"""Resolve programs without an implicit Windows command interpreter."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


def find_program(executable: str) -> str | None:
    found = shutil.which(executable)
    if found is None:
        return None
    if os.name == "nt" and Path(found).suffix.casefold() in {".bat", ".cmd"}:
        raise ValueError(
            "Прямой запуск .bat/.cmd не поддерживается: Windows может повторно разобрать аргументы. "
            "Укажите .exe, python.exe со скриптом .py или powershell.exe с -File и скриптом .ps1"
        )
    return str(Path(found).resolve())
