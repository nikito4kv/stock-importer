from __future__ import annotations

from enum import Enum


class AssetKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class ProviderCapability(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"


class RunStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunStage(str, Enum):
    IDLE = "idle"
    INGESTION = "ingestion"
    INTENT = "intent"
    PROVIDER_SEARCH = "provider_search"
    DOWNLOAD = "download"
    RELEVANCE = "relevance"
    PERSIST = "persist"
    COMPLETE = "complete"


class SessionHealth(str, Enum):
    UNKNOWN = "unknown"
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    CHALLENGE = "challenge_detected"
    EXPIRED = "expired"
    BLOCKED = "blocked"


class EventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
