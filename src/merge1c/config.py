"""Validated, versioned configuration with explicit local tool bindings."""

from __future__ import annotations

import copy
from collections.abc import Mapping
import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path, PurePosixPath
import re
import string
import tomllib
from typing import Any
from types import MappingProxyType


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Tool:
    adapter: str = "command"
    executable: str = ""
    args: tuple[str, ...] = ()
    timeout: int = 300
    kind: str = "validator"
    report_required: bool = False

    def __post_init__(self) -> None:
        adapter = _string(self.adapter, "Tool.adapter")
        kind = _string(self.kind, "Tool.kind")
        if adapter not in {"command", "beyond_compare", "platform"} or kind not in {"resolver", "validator"}:
            raise ConfigError("Неподдержанный adapter/kind инструмента")
        _string(self.executable, "Tool.executable")
        if "\x00" in self.executable:
            raise ConfigError("Tool.executable: недопустимый нулевой символ")
        if type(self.timeout) is not int or self.timeout <= 0:
            raise ConfigError("Tool.timeout: ожидается положительное целое число")
        _boolean(self.report_required, "Tool.report_required")
        object.__setattr__(self, "args", _arguments(self.args, "Tool.args"))


@dataclass(frozen=True)
class Config:
    schema_version: int = 1
    profile: str = "vendor_update"
    incoming_role: str = "vendor"
    source_roots: tuple[str, ...] = ()
    platform_version: str = "8.3.27.2342"
    project_root: str = field(default_factory=lambda: str(Path.cwd().resolve()))
    git_executable: str = "git"
    git_minimum: str = "2.50.0"
    manual_tool: str = ""
    required_validators: tuple[str, ...] = ()
    require_platform: bool = True
    tools: dict[str, Tool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ConfigError("Поддерживается schema_version = 1")
        for name in ("profile", "platform_version", "project_root", "git_executable", "manual_tool"):
            _string(getattr(self, name), name)
        _role(self.incoming_role)
        _minimum_git(self.git_minimum)
        _boolean(self.require_platform, "require_platform")
        object.__setattr__(self, "source_roots", _roots(self.source_roots))
        object.__setattr__(self, "required_validators", _strings(self.required_validators, "required_validators"))
        if not isinstance(self.tools, Mapping) or any(not isinstance(name, str) or not isinstance(tool, Tool) for name, tool in self.tools.items()):
            raise ConfigError("tools: ожидается таблица имён и объектов Tool")
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        _tool_references(self.tools, self.required_validators, self.manual_tool)

    def to_dict(self) -> dict[str, Any]:
        result = {item.name: getattr(self, item.name) for item in fields(self) if item.name != "tools"}
        result["tools"] = {name: asdict(tool) for name, tool in self.tools.items()}
        return result

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


_KEYS = {
    "": {"schema_version", "project", "git", "resolution", "validation", "tools", "profiles", "xml", "bsl"},
    "project": {"source_roots", "platform_version"},
    "git": {"executable", "minimum_version", "rename_detection"},
    "resolution": {"manual_tool", "ambiguous"},
    "validation": {"required_tools", "require_platform"},
    "xml": {"forms"},
    "xml.forms": {"preserve_source", "placement", "id_policy"},
    "bsl": {"mode", "preserve_source"},
}
_TOOL_KEYS = {"adapter", "executable", "args", "timeout", "kind", "report_required"}
_PLACEHOLDERS = {"base", "local", "incoming", "result", "candidate", "report", "path", "project"}


def _table(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{label or 'Конфигурация'}: ожидается таблица TOML")
    return value


def _check_keys(data: dict[str, Any], section: str = "") -> None:
    allowed = _KEYS[section]
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"Неизвестные настройки {section or 'верхнего уровня'}: {', '.join(sorted(unknown))}")
    for key, value in data.items():
        child = f"{section}.{key}" if section else key
        if child in _KEYS:
            _check_keys(_table(value, child), child)


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"Не удалось прочитать {path}: {exc}") from exc


