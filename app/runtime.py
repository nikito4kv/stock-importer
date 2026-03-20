from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from domain.enums import EventLevel
from domain.models import (
    AssetSelection,
    ParagraphIntent,
    Project,
    QueryBundle,
    Run,
    RunManifest,
    ScriptDocument,
    utc_now,
)
from pipeline import MediaSelectionConfig
from services.events import AppEvent
from services.genai_client import create_gemini_model

from .bootstrap import ApplicationContainer, bootstrap_application

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


def _elapsed_ms(started_at: float, *, minimum: int = 0) -> int:
    elapsed = int(round((perf_counter() - started_at) * 1000.0))
    return max(int(minimum), elapsed)


@dataclass(slots=True)
class ApplicationSnapshot:
    workspace_root: Path
    providers: list[str]
    browser_profiles: list[str]


class DesktopApplication:
    def __init__(self, container: ApplicationContainer):
        self.container = container

    @classmethod
    def create(cls, workspace_root: str | Path | None = None) -> "DesktopApplication":
        return cls(bootstrap_application(workspace_root))

    def start(self) -> ApplicationSnapshot:
        profiles = [
            profile.profile_id
            for profile in self.container.profile_registry.list_profiles()
        ]
        providers = [
            provider.provider_id
            for provider in self.container.provider_registry.list_all()
        ]
        return ApplicationSnapshot(
            workspace_root=self.container.workspace.paths.root,
            providers=providers,
            browser_profiles=profiles,
        )

    def close(self) -> None:
        self.container.close()

    def create_project(self, name: str, script_path: str | Path) -> Project:
        import_started_at = perf_counter()
        ingestion_ms = 0
        bootstrap_ms = 0
        save_ms = 0
        document: ScriptDocument | None = None
        project_id = uuid4().hex[:12]
        import_perf_id = uuid4().hex[:12]
        failed_stage = "ingestion"
        try:
            ingestion_started_at = perf_counter()
            document = self.container.ingestion_service.ingest(script_path)
            ingestion_ms = _elapsed_ms(ingestion_started_at, minimum=1)

            failed_stage = "intent_bootstrap"
            bootstrap_started_at = perf_counter()
            document = self._bootstrap_document_intents(document)
            bootstrap_ms = _elapsed_ms(bootstrap_started_at, minimum=1)

            failed_stage = "project_save"
            save_started_at = perf_counter()
            project = Project(
                project_id=project_id,
                name=name,
                workspace_path=self.container.workspace.paths.projects_dir,
                script_document=document,
            )
            saved = self.container.project_repository.save(project)
            save_ms = _elapsed_ms(save_started_at, minimum=1)
            total_ms = _elapsed_ms(import_started_at, minimum=1)
            self._emit_application_event(
                "project.import.completed",
                EventLevel.INFO,
                f"Project {saved.project_id} imported",
                project_id=saved.project_id,
                payload={
                    "script_path": str(script_path),
                    "paragraphs_total": len(document.paragraphs),
                    "ingestion_ms": ingestion_ms,
                    "intent_bootstrap_ms": bootstrap_ms,
                    "project_save_ms": save_ms,
                    "time_to_import_project_ms": total_ms,
                    "perf_context_id": import_perf_id,
                    "perf_run_id": f"import-{saved.project_id}",
                },
            )
            return saved
        except Exception as exc:
            total_ms = _elapsed_ms(import_started_at, minimum=1)
            self._emit_application_event(
                "project.import.failed",
                EventLevel.ERROR,
                f"Project import failed at {failed_stage}: {exc}",
                payload={
                    "script_path": str(script_path),
                    "paragraphs_total": len(document.paragraphs) if document else 0,
                    "ingestion_ms": ingestion_ms,
                    "intent_bootstrap_ms": bootstrap_ms,
                    "project_save_ms": save_ms,
                    "time_to_import_project_ms": total_ms,
                    "failed_stage": failed_stage,
                    "error": str(exc),
                    "perf_context_id": import_perf_id,
                    "perf_run_id": f"import-{project_id}",
                },
            )
            raise

    def _bootstrap_document_intents(self, document: ScriptDocument) -> ScriptDocument:
        include_generic_web_image = (
            self.container.settings.providers.allow_generic_web_image
        )
        return self.container.intent_service.bootstrap_document(
            document,
            strictness="balanced",
            include_generic_web_image=include_generic_web_image,
        )

    def enrich_project_intents(
        self,
        project_id: str,
        *,
        strictness: str = "balanced",
        include_generic_web_image: bool | None = None,
        manual_prompt: str = "",
        attach_full_script_context: bool = False,
    ) -> Project:
        project = self.container.project_repository.load(project_id)
        if project is None or project.script_document is None:
            raise KeyError(project_id)

        gemini_key = self._gemini_api_key()
        if not gemini_key:
            raise RuntimeError("Gemini API key is not configured")

        include_generic = (
            self.container.settings.providers.allow_generic_web_image
            if include_generic_web_image is None
            else include_generic_web_image
        )
        full_script_context = ""
        if (
            attach_full_script_context
            and self.container.settings.ai.full_script_context_enabled
        ):
            full_script_context = self.container.intent_service.build_document_context(
                project.script_document,
                char_budget=self.container.settings.ai.full_script_context_char_budget,
            )

        started_at = perf_counter()
        model = create_gemini_model(
            api_key=gemini_key,
            model_name=DEFAULT_GEMINI_MODEL,
        )
        _intents_by_paragraph, _items, document = (
            self.container.intent_service.extract_document(
                model,
                project.script_document,
                strictness=strictness,
                max_workers=1,
                include_generic_web_image=include_generic,
                manual_prompt=manual_prompt,
                full_script_context=full_script_context,
            )
        )
        project.script_document = document
        project.updated_at = utc_now()
        saved = self.container.project_repository.save(project)
        elapsed_ms = int(round((perf_counter() - started_at) * 1000.0))
        self._emit_application_event(
            "project.intent_enrichment.completed",
            EventLevel.INFO,
            f"Project {saved.project_id} Gemini enrichment completed",
            project_id=saved.project_id,
            payload={
                "strictness": strictness,
                "paragraphs_total": len(document.paragraphs),
                "attach_full_script_context": attach_full_script_context,
                "manual_prompt": bool(manual_prompt.strip()),
                "intent_enrichment_ms": elapsed_ms,
                **self.container.intent_service.last_extract_metrics(),
            },
        )
        return saved

    def _gemini_api_key(self) -> str:
        return (
            self.container.settings_manager.get_secret(
                self.container.settings.security.gemini_api_key_secret_name
            )
            or ""
        ).strip()

    def _emit_application_event(
        self,
        name: str,
        level: EventLevel,
        message: str,
        *,
        project_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        self.container.event_bus.publish(
            AppEvent(
                name=name,
                level=level,
                message=message,
                project_id=project_id,
                payload=dict(payload or {}),
            )
        )

    def update_paragraph_intent(
        self,
        project_id: str,
        paragraph_no: int,
        *,
        intent: ParagraphIntent,
        query_bundle: QueryBundle | None = None,
        strictness: str = "balanced",
        include_generic_web_image: bool = False,
    ) -> Project:
        project = self.container.project_repository.load(project_id)
        if project is None or project.script_document is None:
            raise KeyError(project_id)

        for paragraph in project.script_document.paragraphs:
            if paragraph.paragraph_no != paragraph_no:
                continue
            self.container.intent_service.apply_manual_edit(
                paragraph,
                intent=intent,
                query_bundle=query_bundle,
                strictness=strictness,
                include_generic_web_image=include_generic_web_image,
            )
            project.updated_at = utc_now()
            return self.container.project_repository.save(project)

        raise KeyError(paragraph_no)

    def create_media_run(
        self,
        project_id: str,
        *,
        selected_paragraphs: list[int] | None = None,
        config: MediaSelectionConfig | None = None,
    ) -> Run:
        run, _ = self.container.media_run_service.create_run(
            project_id,
            selected_paragraphs=selected_paragraphs,
            config=config,
        )
        return run

    def execute_media_run(
        self,
        project_id: str,
        *,
        selected_paragraphs: list[int] | None = None,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        return self.container.media_run_service.create_and_execute(
            project_id,
            selected_paragraphs=selected_paragraphs,
            config=config,
        )

    def resume_media_run(
        self,
        run_id: str,
        *,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        return self.container.media_run_service.resume(run_id, config=config)

    def retry_failed_media_run(
        self,
        run_id: str,
        *,
        config: MediaSelectionConfig | None = None,
    ) -> tuple[Run, RunManifest]:
        return self.container.media_run_service.retry_failed_only(run_id, config=config)

    def lock_paragraph_selection(
        self,
        run_id: str,
        paragraph_no: int,
        selection: AssetSelection,
    ) -> RunManifest:
        return self.container.media_run_service.lock_selection(
            run_id, paragraph_no, selection
        )
