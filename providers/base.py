from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from domain.enums import ProviderCapability


class ProviderFactory(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class ProviderDescriptor:
    provider_id: str
    display_name: str
    capability: ProviderCapability
    requires_auth: bool = False
    enabled_by_default: bool = True
    opt_in: bool = False
    legacy: bool = False
    license_policy: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
