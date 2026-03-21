from __future__ import annotations

import re
import threading
from dataclasses import dataclass, fields, is_dataclass, replace
from json import JSONDecodeError
from pathlib import Path
from typing import Callable

from app.runtime import DesktopApplication
from config.settings import ApplicationSettings
from domain.enums import RunStage, RunStatus, SessionHealth
from domain.models import (
    AssetCandidate,
    AssetSelection,
    LiveDownloadedFileSnapshot,
    LiveRunStateSnapshot,
    ParagraphIntent,
    Preset,
    Project,
    QueryBundle,
    Run,
    RunManifest,
    utc_now,
)
from domain.project_modes import (
    DEFAULT_FREE_IMAGE_PROVIDER_IDS,
    get_project_mode,
    normalize_free_image_provider_ids,
    normalize_project_mode,
    provider_ids_for_mode,
)
from pipeline import MediaSelectionConfig
from providers import resolve_execution_concurrency_mode
from services.errors import AppError
from services.events import AppEvent

from .contracts import (
    UiAdvancedSettingsViewModel,
    UiDownloadedFileItem,
    UiImportableBrowserProfileOption,
    UiLiveRunStateViewModel,
    UiLiveSnapshotViewModel,
    UiNotification,
    UiParagraphWorkbenchItem,
    UiPresetViewModel,
    UiProjectSummary,
    UiQuickLaunchSettingsViewModel,
    UiRunHistoryItem,
    UiRunPreviewViewModel,
    UiRunProgressViewModel,
    UiSessionPanelViewModel,
    UiStateViewModel,
)
from .launch_profiles import (
    LaunchProfileCustomTiming,
    ResolvedLaunchProfile,
    default_custom_timing,
    describe_custom_timing_overrides,
    normalize_launch_profile_id,
    resolve_launch_profile,
)
from .presentation import (
    normalize_ui_theme,
    translate_error_text,
)


def _iso(value) -> str:
    return value.isoformat() if value is not None else ""


@dataclass(slots=True)
class _BackgroundRunTask:
    run_id: str
    thread: threading.Thread
    project_id: str
    notification_sent: bool = False
    error: Exception | None = None