def _merge(target: dict[str, Any], extra: dict[str, Any]) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) for x in value):
        raise ConfigError(f"{label}: ожидается массив строк")
    return tuple(value)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label}: ожидается строка")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise ConfigError(f"{label}: ожидается true или false")
    return value


def _role(value: Any) -> str:
    role = _string(value, "incoming_role")
    if role not in {"vendor", "master", "incoming"}:
        raise ConfigError("incoming_role: допустимы vendor, master, incoming")
    return role


def _roots(value: Any) -> tuple[str, ...]:
    normalized = []
    for root in _strings(value, "project.source_roots"):
        path = PurePosixPath(root.replace("\\", "/"))
        if path.is_absolute() or ".." in path.parts or ":" in root or any(c in root for c in "*?\x00"):
            raise ConfigError(f"Корень исходников должен быть относительным каталогом: {root}")
        normalized.append(path.as_posix())
    return () if "." in normalized else tuple(dict.fromkeys(normalized))


def _minimum_git(value: Any) -> str:
    minimum = _string(value, "git.minimum_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", minimum) is None:
        raise ConfigError("git.minimum_version: ожидается версия вида 2.50.0")
    if tuple(int(part) for part in minimum.split(".")) < (2, 50):
        raise ConfigError("Минимальная версия Git — 2.50")
    return minimum


def _arguments(value: Any, label: str) -> tuple[str, ...]:
    args = _strings(value, label)
    for arg in args:
        if "\x00" in arg:
            raise ConfigError(f"{label}: недопустимый нулевой символ")
        try:
            parsed = list(string.Formatter().parse(arg))
            names = [item[1] for item in parsed if item[1] is not None]
        except ValueError as exc:
            raise ConfigError(f"Некорректный шаблон {label}: {exc}") from exc
        if any(name not in _PLACEHOLDERS for name in names) or any(item[2] or item[3] for item in parsed):
            raise ConfigError(f"Неизвестная подстановка {label}: {names}")
    return args


def _parse_tool(name: str, raw: Any) -> Tool:
    raw = _table(raw, f"tools.{name}")
    if set(raw) - _TOOL_KEYS:
        raise ConfigError(f"Неизвестные настройки tools.{name}: {set(raw) - _TOOL_KEYS}")
    adapter = raw.get("adapter", "command")
    return Tool(
        adapter=adapter,
        executable=raw.get("executable", ""),
        args=raw.get("args", ()),
        timeout=raw.get("timeout", 300),
        kind=raw.get("kind", "resolver" if adapter == "beyond_compare" else "validator"),
        report_required=raw.get("report_required", adapter == "platform"),
    )


def _tool_references(tools: Mapping[str, Tool], required: tuple[str, ...], manual: str) -> None:
    for name in required:
        if name not in tools or tools[name].kind != "validator":
            raise ConfigError(f"Обязательный валидатор не определён: {name}")
    if manual and (manual not in tools or tools[manual].kind != "resolver"):
        raise ConfigError(f"Инструмент разрешения конфликтов не определён: {manual}")


def _profile_overlay(name: str, raw: Any, builtin_role: str) -> tuple[dict[str, Any], str]:
    overlay = copy.deepcopy(_table(raw, f"profiles.{name}"))
    role = _role(overlay.pop("incoming_role", builtin_role))
    _check_keys(overlay)
    if "profiles" in overlay or "schema_version" in overlay:
        raise ConfigError("Профиль не может изменять schema_version или profiles")
    # Each profile must have a sound schema even when not selected. References
    # between sections are checked only after applying the selected overlay.
    value_types = {
        "project": {"source_roots": _strings, "platform_version": _string},
        "git": {"executable": _string, "minimum_version": _string, "rename_detection": _string},
        "resolution": {"manual_tool": _string, "ambiguous": _string},
        "validation": {"required_tools": _strings, "require_platform": _boolean},
        "xml.forms": {"preserve_source": _boolean, "placement": _string, "id_policy": _string},
        "bsl": {"mode": _string, "preserve_source": _boolean},
    }
    for section, validators in value_types.items():
        table = overlay
        for part in section.split("."):
            table = table.get(part, {})
        for key, value in table.items():
            validators[key](value, f"profiles.{name}.{section}.{key}")
    for tool_name, tool in _table(overlay.get("tools", {}), f"profiles.{name}.tools").items():
        _parse_tool(tool_name, tool)
    return overlay, role


