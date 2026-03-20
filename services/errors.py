from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class AppError(Exception):
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)
    retryable: bool = False
    fatal: bool = False

    def __str__(self) -> str:
        return self.message

    @property
    def error_code(self) -> str:
        return self.code

    def to_ui_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
            "retryable": bool(self.retryable),
            "fatal": bool(self.fatal),
        }


class ConfigError(AppError):
    pass


class SessionError(AppError):
    pass


class ProviderError(AppError):
    pass


class ProviderSearchError(ProviderError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        provider_id: str,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        fatal: bool = False,
    ) -> None:
        payload = dict(details or {})
        payload.setdefault("provider_id", provider_id)
        payload.setdefault("retryable", bool(retryable))
        payload.setdefault("fatal", bool(fatal))
        super().__init__(
            code=code,
            message=message,
            details=payload,
            retryable=retryable,
            fatal=fatal,
        )
        self.provider_id = provider_id


class DownloadError(AppError):
    pass


class RelevanceError(AppError):
    pass


class PersistenceError(AppError):
    pass
