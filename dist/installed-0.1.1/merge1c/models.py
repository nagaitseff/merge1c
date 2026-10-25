"""Small public result types shared by the integration engines."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    severity: str = "conflict"
    path: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {"conflict", "error"}


@dataclass(frozen=True)
class Change:
    code: str
    message: str
    path: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class MergeResult:
    content: bytes | None
    issues: list[Issue] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    @property
    def resolved(self) -> bool:
        return self.content is not None and not any(issue.blocking for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolved": self.resolved,
            "sha256": sha256(self.content).hexdigest() if self.content is not None else None,
            "issues": [asdict(issue) for issue in self.issues],
            "changes": [asdict(change) for change in self.changes],
        }