def load_config(
    path: str | Path | None = None,
    *,
    local_path: str | Path | None = None,
    profile: str = "vendor_update",
) -> Config:
    """Load project policy, then profile, then restricted local tool bindings."""
    data = _read(Path(path)) if path is not None else {}
    _check_keys(data)
    version = data.get("schema_version", 1)
    if type(version) is not int or version != 1:
        raise ConfigError("Поддерживается schema_version = 1")
    profiles = _table(data.pop("profiles", {}), "profiles")
    builtin_roles = {"vendor_update": "vendor", "develop_sync": "master"}
    _string(profile, "profile")
    if profile not in builtin_roles and profile not in profiles:
        raise ConfigError(f"Неизвестный профиль: {profile}")
    overlays = {name: _profile_overlay(name, raw, builtin_roles.get(name, "incoming"))
                for name, raw in profiles.items()}
    selected, role = overlays.get(profile, ({}, builtin_roles.get(profile, "incoming")))
    _merge(data, selected)
    if local_path is not None:
        local = _read(Path(local_path))
        if set(local) - {"tools", "git"}:
            raise ConfigError("Локальный файл может задавать только tools и git.executable")
        if "git" in local and set(_table(local["git"], "git")) - {"executable"}:
            raise ConfigError("Локальный файл может изменять только git.executable")
        for name, binding in _table(local.get("tools", {}), "tools").items():
            if name not in data.get("tools", {}):
                raise ConfigError(f"Инструмент {name} должен быть сначала объявлен в файле проекта")
            if set(_table(binding, f"tools.{name}")) - {"executable"}:
                raise ConfigError("Локальный файл tools задаёт только executable; правила остаются в проекте")
        _merge(data, local)

    project = data.get("project", {})
    git = data.get("git", {})
    if git.get("rename_detection", "domain") != "domain":
        raise ConfigError("git.rename_detection: поддерживается только domain")
    resolution = data.get("resolution", {})
    if resolution.get("ambiguous", "manual") != "manual":
        raise ConfigError("resolution.ambiguous: поддерживается manual")
    forms = data.get("xml", {}).get("forms", {})
    expected = {
        "preserve_source": True,
        "placement": "after_incoming_in_same_gap",
        "id_policy": "preserve_and_remap_collisions",
    }
    for key, value in forms.items():
        if value != expected[key] or (key == "preserve_source" and type(value) is not bool):
            raise ConfigError(f"xml.forms.{key}: поддерживается {expected[key]!r}")
    bsl = data.get("bsl", {})
    if bsl.get("mode", "conservative") != "conservative" or bsl.get("preserve_source", True) is not True:
        raise ConfigError("BSL поддерживает mode = 'conservative' и preserve_source = true")
    validation = data.get("validation", {})
    tools = {name: _parse_tool(name, raw) for name, raw in _table(data.get("tools", {}), "tools").items()}
    return Config(
        profile=profile,
        incoming_role=role,
        source_roots=project.get("source_roots", ()),
        platform_version=_string(project.get("platform_version", "8.3.27.2342"), "project.platform_version"),
        project_root=str(Path(path).resolve().parent if path is not None else Path.cwd().resolve()),
        git_executable=_string(git.get("executable", "git"), "git.executable"),
        git_minimum=git.get("minimum_version", "2.50.0"),
        manual_tool=resolution.get("manual_tool", ""),
        required_validators=validation.get("required_tools", ()),
        require_platform=validation.get("require_platform", True),
        tools=tools,
    )


def config_from_snapshot(data: dict[str, Any]) -> Config:
    """Restore an already validated immutable session snapshot."""
    fields = copy.deepcopy(data)
    fields["source_roots"] = tuple(fields["source_roots"])
    fields["required_validators"] = tuple(fields["required_validators"])
    fields["tools"] = {name: Tool(**{**tool, "args": tuple(tool["args"])}) for name, tool in fields["tools"].items()}
    return Config(**fields)