class DesktopGuiController:
    def __init__(self, application: DesktopApplication):
        self.application = application
        self.notifications: list[UiNotification] = []
        self._project_cache: dict[str, Project] = {}
        self._background_run: _BackgroundRunTask | None = None
        self._background_lock = threading.Lock()

    @classmethod
    def create(cls, workspace_root: str | Path | None = None) -> "DesktopGuiController":
        return cls(DesktopApplication.create(workspace_root))

    def session_actions_enabled(self) -> bool:
        with self._background_lock:
            return not (
                self._background_run is not None
                and self._background_run.thread.is_alive()
            )

    def _ensure_session_actions_available(self) -> None:
        if not self.session_actions_enabled():
            raise AppError(
                "run_in_progress",
                "Во время активного запуска нельзя менять сессию Storyblocks.",
            )

    def build_state(
        self, *, active_project_id: str | None = None, active_run_id: str | None = None
    ) -> UiStateViewModel:
        self._finalize_background_run_if_needed()
        active_run_id = self._resolve_active_run_id(active_project_id, active_run_id)
        state = UiStateViewModel(
            active_project_id=active_project_id,
            active_run_id=active_run_id,
            status_text=self._status_text(active_run_id),
            projects=self.list_projects(),
            run_history=self.list_run_history(),
            presets=self.list_presets(),
            session=self.session_panel(),
            quick_launch=self.build_quick_launch_settings(),
            advanced=self.build_advanced_settings(),
            notifications=list(self.notifications[-10:]),
        )
        if active_run_id is not None:
            state.run_progress = self.build_run_progress(
                active_run_id, active_project_id=active_project_id
            )
        if active_project_id is not None:
            state.paragraph_items = self.build_paragraph_workbench(
                active_project_id, active_run_id
            )
            state.run_preview = self.build_run_preview(
                active_project_id,
                state.quick_launch,
                state.advanced,
            )
        if state.run_progress is not None:
            state.progress_total = state.run_progress.project_progress_total
            state.progress_completed = state.run_progress.project_progress_completed
        else:
            state.progress_total = len(state.paragraph_items)
            state.progress_completed = sum(
                1 for item in state.paragraph_items if item.status == "completed"
            )
        return state

    def build_live_run_state(
        self,
        *,
        active_project_id: str | None = None,
        active_run_id: str | None = None,
        selected_paragraph_no: int | None = None,
        live_snapshot: UiLiveSnapshotViewModel | None = None,
    ) -> UiLiveRunStateViewModel:
        live = live_snapshot or self.build_live_snapshot(
            active_project_id=active_project_id,
            active_run_id=active_run_id,
        )
        state = UiLiveRunStateViewModel(
            active_run_id=live.active_run_id,
            status_text=live.status_text,
            run_progress=live.run_progress,
        )
        resolved_run_id = live.active_run_id
        if resolved_run_id is None or active_project_id is None:
            return state

        detailed_paragraph_no = selected_paragraph_no
        if detailed_paragraph_no is None and state.run_progress is not None:
            detailed_paragraph_no = state.run_progress.current_paragraph_no
        live_run_state = self._safe_snapshot_live_run_state(
            resolved_run_id,
            detailed_paragraph_no=detailed_paragraph_no,
        )
        if live_run_state is None:
            return state
        latest_events = (
            self.application.container.event_recorder.latest_by_paragraph_for_run(
                resolved_run_id
            )
        )
        state.paragraph_items = self.build_paragraph_workbench(
            active_project_id,
            resolved_run_id,
            live_run_state=live_run_state,
            latest_events=latest_events,
            detailed_paragraph_no=detailed_paragraph_no,
        )
        return state

    def build_live_snapshot(
        self,
        *,
        active_project_id: str | None = None,
        active_run_id: str | None = None,
    ) -> UiLiveSnapshotViewModel:
        self._finalize_background_run_if_needed()
        resolved_run_id = self._resolve_active_run_id(active_project_id, active_run_id)
        state = UiLiveSnapshotViewModel(
            active_run_id=resolved_run_id,
            status_text="готово",
        )
        if resolved_run_id is None:
            return state

        run = self._safe_load_run(resolved_run_id)
        latest_event = self.application.container.event_recorder.latest_for_run(
            resolved_run_id
        )
        state.run_progress = self.build_run_progress(
            resolved_run_id,
            active_project_id=active_project_id,
            run=run,
            manifest=None,
            latest_event=latest_event,
            project=None,
            load_manifest=False,
            load_project=False,
        )
        if state.run_progress is not None:
            current = (
                f" абзац {state.run_progress.current_paragraph_no}"
                if state.run_progress.current_paragraph_no is not None
                else ""
            )
            state.status_text = (
                f"{state.run_progress.status}: {state.run_progress.live_state}{current}"
            )
        return state

    def _resolve_active_run_id(
        self, active_project_id: str | None, active_run_id: str | None
    ) -> str | None:
        if active_run_id is not None or active_project_id is None:
            return active_run_id
        try:
            project = self._require_project(active_project_id)
        except KeyError:
            return None
        return project.active_run_id

    def list_projects(self) -> list[UiProjectSummary]:
        projects = self.application.container.project_repository.list_all()
        summaries: list[UiProjectSummary] = []
        for project in projects:
            document = project.script_document
            summaries.append(
                UiProjectSummary(
                    project_id=project.project_id,
                    name=project.name,
                    source_path=str(document.source_path)
                    if document is not None
                    else "",
                    paragraphs_total=len(document.paragraphs)
                    if document is not None
                    else 0,
                    header_text=document.header_text if document is not None else "",
                    numbering_issues=list(document.numbering_issues)
                    if document is not None
                    else [],
                    active_run_id=project.active_run_id,
                    updated_at=_iso(project.updated_at),
                )
            )
        return summaries

    def list_run_history(self, project_id: str | None = None) -> list[UiRunHistoryItem]:
        runs = self.application.container.run_repository.list_all()
        manifests = {
            manifest.run_id: manifest
            for manifest in self.application.container.manifest_repository.list_all()
        }
        items: list[UiRunHistoryItem] = []
        for run in runs:
            if project_id is not None and run.project_id != project_id:
                continue
            manifest = manifests.get(run.run_id)
            summary = manifest.summary if manifest is not None else {}
            items.append(
                UiRunHistoryItem(
                    run_id=run.run_id,
                    project_id=run.project_id,
                    status=run.status.value,
                    created_at=_iso(run.created_at),
                    finished_at=_iso(run.finished_at),
                    paragraphs_completed=int(
                        summary.get(
                            "paragraphs_completed", len(run.completed_paragraphs)
                        )
                    ),
                    paragraphs_failed=int(
                        summary.get("paragraphs_failed", len(run.failed_paragraphs))
                    ),
                    stage=run.stage.value,
                )
            )
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def list_presets(self) -> list[UiPresetViewModel]:
        presets = self.application.container.settings_manager.list_preset_objects()
        return [
            UiPresetViewModel(name=item.name, updated_at=_iso(item.updated_at))
            for item in presets
        ]

    def build_quick_launch_settings(self) -> UiQuickLaunchSettingsViewModel:
        return self._quick_launch_from_settings(self.application.container.settings)

    def build_advanced_settings(self) -> UiAdvancedSettingsViewModel:
        return self._advanced_settings_from_settings(
            self.application.container.settings
        )

    def _quick_launch_from_settings(
        self, settings: ApplicationSettings
    ) -> UiQuickLaunchSettingsViewModel:
        provider_ids = normalize_free_image_provider_ids(
            [
                provider_id
                for provider_id in settings.providers.enabled_providers
                if provider_id in DEFAULT_FREE_IMAGE_PROVIDER_IDS
            ]
        )
        return UiQuickLaunchSettingsViewModel(
            output_dir=str(self.application.container.workspace.paths.runs_dir),
            mode_id=normalize_project_mode(settings.providers.project_mode),
            launch_profile_id=self._infer_launch_profile_id(settings),
            strictness="balanced",
            provider_ids=provider_ids,
            supporting_image_limit=max(0, settings.providers.supporting_image_limit),
            fallback_image_limit=max(0, settings.providers.fallback_image_limit),
        )

    def _advanced_settings_from_settings(
        self, settings: ApplicationSettings
    ) -> UiAdvancedSettingsViewModel:
        return UiAdvancedSettingsViewModel(
            action_delay_ms=max(0, int(settings.browser.action_delay_ms)),
            launch_timeout_ms=settings.browser.launch_timeout_ms,
            navigation_timeout_ms=settings.browser.navigation_timeout_ms,
            downloads_timeout_seconds=settings.browser.downloads_timeout_seconds,
        )

    def _forms_from_settings(
        self, settings: ApplicationSettings
    ) -> tuple[UiQuickLaunchSettingsViewModel, UiAdvancedSettingsViewModel]:
        return (
            self._quick_launch_from_settings(settings),
            self._advanced_settings_from_settings(settings),
        )

    def open_script(
        self, script_path: str | Path, *, project_name: str | None = None
    ) -> UiProjectSummary:
        path = Path(script_path)
        name = project_name or path.stem.replace("_", " ").strip() or "Проект"
        project = self._remember_project(self.application.create_project(name, path))
        self.notifications.append(
            UiNotification(
                "Сценарий загружен", f"Импортирован файл {path.name}", "success"
            )
        )
        if (
            project.script_document is not None
            and project.script_document.numbering_issues
        ):
            self.notifications.append(
                UiNotification(
                    "Проблемы с нумерацией",
                    "Исправьте нумерацию сценария перед запуском проекта",
                    "warning",
                    {"issues": list(project.script_document.numbering_issues)},
                )
            )
        return self._project_summary(project)

    def build_run_preview(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> UiRunPreviewViewModel:
        project = self._require_project(project_id)
        document = project.script_document
        resolved_quick = self._resolve_quick_launch_settings(project, quick)
        launch_profile = self._resolve_runtime_launch_profile(resolved_quick, advanced)
        mode = get_project_mode(quick.mode_id)
        selected = resolved_quick.selected_paragraphs or [
            paragraph.paragraph_no
            for paragraph in (document.paragraphs if document is not None else [])
        ]
        providers = self._selected_provider_ids(resolved_quick)
        warnings = list(document.numbering_issues) if document is not None else []
        free_provider_ids = [
            provider_id
            for provider_id in providers
            if provider_id in DEFAULT_FREE_IMAGE_PROVIDER_IDS
        ]
        usable_free_provider_ids = self._usable_free_image_provider_ids(
            free_provider_ids
        )
        if mode.free_images_enabled and not free_provider_ids:
            warnings.append(
                "Для выбранного режима нужно включить хотя бы один источник бесплатных изображений"
            )
        warnings.extend(self._free_provider_unavailability_warnings(free_provider_ids))
        if (
            mode.free_images_enabled
            and free_provider_ids
            and not usable_free_provider_ids
        ):
            warnings.append(
                "Для бесплатных изображений сейчас нет ни одного доступного провайдера. Добавьте API key для Pexels/Pixabay или оставьте включенным Openverse."
            )
        if (
            mode.requires_storyblocks
            and self.session_panel().health != SessionHealth.READY.value
        ):
            warnings.append("Сессия Storyblocks не готова для выбранного режима")
        if not self.get_gemini_key():
            warnings.append("Ключ Gemini пока не задан")
        if (
            resolved_quick.attach_full_script_context
            and not self.application.container.settings.ai.full_script_context_enabled
        ):
            warnings.append(
                "Полный AI-контекст сценария выключен в текущей конфигурации."
            )
        scope_label = (
            f"Только выбранные абзацы: {len(selected)}"
            if resolved_quick.selected_paragraphs
            else "Будут обработаны все абзацы"
        )
        summary_lines = [
            scope_label,
            f"Профиль запуска: {launch_profile.label}",
            f"Основные изображения: {resolved_quick.supporting_image_limit}",
            f"Резервные изображения: {resolved_quick.fallback_image_limit}",
            "Сохраняются лучшие кандидаты по стратегии, а не по одному файлу с каждого сервиса.",
        ]
        if launch_profile.launch_profile_id == "custom":
            overrides = describe_custom_timing_overrides(
                self._custom_timing_from_advanced(advanced)
            )
            if overrides:
                summary_lines.append(f"Custom: {', '.join(overrides)}")
        if resolved_quick.paragraph_selection_text.strip():
            summary_lines.append(
                f"Диапазон абзацев: {resolved_quick.paragraph_selection_text.strip()}"
            )
        if resolved_quick.manual_prompt.strip():
            summary_lines.append("Дополнительный AI prompt: задан")
        if resolved_quick.attach_full_script_context:
            summary_lines.append("AI-контекст: прикреплять весь сценарий")
        return UiRunPreviewViewModel(
            project_name=project.name,
            output_dir=resolved_quick.output_dir
            or str(self.application.container.workspace.paths.runs_dir),
            paragraphs_total=len(document.paragraphs) if document is not None else 0,
            selected_paragraphs=len(selected),
            mode_id=mode.mode_id,
            mode_label=mode.label,
            providers=providers,
            warnings=warnings,
            summary_lines=summary_lines,
            session_health=self.session_panel().health,
        )

    def build_paragraph_workbench(
        self,
        project_id: str,
        run_id: str | None = None,
        *,
        manifest: RunManifest | None = None,
        live_run_state: LiveRunStateSnapshot | None = None,
        latest_events: dict[int, AppEvent] | None = None,
        detailed_paragraph_no: int | None = None,
    ) -> list[UiParagraphWorkbenchItem]:
        project = self._require_project(project_id)
        document = project.script_document
        if document is None:
            return []
        manifest = (
            manifest
            if manifest is not None
            else (
                self._safe_load_manifest(run_id)
                if run_id is not None and live_run_state is None
                else None
            )
        )
        manifest_entries = (
            {entry.paragraph_no: entry for entry in manifest.paragraph_entries}
            if manifest is not None
            else {}
        )
        live_states = (
            dict(live_run_state.paragraph_states) if live_run_state is not None else {}
        )
        paragraphs_in_run = set(manifest_entries) | set(live_states)
        latest_events = (
            latest_events
            if latest_events is not None
            else self._latest_events_by_paragraph(run_id)
        )

        items: list[UiParagraphWorkbenchItem] = []
        for paragraph in document.paragraphs:
            entry = manifest_entries.get(paragraph.paragraph_no)
            live_state = live_states.get(paragraph.paragraph_no)
            latest_event = latest_events.get(paragraph.paragraph_no)
            include_detail = (
                detailed_paragraph_no is None
                or paragraph.paragraph_no == detailed_paragraph_no
            )
            has_downloaded_files = (
                bool(live_state.downloaded_files)
                if live_state is not None
                else self._has_downloaded_files(
                    entry.selection if entry is not None else None
                )
            )
            downloaded_files = (
                self._live_downloaded_files(live_state.downloaded_files)
                if include_detail and live_state is not None
                else (
                    self._downloaded_files(
                        entry.selection if entry is not None else None
                    )
                    if include_detail
                    else []
                )
            )
            if live_state is not None:
                status = self._normalize_paragraph_status(
                    live_state.status,
                    has_downloaded_files=has_downloaded_files,
                )
                result_note = live_state.result_note
            elif entry is not None:
                status = self._normalize_paragraph_status(
                    entry.status,
                    has_downloaded_files=has_downloaded_files,
                )
                result_note = self._entry_result_note(entry)
            elif run_id is not None and paragraphs_in_run:
                status = "skipped"
                result_note = ""
            else:
                status = "pending"
                result_note = ""
            items.append(
                UiParagraphWorkbenchItem(
                    paragraph_no=paragraph.paragraph_no,
                    original_index=paragraph.original_index,
                    text=paragraph.text if include_detail else "",
                    numbering_valid=paragraph.numbering_valid,
                    validation_issues=list(paragraph.validation_issues),
                    status=status,
                    result_note=result_note,
                    intent_summary=self._intent_summary(paragraph.intent),
                    current_stage=latest_event.stage.value
                    if latest_event is not None and latest_event.stage is not None
                    else "",
                    current_provider_name=latest_event.provider_name or ""
                    if latest_event is not None
                    else "",
                    current_query=latest_event.query or ""
                    if latest_event is not None
                    else "",
                    video_queries=list(
                        paragraph.query_bundle.video_queries
                        if include_detail and paragraph.query_bundle is not None
                        else []
                    ),
                    image_queries=list(
                        paragraph.query_bundle.image_queries
                        if include_detail and paragraph.query_bundle is not None
                        else []
                    ),
                    downloaded_files=downloaded_files,
                )
            )
        return items

    def build_paragraph_detail(
        self, project_id: str, paragraph_no: int, run_id: str | None = None
    ) -> UiParagraphWorkbenchItem | None:
        for item in self.build_paragraph_workbench(
            project_id,
            run_id,
            detailed_paragraph_no=paragraph_no,
        ):
            if item.paragraph_no == paragraph_no:
                return item
        return None

    def update_paragraph_queries(
        self,
        project_id: str,
        paragraph_no: int,
        *,
        video_queries: list[str],
        image_queries: list[str],
    ) -> Project:
        project = self._require_project(project_id)
        paragraph = self._require_paragraph(project, paragraph_no)
        current_intent = paragraph.intent or ParagraphIntent(paragraph_no=paragraph_no)
        updated_intent = ParagraphIntent.from_dict(current_intent.to_dict())
        updated_intent.primary_video_queries = [
            item.strip() for item in video_queries if item.strip()
        ]
        updated_intent.image_queries = [
            item.strip() for item in image_queries if item.strip()
        ]

        current_bundle = paragraph.query_bundle or QueryBundle()
        provider_queries = dict(current_bundle.provider_queries)
        provider_queries["storyblocks_video"] = list(
            updated_intent.primary_video_queries
        )
        provider_queries["storyblocks_image"] = list(updated_intent.image_queries)
        provider_queries["free_image"] = list(updated_intent.image_queries)
        query_bundle = QueryBundle(
            video_queries=list(updated_intent.primary_video_queries),
            image_queries=list(updated_intent.image_queries),
            provider_queries=provider_queries,
        )
        updated = self.application.update_paragraph_intent(
            project_id,
            paragraph_no,
            intent=updated_intent,
            query_bundle=query_bundle,
        )
        self._remember_project(updated)
        self.notifications.append(
            UiNotification(
                "Абзац обновлен",
                f"Запросы сохранены для абзаца {paragraph_no}",
                "success",
            )
        )
        return updated

    def enrich_project_intents_with_ai(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> Project:
        project = self._require_project(project_id)
        quick = self._resolve_quick_launch_settings(project, quick)
        updated = self.application.enrich_project_intents(
            project_id,
            strictness=quick.strictness,
            manual_prompt=quick.manual_prompt,
            attach_full_script_context=quick.attach_full_script_context,
        )
        self._remember_project(updated)
        self.notifications.append(
            UiNotification(
                "AI enrichment",
                f"Gemini обновил intent/query bundle для проекта {updated.name}",
                "success",
            )
        )
        return updated

    def execute_run(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> tuple[Run, RunManifest]:
        quick = self._validate_run_request(project_id, quick, advanced)
        self.apply_forms_to_settings(quick, advanced)
        run, manifest = self.application.execute_media_run(
            project_id,
            selected_paragraphs=quick.selected_paragraphs or None,
            config=self._media_config_from_forms(quick, advanced),
        )
        self._remember_active_run(project_id, run.run_id)
        self.notifications.append(
            UiNotification(
                "Запуск завершен",
                f"Запуск {run.run_id} завершился со статусом {run.status.value}",
                "success",
            )
        )
        return run, manifest

    def start_run_async(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> str:
        quick = self._validate_run_request(project_id, quick, advanced)
        self.apply_forms_to_settings(quick, advanced)
        config = self._media_config_from_forms(quick, advanced)
        run, _manifest = self.application.container.media_run_service.create_run(
            project_id,
            selected_paragraphs=quick.selected_paragraphs or None,
            config=config,
        )
        self._remember_active_run(project_id, run.run_id)
        self._start_background_run(
            run.run_id,
            project_id,
            lambda: self.application.container.media_run_service.execute(
                run.run_id, config=config
            ),
        )
        self._push_notification(
            UiNotification("Запуск начат", f"Запуск {run.run_id} выполняется", "info")
        )
        return run.run_id

    def rerun_full_run(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> tuple[Run, RunManifest]:
        full_quick = replace(quick, selected_paragraphs=[], paragraph_selection_text="")
        full_quick = self._validate_run_request(project_id, full_quick, advanced)
        self.apply_forms_to_settings(full_quick, advanced)
        run, manifest = self.application.rerun_full_media_run(
            project_id=project_id,
            config=self._media_config_from_forms(full_quick, advanced),
        )
        self._remember_active_run(project_id, run.run_id)
        self._push_notification(
            UiNotification(
                "Полный повтор завершен",
                f"Создан новый полный запуск {run.run_id} со статусом {run.status.value}",
                "success",
            )
        )
        return run, manifest

    def rerun_full_run_async(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> str:
        full_quick = replace(quick, selected_paragraphs=[], paragraph_selection_text="")
        full_quick = self._validate_run_request(project_id, full_quick, advanced)
        self.apply_forms_to_settings(full_quick, advanced)
        config = self._media_config_from_forms(full_quick, advanced)
        run, _manifest = self.application.container.media_run_service.create_run(
            project_id,
            config=config,
        )
        self._remember_active_run(project_id, run.run_id)
        self._start_background_run(
            run.run_id,
            project_id,
            lambda: self.application.container.media_run_service.execute(
                run.run_id, config=config
            ),
        )
        self._push_notification(
            UiNotification(
                "Полный повтор начат",
                f"Создан новый полный запуск {run.run_id}",
                "info",
            )
        )
        return run.run_id

    def cancel_run(self, run_id: str) -> None:
        self.application.container.media_run_service.cancel(run_id)
        self._push_notification(
            UiNotification(
                "Запрошена остановка",
                f"Для запуска {run_id} запрошена отмена",
                "warning",
            )
        )

    def session_panel(self) -> UiSessionPanelViewModel:
        profile = self.application.container.profile_registry.get_or_create_singleton()
        state = self.application.container.session_manager.current_state()
        diagnostic_lines = [
            f"{key}: {value}"
            for key, value in sorted(state.diagnostics.items())
            if str(value).strip()
        ]
        return UiSessionPanelViewModel(
            health=state.health.value,
            account=state.storyblocks_account or "",
            browser_ready=state.persistent_context_ready,
            native_login_running=state.native_login_running,
            native_debug_port=state.native_debug_port,
            current_url=state.current_url or "",
            manual_prompt=state.manual_intervention.prompt
            if state.manual_intervention is not None
            else "",
            last_error=state.last_error or profile.last_import_error or "",
            indicator_tone=self._session_indicator_tone(state.health),
            imported_source=str(profile.import_source_root)
            if profile.import_source_root is not None
            else "",
            imported_profile_name=profile.import_source_profile_name,
            imported_at=_iso(profile.imported_at),
            manual_ready_override=state.manual_ready_override,
            manual_ready_override_note=state.manual_ready_override_note or "",
            reason_code=state.reason_code,
            diagnostic_lines=diagnostic_lines,
        )

    def discover_importable_browser_profiles(
        self, browser_name: str | None = None
    ) -> list[UiImportableBrowserProfileOption]:
        options = self.application.container.profile_import_service.discover_profiles(
            browser_name
        )
        return [
            UiImportableBrowserProfileOption(
                browser_name=item.browser_name,
                browser_label=item.browser_label,
                profile_name=item.profile_label,
                profile_dir=str(item.profile_dir),
                user_data_root=str(item.user_data_root),
                display_label=item.display_label,
                locked=item.locked,
            )
            for item in options
        ]

    def check_storyblocks_session(self) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        state = self.application.container.session_manager.check_authorization(
            persist_handle=False
        )
        if state.health == SessionHealth.READY:
            self.notifications.append(
                UiNotification(
                    "Сессия Storyblocks",
                    "Сессия готова, автоматизация может продолжиться в этом же браузере.",
                    "success",
                )
            )
        elif state.last_error:
            self.notifications.append(
                UiNotification(
                    "Сессия Storyblocks",
                    translate_error_text(state.last_error),
                    "warning",
                )
            )
        elif state.manual_intervention is not None:
            self.notifications.append(
                UiNotification(
                    "Сессия Storyblocks",
                    translate_error_text(state.manual_intervention.prompt),
                    "info",
                )
            )
        return self.session_panel()

    def mark_storyblocks_session_ready(self) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        self.application.container.session_manager.set_manual_ready_override()
        self.notifications.append(
            UiNotification(
                "Сессия Storyblocks",
                "Ручное подтверждение сессии включено. Следующая страница Storyblocks все равно будет автоматически проверена.",
                "warning",
            )
        )
        return self.session_panel()

    def clear_storyblocks_session_override(self) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        self.application.container.session_manager.clear_manual_ready_override()
        self.notifications.append(
            UiNotification(
                "Сессия Storyblocks",
                "Ручное подтверждение сессии отключено.",
                "info",
            )
        )
        return self.session_panel()

    def prepare_storyblocks_login(self) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        self.application.container.session_manager.open_native_login_browser()
        self.notifications.append(
            UiNotification(
                "Вход в браузере",
                "Войдите в Storyblocks в открытом окне браузера, не закрывайте его и затем нажмите Проверить сессию.",
                "info",
            )
        )
        return self.session_panel()

    def logout_storyblocks(self) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        profile = self.application.container.profile_registry.get_or_create_singleton()
        self.application.container.session_manager.close_browser()
        self.application.container.session_manager.close_native_browser()
        self.application.container.profile_registry.update_storyblocks_account(
            profile.profile_id, None
        )
        self.application.container.session_manager.set_health(
            SessionHealth.LOGIN_REQUIRED
        )
        self.notifications.append(
            UiNotification(
                "Сессия Storyblocks",
                "Сессия завершена. Чтобы работать дальше, снова войдите в браузере и проверьте сессию.",
                "info",
            )
        )
        return self.session_panel()

    def reset_storyblocks_session(self) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        self.application.container.session_manager.reset_session_state()
        self.notifications.append(
            UiNotification(
                "Сессия Storyblocks",
                "Состояние сессии сброшено. Войдите заново и выполните Проверить сессию.",
                "warning",
            )
        )
        return self.session_panel()

    def import_storyblocks_session_from_path(
        self,
        source_profile_dir: str | Path,
        *,
        browser_name: str | None = None,
    ) -> UiSessionPanelViewModel:
        self._ensure_session_actions_available()
        self.application.container.session_manager.close_browser()
        self.application.container.session_manager.close_native_browser()
        source = self.application.container.profile_import_service.resolve_source(
            source_profile_dir, browser_name=browser_name
        )
        self.application.container.profile_import_service.import_profile(source)
        self.application.container.session_manager.restore_session()
        self.notifications.append(
            UiNotification(
                "Сессия импортирована",
                f"Профиль {source.profile_label} из {source.browser_label} импортирован в управляемый профиль Storyblocks.",
                "success",
            )
        )
        return self.session_panel()

    def save_preset(
        self,
        name: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> Preset:
        settings = self._build_settings_snapshot(quick, advanced)
        preset = Preset(name=name, settings_snapshot=settings)
        saved = self.application.container.settings_manager.save_preset(preset)
        self.notifications.append(
            UiNotification("Пресет сохранен", f"Пресет {name} сохранен", "success")
        )
        return saved

    def load_preset(
        self, name: str
    ) -> tuple[UiQuickLaunchSettingsViewModel, UiAdvancedSettingsViewModel]:
        preset = self.application.container.settings_manager.load_preset(name)
        if preset is None:
            raise KeyError(name)
        if self._is_compact_preset_snapshot(preset.settings_snapshot):
            quick, advanced = self._forms_from_compact_preset_snapshot(
                preset.settings_snapshot
            )
        else:
            settings = self.application.container.settings_manager.apply_preset(
                self.application.container.settings, name
            )
            quick, advanced = self._forms_from_settings(settings)
        self.apply_forms_to_settings(quick, advanced)
        return quick, advanced

    def export_preset(self, name: str, destination: str | Path) -> Path:
        return self.application.container.settings_manager.export_preset(
            name, destination
        )

    def import_preset(self, source: str | Path) -> Preset:
        preset = self.application.container.settings_manager.import_preset(source)
        self.notifications.append(
            UiNotification(
                "Пресет импортирован", f"Пресет {preset.name} импортирован", "success"
            )
        )
        return preset

    def _is_compact_preset_snapshot(self, snapshot: dict[str, object]) -> bool:
        return isinstance(snapshot.get("quick_launch"), dict)

    def _forms_from_compact_preset_snapshot(
        self, snapshot: dict[str, object]
    ) -> tuple[UiQuickLaunchSettingsViewModel, UiAdvancedSettingsViewModel]:
        quick_payload = snapshot.get("quick_launch", {})
        if not isinstance(quick_payload, dict):
            quick_payload = {}
        quick = self.build_quick_launch_settings()
        quick.mode_id = normalize_project_mode(
            str(quick_payload.get("mode_id", quick.mode_id))
        )
        quick.launch_profile_id = normalize_launch_profile_id(
            str(snapshot.get("launch_profile_id", quick.launch_profile_id))
        )
        quick.strictness = str(quick_payload.get("strictness", quick.strictness))
        provider_ids = quick_payload.get("provider_ids")
        if isinstance(provider_ids, list):
            quick.provider_ids = normalize_free_image_provider_ids(provider_ids)
        quick.supporting_image_limit = max(
            0,
            int(
                quick_payload.get(
                    "supporting_image_limit", quick.supporting_image_limit
                )
            ),
        )
        quick.fallback_image_limit = max(
            0,
            int(quick_payload.get("fallback_image_limit", quick.fallback_image_limit)),
        )
        quick.manual_prompt = str(quick_payload.get("manual_prompt", "")).strip()
        quick.attach_full_script_context = bool(
            quick_payload.get("attach_full_script_context", False)
        )
        advanced = self.build_advanced_settings()
        overrides = snapshot.get("custom_timing_overrides", {})
        if quick.launch_profile_id == "custom":
            if not isinstance(overrides, dict):
                overrides = {}
            default_timing = default_custom_timing()
            advanced.action_delay_ms = max(
                0,
                int(overrides.get("action_delay_ms", default_timing.action_delay_ms)),
            )
            advanced.launch_timeout_ms = max(
                1000,
                int(
                    overrides.get("launch_timeout_ms", default_timing.launch_timeout_ms)
                ),
            )
            advanced.navigation_timeout_ms = max(
                1000,
                int(
                    overrides.get(
                        "navigation_timeout_ms",
                        default_timing.navigation_timeout_ms,
                    )
                ),
            )
            advanced.downloads_timeout_seconds = max(
                1.0,
                float(
                    overrides.get(
                        "downloads_timeout_seconds",
                        default_timing.downloads_timeout_seconds,
                    )
                ),
            )
        return quick, advanced

    def set_gemini_key(self, key: str) -> UiNotification:
        validation = self.validate_gemini_key(key)
        if validation.severity == "error":
            return validation
        self.application.container.settings_manager.set_secret(
            self.application.container.settings.security.gemini_api_key_secret_name,
            key,
        )
        self.notifications.append(validation)
        return validation

    def set_provider_api_key(self, provider_id: str, key: str) -> UiNotification:
        trimmed = key.strip()
        if not trimmed:
            return UiNotification(
                "Ключ провайдера",
                f"Ключ для {self._provider_display_name(provider_id)} пустой.",
                "error",
            )
        self.application.container.settings_manager.set_secret(
            self._provider_api_secret_name(provider_id),
            trimmed,
        )
        self._rebuild_free_image_backends()
        notification = UiNotification(
            "Ключ провайдера",
            f"Ключ для {self._provider_display_name(provider_id)} сохранен.",
            "success",
        )
        self.notifications.append(notification)
        return notification

    def get_provider_api_key(self, provider_id: str) -> str | None:
        return self.application.container.settings_manager.get_secret(
            self._provider_api_secret_name(provider_id)
        )

    def delete_provider_api_key(self, provider_id: str) -> UiNotification:
        self.application.container.settings_manager.delete_secret(
            self._provider_api_secret_name(provider_id)
        )
        self._rebuild_free_image_backends()
        notification = UiNotification(
            "Ключ провайдера",
            f"Ключ для {self._provider_display_name(provider_id)} удален.",
            "info",
        )
        self.notifications.append(notification)
        return notification

    def get_gemini_key(self) -> str | None:
        return self.application.container.settings_manager.get_secret(
            self.application.container.settings.security.gemini_api_key_secret_name,
        )

    def delete_gemini_key(self) -> None:
        self.application.container.settings_manager.delete_secret(
            self.application.container.settings.security.gemini_api_key_secret_name,
        )

    def validate_gemini_key(self, key: str) -> UiNotification:
        trimmed = key.strip()
        if not trimmed:
            return UiNotification("Ключ Gemini", "Ключ пустой.", "error")
        if " " in trimmed:
            return UiNotification(
                "Ключ Gemini", "Ключ не должен содержать пробелы.", "error"
            )
        if len(trimmed) < 16:
            return UiNotification(
                "Ключ Gemini", "Ключ слишком короткий и выглядит некорректным.", "error"
            )
        return UiNotification(
            "Ключ Gemini",
            "Ключ выглядит корректным и будет безопасно сохранен. Проверка API произойдет при первом обращении к Gemini.",
            "success",
        )

    def get_ui_theme(self) -> str:
        return normalize_ui_theme(self.application.container.settings.ui_theme)

    def set_ui_theme(self, theme_id: str) -> str:
        settings = self.application.container.settings_manager.load()
        settings.ui_theme = normalize_ui_theme(theme_id)
        self._apply_settings_object(settings)
        return settings.ui_theme

    def _provider_api_secret_name(self, provider_id: str) -> str:
        security = self.application.container.settings.security
        mapping = {
            "pexels": security.pexels_api_key_secret_name,
            "pixabay": security.pixabay_api_key_secret_name,
        }
        if provider_id not in mapping:
            raise AppError(
                "unsupported_provider_key",
                f"Ключ для провайдера {provider_id} не поддерживается интерфейсом.",
            )
        return mapping[provider_id]

    def _provider_display_name(self, provider_id: str) -> str:
        return {
            "pexels": "Pexels",
            "pixabay": "Pixabay",
            "openverse": "Openverse",
        }.get(provider_id, provider_id)

    def _usable_free_image_provider_ids(self, provider_ids: list[str]) -> list[str]:
        available = set(
            self.application.container.media_pipeline.available_free_image_provider_ids()
        )
        return [provider_id for provider_id in provider_ids if provider_id in available]

    def _free_provider_unavailability_warnings(
        self, provider_ids: list[str]
    ) -> list[str]:
        warnings: list[str] = []
        available = set(
            self.application.container.media_pipeline.available_free_image_provider_ids()
        )
        for provider_id in provider_ids:
            if provider_id in available:
                continue
            display_name = self._provider_display_name(provider_id)
            if provider_id in {"pexels", "pixabay"}:
                warnings.append(
                    f"{display_name}: API key не задан, поэтому провайдер сейчас недоступен."
                )
            else:
                warnings.append(
                    f"{display_name}: провайдер сейчас недоступен в текущей конфигурации."
                )
        return warnings

    def _rebuild_free_image_backends(self) -> None:
        security = self.application.container.settings.security
        self.application.container.media_pipeline.build_default_free_image_backends(
            self.application.container.image_provider_search_service,
            pexels_api_key=self.application.container.settings_manager.get_secret(
                security.pexels_api_key_secret_name
            ),
            pixabay_api_key=self.application.container.settings_manager.get_secret(
                security.pixabay_api_key_secret_name
            ),
        )

    def _custom_timing_from_advanced(
        self, advanced: UiAdvancedSettingsViewModel
    ) -> LaunchProfileCustomTiming:
        return LaunchProfileCustomTiming(
            action_delay_ms=max(0, int(advanced.action_delay_ms)),
            launch_timeout_ms=max(1000, int(advanced.launch_timeout_ms)),
            navigation_timeout_ms=max(1000, int(advanced.navigation_timeout_ms)),
            downloads_timeout_seconds=max(
                1.0, float(advanced.downloads_timeout_seconds)
            ),
        )

    def _concurrency_mode_for_quick(self, quick: UiQuickLaunchSettingsViewModel):
        mode = get_project_mode(quick.mode_id)
        settings = self.application.container.settings_manager.load()
        settings.providers.enabled_providers = list(
            dict.fromkeys(self._selected_provider_ids(quick))
        )
        settings.providers.free_images_only = mode.mode_id == "free_images_only"
        return resolve_execution_concurrency_mode(
            settings.providers,
            video_enabled=mode.video_enabled,
            storyblocks_images_enabled=mode.storyblocks_images_enabled,
            free_images_enabled=mode.free_images_enabled,
        )

    def _resolve_runtime_launch_profile(
        self,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> ResolvedLaunchProfile:
        mode_resolution = self._concurrency_mode_for_quick(quick)
        return resolve_launch_profile(
            quick.launch_profile_id,
            mode_resolution.mode,
            custom_timing=self._custom_timing_from_advanced(advanced),
        )

    def _infer_launch_profile_id(self, settings: ApplicationSettings) -> str:
        quick = UiQuickLaunchSettingsViewModel(
            mode_id=normalize_project_mode(settings.providers.project_mode),
            provider_ids=normalize_free_image_provider_ids(
                [
                    provider_id
                    for provider_id in settings.providers.enabled_providers
                    if provider_id in DEFAULT_FREE_IMAGE_PROVIDER_IDS
                ]
            ),
        )
        mode_resolution = self._concurrency_mode_for_quick(quick)
        normal = resolve_launch_profile("normal", mode_resolution.mode)
        if self._settings_match_launch_profile(settings, normal):
            return "normal"
        fast = resolve_launch_profile("fast", mode_resolution.mode)
        if self._settings_match_launch_profile(settings, fast):
            return "fast"
        return "custom"

    def _settings_match_launch_profile(
        self,
        settings: ApplicationSettings,
        launch_profile: ResolvedLaunchProfile,
    ) -> bool:
        return (
            settings.browser.action_delay_ms == launch_profile.action_delay_ms
            and settings.browser.slow_mode == launch_profile.slow_mode
            and settings.browser.launch_timeout_ms == launch_profile.launch_timeout_ms
            and settings.browser.navigation_timeout_ms
            == launch_profile.navigation_timeout_ms
            and self._float_equal(
                settings.browser.downloads_timeout_seconds,
                launch_profile.downloads_timeout_seconds,
            )
            and settings.concurrency.paragraph_workers
            == launch_profile.paragraph_workers
            and settings.concurrency.provider_workers == launch_profile.provider_workers
            and settings.concurrency.provider_queue_size
            == launch_profile.provider_queue_size
            and settings.concurrency.download_workers == launch_profile.download_workers
            and settings.concurrency.download_queue_size
            == launch_profile.download_queue_size
            and settings.concurrency.queue_size == launch_profile.queue_size
            and self._float_equal(
                settings.concurrency.search_timeout_seconds,
                launch_profile.search_timeout_seconds,
            )
            and self._float_equal(
                settings.concurrency.download_timeout_seconds,
                launch_profile.downloads_timeout_seconds,
            )
            and settings.concurrency.retry_budget == launch_profile.retry_budget
            and settings.concurrency.fail_fast_storyblocks_errors
            == launch_profile.fail_fast_storyblocks_errors
            and self._float_equal(
                settings.providers.no_match_budget_seconds,
                launch_profile.no_match_budget_seconds,
            )
        )

    def _float_equal(self, left: float, right: float) -> bool:
        return abs(float(left) - float(right)) <= 1e-9

    def apply_forms_to_settings(
        self,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> ApplicationSettings:
        settings = self.application.container.settings_manager.load()
        resolved_profile = self._resolve_runtime_launch_profile(quick, advanced)
        mode = get_project_mode(quick.mode_id)
        settings.providers.project_mode = mode.mode_id
        settings.providers.enabled_providers = list(
            dict.fromkeys(self._selected_provider_ids(quick))
        )
        settings.providers.default_video_providers = [
            provider_id
            for provider_id in settings.providers.enabled_providers
            if provider_id == "storyblocks_video"
        ]
        settings.providers.default_image_providers = [
            provider_id
            for provider_id in settings.providers.enabled_providers
            if provider_id != "storyblocks_video"
        ]
        settings.providers.free_images_only = mode.mode_id == "free_images_only"
        settings.providers.supporting_image_limit = max(
            0, int(quick.supporting_image_limit)
        )
        settings.providers.fallback_image_limit = max(
            0, int(quick.fallback_image_limit)
        )
        settings.providers.no_match_budget_seconds = (
            resolved_profile.no_match_budget_seconds
        )
        settings.browser.action_delay_ms = resolved_profile.action_delay_ms
        settings.browser.slow_mode = resolved_profile.slow_mode
        settings.browser.launch_timeout_ms = resolved_profile.launch_timeout_ms
        settings.browser.navigation_timeout_ms = resolved_profile.navigation_timeout_ms
        settings.browser.downloads_timeout_seconds = (
            resolved_profile.downloads_timeout_seconds
        )
        settings.concurrency.paragraph_workers = resolved_profile.paragraph_workers
        settings.concurrency.provider_workers = resolved_profile.provider_workers
        settings.concurrency.provider_queue_size = resolved_profile.provider_queue_size
        settings.concurrency.download_workers = resolved_profile.download_workers
        settings.concurrency.download_queue_size = resolved_profile.download_queue_size
        settings.concurrency.search_timeout_seconds = (
            resolved_profile.search_timeout_seconds
        )
        settings.concurrency.download_timeout_seconds = (
            resolved_profile.downloads_timeout_seconds
        )
        settings.concurrency.retry_budget = resolved_profile.retry_budget
        settings.concurrency.fail_fast_storyblocks_errors = (
            resolved_profile.fail_fast_storyblocks_errors
        )
        settings.concurrency.queue_size = resolved_profile.queue_size
        self.application.container.orchestrator.configure(
            max_workers=settings.concurrency.paragraph_workers,
            queue_size=settings.concurrency.queue_size,
        )
        self._apply_settings_object(settings)
        return settings

    def _apply_settings_object(self, settings: ApplicationSettings) -> None:
        current = self.application.container.settings
        _copy_dataclass_values(current, settings)
        self.application.container.settings_manager.save(current)

    def _build_settings_snapshot(
        self,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> dict[str, object]:
        launch_profile_id = normalize_launch_profile_id(quick.launch_profile_id)
        snapshot: dict[str, object] = {
            "quick_launch": {
                "mode_id": normalize_project_mode(quick.mode_id),
                "strictness": quick.strictness,
                "provider_ids": normalize_free_image_provider_ids(quick.provider_ids),
                "supporting_image_limit": max(0, int(quick.supporting_image_limit)),
                "fallback_image_limit": max(0, int(quick.fallback_image_limit)),
                "manual_prompt": quick.manual_prompt.strip(),
                "attach_full_script_context": bool(quick.attach_full_script_context),
            },
            "launch_profile_id": launch_profile_id,
        }
        if launch_profile_id == "custom":
            custom_timing = self._custom_timing_from_advanced(advanced)
            snapshot["custom_timing_overrides"] = {
                "action_delay_ms": custom_timing.action_delay_ms,
                "launch_timeout_ms": custom_timing.launch_timeout_ms,
                "navigation_timeout_ms": custom_timing.navigation_timeout_ms,
                "downloads_timeout_seconds": custom_timing.downloads_timeout_seconds,
            }
        return snapshot

    def _media_config_from_forms(
        self,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> MediaSelectionConfig:
        mode = get_project_mode(quick.mode_id)
        resolved_profile = self._resolve_runtime_launch_profile(quick, advanced)
        return MediaSelectionConfig(
            video_enabled=mode.video_enabled,
            storyblocks_images_enabled=mode.storyblocks_images_enabled,
            free_images_enabled=mode.free_images_enabled,
            supporting_image_limit=max(0, int(quick.supporting_image_limit)),
            fallback_image_limit=max(0, int(quick.fallback_image_limit)),
            max_candidates_per_provider=max(1, resolved_profile.provider_workers * 2),
            provider_workers=max(1, resolved_profile.provider_workers),
            provider_queue_size=max(1, resolved_profile.provider_queue_size),
            bounded_downloads=max(1, resolved_profile.download_queue_size),
            download_workers=max(1, resolved_profile.download_workers),
            early_stop_when_satisfied=True,
            no_match_budget_seconds=max(
                0.0, float(resolved_profile.no_match_budget_seconds)
            ),
            search_timeout_seconds=max(
                0.0, float(resolved_profile.search_timeout_seconds)
            ),
            download_timeout_seconds=max(
                1.0, float(resolved_profile.downloads_timeout_seconds)
            ),
            retry_budget=max(0, int(resolved_profile.retry_budget)),
            fail_fast_storyblocks_errors=bool(
                resolved_profile.fail_fast_storyblocks_errors
            ),
            output_root=quick.output_dir.strip(),
        )

    def _selected_provider_ids(
        self,
        quick: UiQuickLaunchSettingsViewModel,
    ) -> list[str]:
        return provider_ids_for_mode(
            quick.mode_id,
            free_image_provider_ids=quick.provider_ids,
        )

    def build_run_progress(
        self,
        run_id: str,
        *,
        active_project_id: str | None = None,
        run: Run | None = None,
        manifest: RunManifest | None = None,
        latest_event: AppEvent | None = None,
        project: Project | None = None,
        load_manifest: bool = True,
        load_project: bool = True,
    ) -> UiRunProgressViewModel | None:
        run = run if run is not None else self._safe_load_run(run_id)
        if run is None:
            return None
        manifest = (
            manifest
            if manifest is not None
            else (self._safe_load_manifest(run_id) if load_manifest else None)
        )
        latest_event = (
            latest_event
            if latest_event is not None
            else self.application.container.event_recorder.latest_for_run(run_id)
        )
        if project is None and load_project and (active_project_id or run.project_id):
            try:
                project = self._require_project(active_project_id or run.project_id)
            except KeyError:
                project = None
        total = 0
        processed = len(run.completed_paragraphs) + len(run.failed_paragraphs)
        failed = len(run.failed_paragraphs)
        completed = max(0, len(run.completed_paragraphs))
        no_match = 0
        downloads_root = ""
        videos_dir = ""
        images_dir = ""
        downloaded_video_files = 0
        downloaded_image_files = 0
        if manifest is not None:
            summary = manifest.summary
            total = int(
                summary.get("paragraphs_total", len(manifest.paragraph_entries))
            )
            processed = max(
                processed,
                int(summary.get("paragraphs_processed", processed)),
            )
            completed = int(summary.get("paragraphs_completed", completed))
            failed = max(failed, int(summary.get("paragraphs_failed", failed)))
            no_match = int(summary.get("paragraphs_no_match", 0))
            downloads_root = str(summary.get("downloads_root", ""))
            videos_dir = str(summary.get("videos_dir", ""))
            images_dir = str(summary.get("images_dir", ""))
            downloaded_video_files = int(summary.get("downloaded_video_files", 0))
            downloaded_image_files = int(summary.get("downloaded_image_files", 0))
        else:
            stored_total = int(run.metadata.get("paragraphs_total", 0) or 0)
            total = max(total, stored_total)
            if total <= 0 and run.selected_paragraphs:
                total = len(set(run.selected_paragraphs))
            if (
                total <= 0
                and project is not None
                and project.script_document is not None
            ):
                paragraphs = project.script_document.paragraphs
                selected = (
                    set(run.selected_paragraphs) if run.selected_paragraphs else None
                )
                total = sum(
                    1
                    for paragraph in paragraphs
                    if selected is None or paragraph.paragraph_no in selected
                )
        stage = (
            latest_event.stage
            if latest_event is not None and latest_event.stage is not None
            else run.stage
        )
        paragraph_stage_value = {
            RunStage.IDLE: 0,
            RunStage.INGESTION: 1,
            RunStage.INTENT: 2,
            RunStage.PROVIDER_SEARCH: 3,
            RunStage.DOWNLOAD: 4,
            RunStage.RELEVANCE: 5,
            RunStage.PERSIST: 6,
            RunStage.COMPLETE: 7,
        }.get(stage, 0)
        percent_complete = (
            0.0 if total <= 0 else round((processed / max(1, total)) * 100.0, 1)
        )
        eta_text = self._eta_text(run, processed, total)
        effective_status = self._effective_run_status(run, latest_event)
        return UiRunProgressViewModel(
            run_id=run.run_id,
            status=effective_status.value,
            eta_text=eta_text,
            current_stage=stage.value,
            current_paragraph_no=latest_event.paragraph_no
            if latest_event is not None
            else None,
            current_provider_name=latest_event.provider_name or ""
            if latest_event is not None
            else "",
            current_query=latest_event.query or "" if latest_event is not None else "",
            current_asset_id=str(latest_event.payload.get("current_asset_id", ""))
            if latest_event is not None
            else "",
            paragraphs_total=total,
            paragraphs_processed=processed,
            paragraphs_completed=completed,
            project_progress_total=total,
            project_progress_completed=processed,
            paragraphs_no_match=no_match,
            paragraph_progress_total=7,
            paragraph_progress_completed=paragraph_stage_value,
            paragraphs_failed=failed,
            downloads_root=downloads_root,
            videos_dir=videos_dir,
            images_dir=images_dir,
            downloaded_video_files=downloaded_video_files,
            downloaded_image_files=downloaded_image_files,
            percent_complete=percent_complete,
            live_state=self._live_state(run, latest_event, status=effective_status),
            can_cancel=effective_status == RunStatus.RUNNING,
        )

    def format_run_log(self, run_id: str) -> str:
        run = self._require_run(run_id)
        manifest = self._safe_load_manifest(run_id)
        progress = self.build_run_progress(run_id, run=run, manifest=manifest)
        events = self.application.container.event_recorder.by_run(run_id)
        lines = [
            f"Run ID: {run.run_id}",
            f"Project ID: {run.project_id}",
            f"Status: {run.status.value}",
            f"Stage: {run.stage.value}",
            f"Started: {_iso(run.started_at)}",
            f"Finished: {_iso(run.finished_at)}",
        ]
        if progress is not None:
            lines.extend(
                [
                    f"Processed: {progress.project_progress_completed}/{progress.project_progress_total}",
                    f"Completed: {progress.paragraphs_completed}",
                    f"No match: {progress.paragraphs_no_match}",
                    f"Failed: {progress.paragraphs_failed}",
                    f"Downloads root: {progress.downloads_root or '-'}",
                    f"ETA: {progress.eta_text or '-'}",
                ]
            )
        if run.last_error:
            lines.append(f"Last error: {run.last_error}")
        if manifest is not None:
            lines.append("")
            lines.append("Manifest summary:")
            for key, value in sorted(manifest.summary.items()):
                lines.append(f"- {key}: {value}")
        lines.append("")
        lines.append("Events:")
        for event in events:
            context = []
            if event.paragraph_no is not None:
                context.append(f"P{event.paragraph_no}")
            if event.provider_name:
                context.append(event.provider_name)
            if event.query:
                context.append(event.query)
            current_asset_id = str(event.payload.get("current_asset_id", "")).strip()
            if current_asset_id:
                context.append(current_asset_id)
            suffix = f" [{' | '.join(context)}]" if context else ""
            lines.append(
                " | ".join(
                    [
                        _iso(event.created_at),
                        event.level.value,
                        event.stage.value if event.stage is not None else "",
                        f"{event.message}{suffix}",
                    ]
                )
            )
        return "\n".join(lines)

    def export_run_log(self, run_id: str, destination_path: str) -> Path:
        path = Path(destination_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.format_run_log(run_id), encoding="utf-8")
        return path

    def _status_text(self, active_run_id: str | None) -> str:
        if active_run_id is not None:
            progress = self.build_run_progress(active_run_id)
            if progress is not None:
                current = (
                    f" абзац {progress.current_paragraph_no}"
                    if progress.current_paragraph_no is not None
                    else ""
                )
                return f"{progress.status}: {progress.live_state}{current}"
        return "готово"

    def _eta_text(self, run: Run, processed: int, total: int) -> str:
        if run.started_at is None or processed <= 0 or total <= processed:
            return ""
        elapsed = max(0.0, (utc_now() - run.started_at).total_seconds())
        if elapsed < 5.0:
            return ""
        average_per_paragraph = elapsed / max(1, processed)
        remaining_seconds = int(round(average_per_paragraph * (total - processed)))
        if remaining_seconds <= 0:
            return ""
        minutes, seconds = divmod(remaining_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours > 0:
            return f"Осталось примерно {hours} ч {minutes} мин"
        if minutes > 0:
            return f"Осталось примерно {minutes} мин {seconds} сек"
        return f"Осталось примерно {seconds} сек"

    def _session_indicator_tone(self, health: SessionHealth) -> str:
        if health == SessionHealth.READY:
            return "healthy"
        if health in {
            SessionHealth.LOGIN_REQUIRED,
            SessionHealth.EXPIRED,
            SessionHealth.CHALLENGE,
            SessionHealth.BLOCKED,
        }:
            return "warning"
        return "neutral"

    def _latest_events_by_paragraph(self, run_id: str | None) -> dict[int, AppEvent]:
        return self.application.container.event_recorder.latest_by_paragraph_for_run(
            run_id
        )

    def _effective_run_status(
        self,
        run: Run,
        latest_event: AppEvent | None,
    ) -> RunStatus:
        if latest_event is not None:
            status_by_event = {
                "run.started": RunStatus.RUNNING,
                "run.cancelled": RunStatus.CANCELLED,
                "run.completed": RunStatus.COMPLETED,
                "run.failed": RunStatus.FAILED,
            }
            resolved = status_by_event.get(latest_event.name)
            if resolved is not None:
                return resolved
        return run.status

    def _live_state(
        self,
        run: Run,
        latest_event: AppEvent | None,
        *,
        status: RunStatus | None = None,
    ) -> str:
        effective_status = status or run.status
        if latest_event is not None:
            if latest_event.name == "run.cancel_requested":
                return "запрошена отмена"
            if latest_event.name == "paragraph.processing.started":
                return "обработка абзаца"
            if latest_event.name == "provider.search.started":
                return "поиск у провайдера"
            if latest_event.name == "asset.download.started":
                return "скачивание ассета"
            if latest_event.name == "asset.download.completed":
                return "скачивание завершено"
            if latest_event.name == "asset.download.failed":
                return "ошибка скачивания"
            if latest_event.name == "paragraph.relevance.started":
                return "оценка релевантности"
            if latest_event.name == "paragraph.persisted":
                return "сохранение результата"
            if latest_event.name == "provider.search.empty":
                return "нет результатов у провайдера"
            if latest_event.name == "paragraph.search_budget.exhausted":
                return "истек лимит поиска"
        if effective_status == RunStatus.COMPLETED:
            return "завершено"
        if effective_status == RunStatus.FAILED:
            return "с ошибкой"
        if effective_status == RunStatus.CANCELLED:
            return "остановлено"
        if effective_status == RunStatus.RUNNING:
            return "выполняется"
        return "ожидание"

    def _push_notification(self, notification: UiNotification) -> None:
        self.notifications.append(notification)
        if len(self.notifications) > 30:
            self.notifications = self.notifications[-30:]

    def _start_background_run(
        self,
        run_id: str,
        project_id: str,
        work: Callable[[], tuple[Run, RunManifest]],
    ) -> None:
        with self._background_lock:
            if (
                self._background_run is not None
                and self._background_run.thread.is_alive()
            ):
                raise AppError(
                    "run_busy",
                    "Уже выполняется другой запуск.",
                )

            task = _BackgroundRunTask(
                run_id=run_id,
                project_id=project_id,
                thread=threading.Thread(
                    target=lambda: self._run_background_task(work),
                    name=f"ui-run-{run_id}",
                    daemon=True,
                ),
            )
            self._background_run = task
            task.thread.start()

    def _run_background_task(self, work: Callable[[], tuple[Run, RunManifest]]) -> None:
        error: Exception | None = None
        try:
            work()
        except Exception as exc:  # pragma: no cover - surfaced through polling state
            error = exc
        with self._background_lock:
            if self._background_run is not None:
                self._background_run.error = error

    def _finalize_background_run_if_needed(self) -> None:
        with self._background_lock:
            task = self._background_run
            if task is None or task.thread.is_alive() or task.notification_sent:
                return
            task.notification_sent = True
            error = task.error
            self._background_run = None
        if error is not None:
            self._push_notification(handle_ui_error(error))
            return
        run = self.application.container.run_repository.load(task.run_id)
        if run is None:
            return
        severity = "success"
        if run.status in {RunStatus.FAILED, RunStatus.CANCELLED}:
            severity = "warning"
        self._push_notification(
            UiNotification(
                "Run update",
                f"Run {run.run_id} finished with status {run.status.value}",
                severity,
            )
        )

    def _project_summary(self, project: Project) -> UiProjectSummary:
        document = project.script_document
        return UiProjectSummary(
            project_id=project.project_id,
            name=project.name,
            source_path=str(document.source_path) if document is not None else "",
            paragraphs_total=len(document.paragraphs) if document is not None else 0,
            header_text=document.header_text if document is not None else "",
            numbering_issues=list(document.numbering_issues)
            if document is not None
            else [],
            active_run_id=project.active_run_id,
            updated_at=_iso(project.updated_at),
        )

    def _validate_run_request(
        self,
        project_id: str,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ) -> UiQuickLaunchSettingsViewModel:
        project = self._require_project(project_id)
        document = project.script_document
        if document is None or not document.paragraphs:
            raise AppError(
                "script_missing", "Load a numbered DOCX script before starting a run"
            )
        resolved_quick = self._resolve_quick_launch_settings(project, quick)
        if document.numbering_issues:
            raise AppError(
                "invalid_numbering",
                "Исправьте нумерацию сценария перед запуском.",
                {"issues": list(document.numbering_issues)},
            )
        if (
            not resolved_quick.selected_paragraphs
            and resolved_quick.paragraph_selection_text
        ):
            raise AppError(
                "invalid_paragraph_selection",
                "Выберите корректный диапазон абзацев перед запуском.",
            )
        mode = get_project_mode(resolved_quick.mode_id)
        if (
            mode.requires_storyblocks
            and self.session_panel().health != SessionHealth.READY.value
            and not self.session_panel().manual_ready_override
        ):
            raise AppError(
                "storyblocks_session_not_ready",
                "Для выбранного режима нужна готовая сессия Storyblocks.",
                {"mode": mode.mode_id, "session_health": self.session_panel().health},
            )
        selected_providers = self._selected_provider_ids(resolved_quick)
        if not selected_providers:
            raise AppError(
                "provider_selection_empty",
                "Для выбранного режима не включены нужные провайдеры. Включите их перед запуском.",
                {"mode": mode.mode_id},
            )
        mode_resolution = self._resolve_concurrency_mode_for_request(
            resolved_quick, advanced
        )
        resolved_profile = self._resolve_runtime_launch_profile(
            resolved_quick, advanced
        )
        selected_providers = list(mode_resolution.selected_provider_ids)
        if (
            resolved_profile.paragraph_workers > 1
            and mode_resolution.requires_serial_paragraph_workers
        ):
            raise AppError(
                "storyblocks_parallelism_guard",
                "Для режимов Storyblocks нужен последовательный запуск абзацев. Выберите безопасный профиль запуска или режим без Storyblocks.",
                {
                    "mode": mode.mode_id,
                    "paragraph_workers": resolved_profile.paragraph_workers,
                    "recommended_paragraph_workers": 1,
                    "concurrency_mode": mode_resolution.mode.value,
                    "selected_provider_ids": selected_providers,
                },
            )
        free_provider_ids = [
            provider_id
            for provider_id in selected_providers
            if provider_id in DEFAULT_FREE_IMAGE_PROVIDER_IDS
        ]
        if mode.free_images_enabled and not free_provider_ids:
            raise AppError(
                "free_image_provider_missing",
                "Выберите хотя бы один источник бесплатных изображений для этого режима.",
                {"mode": mode.mode_id},
            )
        usable_free_provider_ids = self._usable_free_image_provider_ids(
            free_provider_ids
        )
        if (
            mode.free_images_enabled
            and free_provider_ids
            and not usable_free_provider_ids
        ):
            raise AppError(
                "free_image_provider_unavailable",
                "Для выбранных бесплатных изображений нет ни одного доступного провайдера. Добавьте API key для Pexels/Pixabay или оставьте включенным Openverse.",
                {"mode": mode.mode_id, "providers": list(free_provider_ids)},
            )
        return resolved_quick

    def _resolve_quick_launch_settings(
        self, project: Project, quick: UiQuickLaunchSettingsViewModel
    ) -> UiQuickLaunchSettingsViewModel:
        document = project.script_document
        resolved_paragraphs = self._resolve_selected_paragraphs(
            document,
            quick.selected_paragraphs,
            quick.paragraph_selection_text,
        )
        normalized_text = quick.paragraph_selection_text.strip()
        if not normalized_text and resolved_paragraphs:
            normalized_text = ", ".join(str(item) for item in resolved_paragraphs)
        return replace(
            quick,
            selected_paragraphs=resolved_paragraphs,
            paragraph_selection_text=normalized_text,
            mode_id=normalize_project_mode(quick.mode_id),
            launch_profile_id=normalize_launch_profile_id(quick.launch_profile_id),
            provider_ids=normalize_free_image_provider_ids(quick.provider_ids),
            supporting_image_limit=max(0, int(quick.supporting_image_limit)),
            fallback_image_limit=max(0, int(quick.fallback_image_limit)),
            manual_prompt=quick.manual_prompt.strip(),
        )

    def _resolve_selected_paragraphs(
        self,
        document,
        selected_paragraphs: list[int],
        paragraph_selection_text: str,
    ) -> list[int]:
        if document is None:
            return list(selected_paragraphs)
        available = [paragraph.paragraph_no for paragraph in document.paragraphs]
        if selected_paragraphs:
            return self._normalize_selected_paragraph_numbers(
                available, selected_paragraphs
            )
        text = paragraph_selection_text.strip()
        if not text:
            return []
        resolved: list[int] = []
        for token in re.split(r"[,;]+", text):
            chunk = token.strip()
            if not chunk:
                continue
            match = re.fullmatch(
                r"(\d+)\s*(?:\.\.|-|:)\s*(\d+|end)?", chunk, re.IGNORECASE
            )
            if match is not None:
                start = int(match.group(1))
                raw_end = (match.group(2) or "end").casefold()
                end = available[-1] if raw_end == "end" else int(raw_end)
                if end < start:
                    raise AppError(
                        "invalid_paragraph_selection",
                        f"Диапазон '{chunk}' задан в обратном порядке.",
                    )
                resolved.extend(
                    paragraph_no
                    for paragraph_no in available
                    if start <= paragraph_no <= end
                )
                continue
            if chunk.isdigit():
                resolved.append(int(chunk))
                continue
            raise AppError(
                "invalid_paragraph_selection",
                f"Не удалось разобрать диапазон абзацев: '{chunk}'.",
            )
        return self._normalize_selected_paragraph_numbers(available, resolved)

    def _normalize_selected_paragraph_numbers(
        self, available: list[int], values: list[int]
    ) -> list[int]:
        available_set = set(available)
        requested = [int(item) for item in values]
        missing = [item for item in requested if item not in available_set]
        if missing:
            raise AppError(
                "invalid_paragraph_selection",
                "Указаны абзацы, которых нет в текущем сценарии.",
                {"missing": sorted(set(missing))},
            )
        selected_set = set(requested)
        return [
            paragraph_no for paragraph_no in available if paragraph_no in selected_set
        ]

    def _uses_storyblocks_provider(self, provider_ids: list[str]) -> bool:
        return any(
            provider_id in {"storyblocks_video", "storyblocks_image"}
            for provider_id in provider_ids
        )

    def _resolve_concurrency_mode_for_request(
        self,
        quick: UiQuickLaunchSettingsViewModel,
        advanced: UiAdvancedSettingsViewModel,
    ):
        return self._concurrency_mode_for_quick(quick)

    def _downloaded_files(
        self, selection: AssetSelection | None
    ) -> list[UiDownloadedFileItem]:
        if selection is None:
            return []
        items: list[UiDownloadedFileItem] = []
        for role, asset in self._selection_downloaded_assets(selection):
            items.append(self._downloaded_file_item(asset, role=role))
        return items

    def _live_downloaded_files(
        self, snapshots: list[LiveDownloadedFileSnapshot]
    ) -> list[UiDownloadedFileItem]:
        return [
            UiDownloadedFileItem(
                asset_id=snapshot.asset_id,
                provider_name=snapshot.provider_name,
                kind=snapshot.kind.value,
                role=snapshot.role,
                title=snapshot.title or snapshot.asset_id,
                local_path=str(snapshot.local_path or ""),
                exists=snapshot.exists,
            )
            for snapshot in snapshots
        ]

    def _selection_downloaded_assets(
        self, selection: AssetSelection
    ) -> list[tuple[str, AssetCandidate]]:
        assets: list[tuple[str, AssetCandidate]] = []
        for role, asset in self._selection_assets_with_roles(selection):
            if asset.local_path is None:
                continue
            assets.append((role, asset))
        return assets

    def _selection_assets_with_roles(
        self, selection: AssetSelection
    ) -> list[tuple[str, AssetCandidate]]:
        assets: list[tuple[str, AssetCandidate]] = []
        for role, asset in (
            ("primary", selection.primary_asset),
            *[("supporting", asset) for asset in selection.supporting_assets],
            *[("fallback", asset) for asset in selection.fallback_assets],
        ):
            if asset is None:
                continue
            assets.append((role, asset))
        return assets

    def _has_downloaded_files(self, selection: AssetSelection | None) -> bool:
        if selection is None:
            return False
        return any(
            asset.local_path is not None
            for _role, asset in self._selection_assets_with_roles(selection)
        )

    def _downloaded_file_item(
        self, asset: AssetCandidate, *, role: str
    ) -> UiDownloadedFileItem:
        local_path = str(asset.local_path or "")
        return UiDownloadedFileItem(
            asset_id=asset.asset_id,
            provider_name=asset.provider_name,
            kind=asset.kind.value,
            role=role,
            title=str(asset.metadata.get("title", asset.asset_id)),
            local_path=local_path,
            exists=self._path_exists(asset.local_path),
        )

    def _path_exists(self, path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.exists()
        except OSError:
            return False

    def _normalize_paragraph_status(
        self,
        status: str | None,
        *,
        has_downloaded_files: bool,
    ) -> str:
        value = (status or "pending").strip().casefold()
        mapping = {
            "selected": "completed",
            "locked": "completed",
            "partial_success": "completed",
            "pending": "pending",
            "processing": "processing",
            "completed": "completed",
            "no_match": "no_match",
            "failed": "failed",
            "skipped": "skipped",
        }
        if value == "needs_review":
            return "completed" if has_downloaded_files else "failed"
        return mapping.get(value, "pending")

    def _entry_result_note(self, entry) -> str:
        if entry.selection is not None and entry.selection.reason.strip():
            return entry.selection.reason.strip()
        for reason in entry.rejection_reasons:
            if str(reason).strip():
                return str(reason).strip()
        return ""

    def _intent_summary(self, intent: ParagraphIntent | None) -> str:
        if intent is None:
            return "Intent not prepared"
        parts = [intent.subject, intent.action, intent.setting]
        summary = " / ".join(part for part in parts if part)
        return summary or "Intent prepared"

    def _require_project(self, project_id: str) -> Project:
        project = self._project_cache.get(project_id)
        if project is None:
            project = self.application.container.project_repository.load(project_id)
        if project is None:
            raise KeyError(project_id)
        return self._remember_project(project)

    def _require_run(self, run_id: str) -> Run:
        run = self.application.container.run_repository.load(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _safe_load_run(self, run_id: str) -> Run | None:
        try:
            return self.application.container.run_repository.load(run_id)
        except (JSONDecodeError, ValueError):
            return None

    def _safe_load_manifest(self, run_id: str) -> RunManifest | None:
        try:
            return self.application.container.media_run_service.load_manifest(run_id)
        except (JSONDecodeError, ValueError):
            return None

    def _safe_snapshot_manifest(self, run_id: str) -> RunManifest | None:
        try:
            return self.application.container.media_run_service.snapshot_manifest(
                run_id
            )
        except (JSONDecodeError, ValueError):
            return None

    def _safe_snapshot_live_run_state(
        self,
        run_id: str,
        *,
        detailed_paragraph_no: int | None = None,
    ) -> LiveRunStateSnapshot | None:
        try:
            return self.application.container.media_run_service.snapshot_live_run_state(
                run_id,
                detailed_paragraph_no=detailed_paragraph_no,
            )
        except (JSONDecodeError, ValueError):
            return None

    def _remember_project(self, project: Project) -> Project:
        self._project_cache[project.project_id] = project
        return project

    def _remember_active_run(self, project_id: str, run_id: str) -> None:
        try:
            project = self._require_project(project_id)
        except KeyError:
            return
        project.active_run_id = run_id
        self._remember_project(project)

    def _require_paragraph(self, project: Project, paragraph_no: int):
        document = project.script_document
        if document is None:
            raise KeyError(paragraph_no)
        for paragraph in document.paragraphs:
            if paragraph.paragraph_no == paragraph_no:
                return paragraph
        raise KeyError(paragraph_no)


def handle_ui_error(exc: Exception) -> UiNotification:
    if isinstance(exc, AppError):
        return UiNotification(
            exc.code, translate_error_text(exc.message), "error", dict(exc.details)
        )
    return UiNotification("Неожиданная ошибка", translate_error_text(str(exc)), "error")


def _copy_dataclass_values(target, source) -> None:
    for item in fields(target):
        source_value = getattr(source, item.name)
        target_value = getattr(target, item.name)
        if is_dataclass(target_value) and is_dataclass(source_value):
            _copy_dataclass_values(target_value, source_value)
        else:
            setattr(target, item.name, source_value)
