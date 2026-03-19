from __future__ import annotations

from dataclasses import MISSING, dataclass, field, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
import types
from typing import Any, get_args, get_origin, get_type_hints

from .enums import AssetKind, ProviderCapability, RunStage, RunStatus, SessionHealth


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            item.name: _serialize_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value") and hasattr(value, "name"):
        return value.value
    if isinstance(value, dict):
        return {str(key): _serialize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize_value(item) for item in value]
    return value


def _deserialize_value(annotation: Any, value: Any) -> Any:
    if value is None:
        return None

    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is None:
        if isinstance(annotation, type):
            if issubclass(annotation, datetime):
                return datetime.fromisoformat(value)
            if issubclass(annotation, Path):
                return Path(value)
            if issubclass(
                annotation,
                (RunStatus, RunStage, AssetKind, ProviderCapability, SessionHealth),
            ):
                return annotation(value)
            if is_dataclass(annotation):
                return annotation.from_dict(value)
        return value

    if origin in {list, tuple, set}:
        inner = args[0] if args else Any
        items = [_deserialize_value(inner, item) for item in value]
        if origin is tuple:
            return tuple(items)
        if origin is set:
            return set(items)
        return items

    if origin is dict:
        key_type = args[0] if len(args) > 0 else Any
        value_type = args[1] if len(args) > 1 else Any
        return {
            _deserialize_value(key_type, key): _deserialize_value(value_type, item)
            for key, item in value.items()
        }

    if origin in {types.UnionType} or str(origin).endswith("Union"):
        non_none = [arg for arg in args if arg is not type(None)]
        for item in non_none:
            try:
                return _deserialize_value(item, value)
            except Exception:
                continue
        return value

    return value


class SerializableModel:
    def to_dict(self) -> dict[str, Any]:
        return {
            item.name: _serialize_value(getattr(self, item.name))
            for item in fields(self)
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        hints = get_type_hints(cls)
        payload: dict[str, Any] = {}
        for item in fields(cls):
            if item.name not in data:
                if item.default is not MISSING or item.default_factory is not MISSING:
                    continue
            value = data.get(item.name)
            payload[item.name] = _deserialize_value(hints.get(item.name, Any), value)
        return cls(**payload)


@dataclass(slots=True)
class QueryBundle(SerializableModel):
    video_queries: list[str] = field(default_factory=list)
    image_queries: list[str] = field(default_factory=list)
    provider_queries: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class ParagraphIntent(SerializableModel):
    paragraph_no: int
    primary_video_queries: list[str] = field(default_factory=list)
    image_queries: list[str] = field(default_factory=list)
    subject: str = ""
    action: str = ""
    setting: str = ""
    mood: str = ""
    style: str = ""
    negative_terms: list[str] = field(default_factory=list)
    source_language: str = ""
    translated_queries: list[str] = field(default_factory=list)
    estimated_duration_seconds: float | None = None


@dataclass(slots=True)
class ParagraphUnit(SerializableModel):
    paragraph_no: int
    original_index: int
    text: str
    numbering_valid: bool = True
    validation_issues: list[str] = field(default_factory=list)
    intent: ParagraphIntent | None = None
    query_bundle: QueryBundle | None = None


@dataclass(slots=True)
class ScriptDocument(SerializableModel):
    source_path: Path
    header_text: str
    schema_version: int = 1
    paragraphs: list[ParagraphUnit] = field(default_factory=list)
    numbering_issues: list[str] = field(default_factory=list)
    imported_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class AssetCandidate(SerializableModel):
    asset_id: str
    provider_name: str
    kind: AssetKind
    source_url: str | None = None
    local_path: Path | None = None
    license_name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ProviderResult(SerializableModel):
    provider_name: str
    capability: ProviderCapability
    query: str
    candidates: list[AssetCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AssetSelection(SerializableModel):
    paragraph_no: int
    primary_asset: AssetCandidate | None = None
    supporting_assets: list[AssetCandidate] = field(default_factory=list)
    fallback_assets: list[AssetCandidate] = field(default_factory=list)
    media_slots: list["MediaSlot"] = field(default_factory=list)
    provider_results: list[ProviderResult] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    user_decision_status: str = "auto_selected"
    reason: str = ""
    user_locked: bool = False
    status: str = "pending"


@dataclass(slots=True)
class MediaSlot(SerializableModel):
    slot_id: str
    kind: AssetKind
    role: str
    required: bool = False
    selected_asset_id: str | None = None
    user_locked: bool = False


@dataclass(slots=True)
class ParagraphDiagnostics(SerializableModel):
    paragraph_no: int
    provider_queries: dict[str, list[str]] = field(default_factory=dict)
    provider_results: list[ProviderResult] = field(default_factory=list)
    rejected_reasons: list[str] = field(default_factory=list)
    fanout_limits: dict[str, int] = field(default_factory=dict)
    dedupe_rejections: dict[str, int] = field(default_factory=dict)
    early_stop_triggered: bool = False
    selected_from_provider: str | None = None


@dataclass(slots=True)
class ParagraphManifestEntry(SerializableModel):
    paragraph_no: int
    original_index: int
    text: str
    intent: ParagraphIntent | None = None
    query_bundle: QueryBundle | None = None
    slots: list[MediaSlot] = field(default_factory=list)
    selection: AssetSelection | None = None
    diagnostics: ParagraphDiagnostics | None = None
    fallback_options: list[AssetCandidate] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    user_decision_status: str = "auto_selected"
    status: str = "pending"


@dataclass(slots=True)
class RunManifest(SerializableModel):
    run_id: str
    project_id: str
    project_name: str = ""
    schema_version: int = 1
    paragraph_entries: list[ParagraphManifestEntry] = field(default_factory=list)
    sourcing_strategy: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class BrowserProfile(SerializableModel):
    profile_id: str
    display_name: str
    storage_path: Path
    storyblocks_account: str | None = None
    launch_profile_dir_name: str = "Default"
    import_source_browser: str = ""
    import_source_root: Path | None = None
    import_source_profile_dir: Path | None = None
    import_source_profile_dir_name: str = ""
    import_source_profile_name: str = ""
    imported_at: datetime | None = None
    last_import_error: str = ""
    session_health: SessionHealth = SessionHealth.UNKNOWN
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    is_active: bool = False


@dataclass(slots=True)
class RunCheckpoint(SerializableModel):
    run_id: str
    stage: RunStage
    current_paragraph_no: int | None = None
    selected_paragraphs: list[int] = field(default_factory=list)
    completed_paragraphs: list[int] = field(default_factory=list)
    failed_paragraphs: list[int] = field(default_factory=list)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Run(SerializableModel):
    run_id: str
    project_id: str
    schema_version: int = 1
    status: RunStatus = RunStatus.DRAFT
    stage: RunStage = RunStage.IDLE
    selected_paragraphs: list[int] = field(default_factory=list)
    completed_paragraphs: list[int] = field(default_factory=list)
    failed_paragraphs: list[int] = field(default_factory=list)
    checkpoint: RunCheckpoint | None = None
    last_error: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Preset(SerializableModel):
    name: str
    settings_snapshot: dict[str, Any]
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class Project(SerializableModel):
    project_id: str
    name: str
    workspace_path: Path
    schema_version: int = 1
    script_document: ScriptDocument | None = None
    active_run_id: str | None = None
    active_browser_profile_id: str | None = None
    preset_names: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
