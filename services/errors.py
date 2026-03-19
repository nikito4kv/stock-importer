from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def to_ui_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class ConfigError(AppError):
    pass


class SessionError(AppError):
    pass


class ProviderError(AppError):
    pass


class DownloadError(AppError):
    pass


class RelevanceError(AppError):
    pass


class PersistenceError(AppError):
    pass
