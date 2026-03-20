# Анализ проекта `vid-img-downloader`

## Область анализа
- Проанализированы основные runtime-модули: `app`, `browser`, `config`, `domain`, `legacy_core`, `pipeline`, `providers`, `release_tools`, `services`, `storage`, `ui`, а также top-level скрипты `keyword_extractor.py`, `image_fetcher.py`, `video_fetcher.py`.
- Исключены артефакты и окружение: `.venv/`, `dist/`, `workspace/`, `output/`, `recordings/`, `tmp_*`.
- Отдельно просмотрен состав тестов в `tests/`, чтобы зафиксировать покрытые подсистемы.

## Что делает проект
Проект собирает медиа для сценария по абзацам. Основной сценарий работы такой:

1. DOCX-сценарий импортируется и валидируется по нумерации.
2. Для каждого абзаца строится intent и набор поисковых запросов.
3. Для запуска создается `Run` и `RunManifest`.
4. Pipeline ищет кандидатов у Storyblocks и/или у бесплатных image providers.
5. Кандидаты дедуплицируются, ранжируются, скачиваются и сохраняются в manifest.
6. UI показывает live progress, event journal, состояние Storyblocks-сессии и позволяет вручную закреплять/отклонять ассеты.

Есть два слоя:

- новая desktop-архитектура: `app` + `ui` + `pipeline` + `browser` + `providers` + `services` + `storage` + `domain`;
- legacy-слой: `legacy_core` и старые CLI-скрипты для извлечения intent/images/videos.

## Карта архитектуры

```text
DOCX -> legacy_core.ingestion -> pipeline.ingestion -> domain.ScriptDocument
     -> pipeline.intents / bootstrap intent -> QueryBundle
     -> pipeline.media -> Provider backends
        -> browser.storyblocks_backend -> browser.session -> Playwright / native browser
        -> providers.images.service -> providers.images.clients -> legacy_core.image_providers
     -> storage.repositories -> run.json / manifest.json
     -> services.events -> EventRecorder / app.log
     -> ui.controller -> ui.qt_app
```

## Главные потоки вызовов

### 1. Запуск desktop-приложения
- `app/__main__.py:main()` создает `DesktopApplication` и при обычном запуске передает его в `ui.launch_desktop_app()`.
- `app/bootstrap.py:bootstrap_application()` поднимает workspace, настройки, репозитории, event bus, provider registry, Storyblocks session manager и media pipeline.
- `ui/__init__.py:launch_desktop_app()` загружает только Qt UI и явно сообщает об отсутствии `PySide6`.

### 2. Импорт сценария
- `ui/controller.py:open_script()` -> `app/runtime.py:DesktopApplication.create_project()`.
- `DesktopApplication.create_project()` вызывает `pipeline.ingestion.ScriptIngestionService.ingest()`.
- `ScriptIngestionService` адаптирует результат `legacy_core.ingestion.ingest_script_docx()` в `domain.models.ScriptDocument`.
- Затем `ParagraphIntentService.bootstrap_document()` заполняет каждому абзацу базовый `ParagraphIntent` и `QueryBundle`.

### 3. Запуск media run
- `ui/controller.py:start_run_async()` или `execute_run()` валидирует форму, применяет настройки и создает `MediaSelectionConfig`.
- `pipeline.media.ParagraphMediaRunService.create_run()` создает `Run` через `RunOrchestrator.create_run()` и `RunManifest` через `ParagraphMediaPipeline.create_manifest()`.
- `ParagraphMediaRunService.execute()` передает абзацы в `RunOrchestrator.execute()`.
- `RunOrchestrator.execute()` вызывает processor для каждого абзаца.
- Processor внутри `ParagraphMediaRunService` делегирует в `ParagraphMediaPipeline.process_paragraph()`.

### 4. Storyblocks-поиск и загрузка
- `ParagraphMediaPipeline` использует зарегистрированный `StoryblocksCandidateSearchBackend`.
- `StoryblocksCandidateSearchBackend.search()` через `BrowserSessionManager` проверяет готовность сессии, открывает persistent browser и запускает `StoryblocksSearchAdapter`.
- HTML страницы проверяется через `StoryblocksDomContractChecker`, карточки парсятся в `StoryblocksSearchAdapter.parse_result_cards()`.
- `download_asset()` использует `PlaywrightDownloadDriver` и `StoryblocksDownloadManager`.

### 5. Бесплатные image providers
- `ParagraphMediaPipeline.build_default_free_image_backends()` создает `FreeImageCandidateSearchBackend` для `pexels`, `pixabay`, `openverse`, `wikimedia`, опционально `bing`.
- `FreeImageCandidateSearchBackend.search()` вызывает `ImageProviderSearchService.search_provider()`.
- `ImageProviderSearchService` строит query plan, читает/пишет SQLite cache, запускает legacy provider client и фильтрует кандидатов.

### 6. Event flow
- `RunOrchestrator` и `ParagraphMediaPipeline` публикуют `AppEvent` в `EventBus`.
- Подписчики: `EventRecorder` и `JsonLineEventLogger`.
- `ui/controller.py` читает `EventRecorder` и строит `UiRunProgressViewModel`, `UiEventJournalItem`, live state и статусы абзацев.

## Модульная карта

## `app`

### `app/__init__.py`
- Реэкспортирует `ApplicationContainer`, `ApplicationSnapshot`, `DesktopApplication`, `bootstrap_application`.

### `app/__main__.py`
- Роль: CLI entry point desktop-приложения.
- Функции:
  - `main()`: парсит `--workspace`, `--smoke`, `--no-gui`; создает приложение; либо печатает snapshot, либо запускает GUI.
- Связи: зависит от `app.runtime` и `ui`.

### `app/bootstrap.py`
- Роль: composition root / DI wiring.
- Типы:
  - `ApplicationContainer`: агрегирует settings, workspace, repositories, services, provider registry, browser/session services, media pipeline и UI-facing сервисы.
- Функции:
  - `bootstrap_application(workspace_root=None)`: создает весь runtime-граф зависимостей.
- Ключевые связи:
  - инициализирует `WorkspaceStorage`, `SettingsManager`, `EventBus`, `EventRecorder`, JSON repositories;
  - создает `ProviderRegistry`, `ImageProviderSearchService`, `BrowserProfileRegistry`, `ChromiumProfileImportService`, `BrowserSessionManager`;
  - регистрирует Storyblocks backends и free-image backends в `ParagraphMediaPipeline`;
  - собирает `ParagraphMediaRunService`.

### `app/runtime.py`
- Роль: тонкий facade над container для UI/CLI.
- Типы:
  - `ApplicationSnapshot`: snapshot workspace root, provider ids и browser profiles.
  - `DesktopApplication`: высокоуровневые операции проекта и run.
- Основные методы `DesktopApplication`:
  - `create()`: создает приложение через `bootstrap_application()`.
  - `start()`: возвращает snapshot для smoke/startup.
  - `create_project()`: ingest DOCX, bootstrap intent/query bundle, сохранить `Project`.
  - `update_paragraph_intent()`: вручную изменить intent/query bundle одного абзаца.
  - `create_media_run()`, `execute_media_run()`, `resume_media_run()`, `retry_failed_media_run()`: прокси к `ParagraphMediaRunService`.
  - `lock_paragraph_selection()`: фиксирует ручной выбор в manifest.

## `config`

### `config/__init__.py`
- Реэкспорт `ApplicationSettings` и related types.

### `config/settings.py`
- Роль: схема runtime-настроек.
- Dataclass-и:
  - `ConcurrencySettings`: limits для paragraph/provider/download/relevance workers и queue size.
  - `BrowserSettings`: automation stack, profile root, browser channels, slow mode, timeouts, Storyblocks base URL.
  - `StorageSettings`: workspace/cache/logs/secrets roots.
  - `ProviderSettings`: project mode, enabled/default providers, image priority, license policy, free-only/mixed fallback flags.
  - `SecuritySettings`: имена secrets для Storyblocks/Gemini/Pexels/Pixabay.
  - `ApplicationSettings`: корневой объект настроек.
- Функции:
  - `default_settings()`: возвращает дефолтный `ApplicationSettings`.

## `domain`

### `domain/__init__.py`
- Реэкспорт enums, models и project modes.

### `domain/enums.py`
- Роль: базовые enum-значения домена.
- Enum-ы:
  - `AssetKind`: `video`, `image`, `audio`.
  - `ProviderCapability`: capability provider-а.
  - `RunStatus`: жизненный цикл run.
  - `RunStage`: текущий этап run.
  - `SessionHealth`: состояние Storyblocks-сессии.
  - `EventLevel`: severity событий.

### `domain/models.py`
- Роль: сериализуемые domain-модели и общая JSON- (de)serialization логика.
- Функции:
  - `utc_now()`: UTC timestamp factory.
  - `_serialize_value()`: сериализация dataclass/path/datetime/enum/list/dict.
  - `_deserialize_value()`: обратная десериализация по type hints.
- Базовый класс:
  - `SerializableModel`: `to_dict()` и `from_dict()`.
- Domain-модели:
  - `QueryBundle`: общие и provider-specific queries.
  - `ParagraphIntent`: subject/action/setting + query lists.
  - `ParagraphUnit`: один абзац сценария.
  - `ScriptDocument`: исходный документ и список абзацев.
  - `AssetCandidate`: найденный медиа-кандидат.
  - `ProviderResult`: результат одного provider search.
  - `AssetSelection`: выбор по абзацу, diagnostics, user decision state.
  - `MediaSlot`: ожидаемые роли ассетов.
  - `ParagraphDiagnostics`: provider queries/results, dedupe и early-stop diagnostics.
  - `ParagraphManifestEntry`: manifest-запись по абзацу.
  - `RunManifest`: manifest запуска.
  - `BrowserProfile`: управляемый профиль браузера.
  - `RunCheckpoint`: checkpoint выполнения.
  - `Run`: runtime state запуска.
  - `Preset`: снапшот настроек.
  - `Project`: проект со script document и active run.

### `domain/project_modes.py`
- Роль: фиксированная матрица режимов проекта и правила выбора provider-ов.
- Типы:
  - `ProjectModeDefinition`: описывает один mode.
- Константы:
  - `DEFAULT_FREE_IMAGE_PROVIDER_IDS`, `OPT_IN_FREE_IMAGE_PROVIDER_IDS`, `ALL_FREE_IMAGE_PROVIDER_IDS`.
- Функции:
  - `list_project_modes()`: список режимов.
  - `get_project_mode()`: получить definition по id.
  - `normalize_project_mode()`: нормализует mode id.
  - `infer_project_mode()`: выводит mode из набора флагов.
  - `normalize_free_image_provider_ids()`: валидирует список free image provider ids.
  - `provider_ids_for_mode()`: собирает provider ids для выбранного режима.

## `storage`

### `storage/__init__.py`
- Реэкспорт repository и workspace API.

### `storage/workspace.py`
- Роль: физическая структура workspace.
- Типы:
  - `WorkspacePaths`: typed paths для config/projects/runs/presets/cache/browser_profiles/logs/secrets.
  - `WorkspaceStorage`: runtime wrapper.
- Функции:
  - `build_workspace_paths(root)`: собрать `WorkspacePaths`.
- Методы:
  - `WorkspaceStorage.initialize()`: создать каталоговую структуру.

### `storage/serialization.py`
- Роль: безопасное чтение/запись JSON.
- Функции:
  - `ensure_parent(path)`: создает parent dir.
  - `read_json(path)`: читает JSON object с retry на `PermissionError`.
  - `write_json(path, payload)`: атомарно пишет JSON через temp file + `os.replace()`.

### `storage/repositories.py`
- Роль: JSON repositories для всех persisted сущностей.
- Типы:
  - `JsonModelRepository[T]`: generic loader/saver через `SerializableModel`.
  - `SettingsRepository`: load/save `ApplicationSettings` с восстановлением mode и nested settings.
  - `ProjectRepository`: хранит `projects/<id>/project.json`.
  - `RunRepository`: хранит `runs/<id>/run.json`.
  - `ManifestRepository`: хранит `runs/<id>/manifest.json`.
  - `PresetRepository`: хранит `presets/<name>.json`.
  - `BrowserProfileRepository`: хранит `browser_profiles/<id>.json`.
- Важные методы:
  - `path_for()`, `load()`, `save()`, `list_all()` / `list_names()`.

## `services`

### `services/__init__.py`
- Реэкспорт ошибок, event infra, secret store, settings manager, Gemini helpers.

### `services/errors.py`
- Роль: typed application exceptions.
- Типы:
  - `AppError`: базовая ошибка с `code`, `message`, `details`, `to_ui_payload()`.
  - `ConfigError`, `SessionError`, `ProviderError`, `DownloadError`, `RelevanceError`, `PersistenceError`: специализированные ошибки.

### `services/events.py`
- Роль: in-memory event bus и recorder.
- Функции:
  - `event_now()`: UTC timestamp.
- Типы:
  - `AppEvent`: единая структура события.
  - `EventBus`: `subscribe()`, `publish()`.
  - `EventRecorder`: callable-listener; сохраняет события, индексирует по run и по paragraph.

### `services/logging.py`
- Роль: persist событий в JSONL.
- Типы:
  - `JsonLineEventLogger`: пишет `AppEvent` в `logs/app.log`.
- Методы:
  - `write(event)`.

### `services/secrets.py`
- Роль: локальное безопасное хранение секретов.
- Внутренние типы/функции:
  - `_DataBlob`, `_blob_from_bytes()`, `_bytes_from_blob()`.
- Типы:
  - `SecretStore`: файловое хранилище `.secret` c DPAPI на Windows и base64 fallback вне Windows.
- Методы:
  - `set_secret()`, `get_secret()`, `delete_secret()`.

### `services/genai_client.py`
- Роль: тонкий адаптер над Gemini SDK.
- Функции:
  - `get_transient_exceptions()`: возвращает retryable Google exceptions.
  - `ensure_gemini_sdk_available()`: проверка SDK.
  - `create_gemini_model(api_key, model_name)`: build `GeminiModelAdapter`.
- Типы:
  - `GeminiModelAdapter`: `generate_content()` и `_normalize_contents()` для text/binary payloads.

### `services/settings_manager.py`
- Роль: orchestration layer над settings/presets/secrets.
- Типы:
  - `SettingsManager`.
- Основные методы:
  - `load()`, `save()`, `get_or_create()`.
  - `save_preset()`, `load_preset()`, `list_presets()`, `list_preset_objects()`, `apply_preset()`.
  - `set_secret()`, `get_secret()`, `delete_secret()`.
  - `export_preset()`, `import_preset()`.
- Вспомогательная функция:
  - `_merge_dataclass(instance, patch)`: рекурсивный merge dataclass snapshot-а в runtime settings.

## `providers`

### `providers/__init__.py`
- Реэкспорт provider registry и image subsystem.

### `providers/base.py`
- Роль: базовые provider descriptors.
- Типы:
  - `ProviderFactory`: protocol factory signature.
  - `ProviderDescriptor`: описание provider-а (id, capability, group, priority, auth, opt-in, legacy, metadata).

### `providers/registry.py`
- Роль: единый каталог provider-ов и правила включения.
- Типы:
  - `ProviderRegistry`.
- Основные методы:
  - `register()`, `get()`, `list_all()`, `list_by_capability()`, `list_by_group()`.
  - `resolve_enabled(settings, capability=None, include_opt_in=False)`: фильтрует provider-ы по mode/settings.
  - `default_image_descriptors()`: default image set.
  - `resolve_image_strategy()`: делит image providers на `primary` и `fallback`.
- Функции:
  - `build_default_provider_registry()`: регистрирует `storyblocks_video`, `storyblocks_image`, `pexels`, `pixabay`, `openverse`, `wikimedia`, `bing`.

### `providers/images/__init__.py`
- Реэкспорт caching/querying/filtering/client/service API.

### `providers/images/clients.py`
- Роль: bridge между новым provider API и `legacy_core.image_providers`.
- Типы:
  - `ImageSearchProvider`: protocol с `search()`.
  - `ImageProviderBuildContext`: timeout/user agent/API keys/options.
  - `WrappedImageSearchProvider`: новый wrapper поверх legacy client-а.
- Функции:
  - `build_image_provider_clients()`: собирает wrapped providers из registry и context.
  - `_build_single_provider()`: factory для `pexels`, `pixabay`, `openverse`, `wikimedia`, `bing`.
  - `default_cache_root(base_dir)`: root SQLite caches.

### `providers/images/querying.py`
- Роль: provider-specific query rewriting.
- Типы:
  - `ProviderQueryPlan`: provider id + query list.
  - `ImageQueryPlanner`: нормализация и генерация query variants.
- Методы/функции:
  - `normalize()`: whitespace normalization.
  - `rewrite_for_provider()`: добавляет суффиксы вроде `photo`, `cinematic`, `realistic`.
  - `_suffixes_for()`: возвращает suffixes по provider group.

### `providers/images/filtering.py`
- Роль: prefilter/ranking кандидатов.
- Типы:
  - `ImageLicensePolicy`: коммерческое использование и attribution policy.
  - `RankedCandidate`: candidate + score + reasons.
- Функции:
  - `filter_and_rank_candidates()`: license filtering + quality filter + rank.
  - `cached_quality_assessment()`: cache wrapper.
  - `assess_candidate_quality()`: вычисляет score и отбрасывает low-quality assets.

### `providers/images/caching.py`
- Роль: SQLite cache для provider search и metadata score.
- Функции:
  - `_serialize_candidate()`, `_deserialize_candidate()`.
- Типы:
  - `SearchResultCache`: cache по `(provider_id, query, limit)`.
  - `MetadataCache`: cache quality assessment по `cache_key`.

### `providers/images/service.py`
- Роль: оркестратор free-image поиска.
- Типы:
  - `ImageSearchDiagnostics`: provider queries, rejected prefilters, cache hits.
  - `ImageProviderSearchService`: основной сервис.
- Основные методы:
  - `build_providers()`: собрать provider clients.
  - `search_keyword()`: обойти несколько providers, дедуплицировать URL, объединить результаты.
  - `search_provider()`: для одного provider-а строит query plan, использует cache, запускает search и filtering.
  - `close()`: закрывает caches.

## `browser`

### `browser/__init__.py`
- Реэкспорт browser/session/storyblocks API.

### `browser/automation.py`
- Роль: управление persistent Playwright browser и browser-channel discovery.
- Вспомогательные функции:
  - `_now()`, `_current_thread_id()`, `_current_thread_name()`, `_is_browser_internal_url()`.
  - `select_preferred_page(pages, target_url)`: выбирает лучшую вкладку для Storyblocks.
  - `build_launch_plan()`: собирает `BrowserLaunchPlan` из profile paths и settings.
- Типы:
  - `BrowserChannelAvailability`: availability одного browser channel.
  - `BrowserLaunchPlan`: параметры запуска persistent context.
  - `PersistentBrowserHandle`: Playwright context/page + `close()`.
  - `PersistentBrowserSession`: runtime session с owner thread metadata.
  - `PersistentContextFactory`: protocol.
  - `BrowserChannelResolver`: поиск `chrome` / `msedge` executable.
  - `BrowserProfileLockProbe`: проверка lock-файлов профиля.
  - `PlaywrightPersistentContextFactory`: launch/attach persistent browser.

### `browser/native_browser.py`
- Роль: открыть нативный Chrome/Edge для ручного Storyblocks login и remote debugging attach.
- Функции:
  - `_now()`, `find_available_tcp_port()`.
- Типы:
  - `NativeBrowserLaunchPlan`, `NativeBrowserSession`.
  - `NativeBrowserLauncher`: protocol.
  - `SubprocessNativeBrowserLauncher`: запускает subprocess с `--remote-debugging-port`.

### `browser/profiles.py`
- Роль: registry app-managed browser profiles.
- Функции:
  - `_now()`, `build_browser_profile_paths(root)`.
- Типы:
  - `BrowserProfilePaths`: `user_data`, `downloads`, `diagnostics`, lock path.
  - `BrowserProfileRegistry`.
- Основные методы:
  - `list_profiles()`, `save_profile()`, `create_profile()`, `ensure_profile_structure()`, `paths_for()`.
  - `rename_profile()`, `delete_profile()`, `set_active()`, `select_profile()`, `get_active()`, `get_profile()`.
  - `update_session_health()`, `update_storyblocks_account()`.

### `browser/profile_import.py`
- Роль: импорт внешнего Chrome/Edge profile в managed Storyblocks profile.
- Функции:
  - `_now()`.
- Типы:
  - `ImportableBrowserSession`: внешний profile source.
  - `ChromiumProfileImportService`: главный import service.
- Основные методы:
  - `discover_profiles()`: найти локальные Chromium profiles.
  - `resolve_source()`: валидировать/нормализовать выбранный source path.
  - `import_profile()`: скопировать внешний profile в managed profile.
  - `reimport_profile()`: переимпорт из ранее сохраненного source.
  - внутренние helpers: `_resolve_target_profile()`, `_copy_source_profile()`, `_patch_local_state()`, `_swap_user_data_dir()`, `_write_import_diagnostics()`, `_candidate_user_data_roots()`, `_profile_dirs()`, `_looks_like_profile_dir()`, `_is_external_root_locked()`, `_profile_label()`, `_browser_label()`, `_infer_browser_name()`, `_ignore_copy()`.

### `browser/slowmode.py`
- Роль: pacing/backoff для browser actions.
- Типы:
  - `SlowModePolicy`: slow mode settings.
  - `BrowserActionPacer`: delay/backoff state machine.
- Методы:
  - `SlowModePolicy.from_settings()`.
  - `BrowserActionPacer.current_backoff_seconds`, `before_action()`, `next_delay_seconds()`, `record_failure()`, `record_success()`.

### `browser/session.py`
- Роль: центральный state manager Storyblocks-сессии.
- Типы:
  - `ManualInterventionRequest`: что должен сделать пользователь.
  - `AuthorizationSnapshot`: вывод session probe.
  - `SessionProbe`: protocol HTML/page inspection.
  - `BrowserSessionState`: полный session state для UI.
  - `BrowserSessionManager`: основной orchestration class.
- Основные методы `BrowserSessionManager`:
  - state/profile: `current_state()`, `set_health()`, `set_manual_ready_override()`, `clear_manual_ready_override()`, `has_manual_ready_override()`, `check_browser_channel()`, `profile_in_use()`;
  - browser lifecycle: `open_browser()`, `open_native_login_browser()`, `close_native_browser()`, `native_browser_running()`, `close_browser()`, `close_browsers_owned_by_current_thread()`;
  - auth/manual flow: `check_authorization()`, `require_manual_login()`, `register_challenge()`, `mark_blocked()`, `confirm_manual_intervention()`, `wait_for_user()`, `restore_session()`;
  - rescue/pacing: `record_instability()`, `record_stable_action()`, `open_rescue_url()`, `rescue_storyblocks_query()`;
  - internal: `_attach_to_native_browser()`, `_select_storyblocks_page()`, `_ensure_storyblocks_page()`, `_set_manual_intervention()`, `_resolve_profile()`, `_refresh_native_browser_session()`, `_write_native_login_diagnostics()`.
- Ключевые особенности:
  - защищает browser session thread ownership;
  - умеет attach-иться к нативному login browser по CDP;
  - пишет diagnostics в profile diagnostics dir.

### `browser/storyblocks.py`
- Роль: Storyblocks DOM/session/search helpers.
- Функции:
  - `_normalize_text()`, `slugify_storyblocks_query()`.
  - `_extract_asset_id()`, `_make_candidate()`.
  - `first_available_selector()`: поиск доступного selector-а на странице.
  - `capture_storyblocks_page_snapshot()`: снимает HTML snapshot с retry на transient navigation.
  - `_is_transient_storyblocks_navigation_error()`.
- Типы:
  - `StoryblocksSearchFilter`, `StoryblocksSelectorCatalog`.
  - `StoryblocksDomContractResult`, `StoryblocksPageSnapshot`.
  - `StoryblocksDomContractChecker`: проверяет дрейф DOM-контракта.
  - `StoryblocksSessionProbe`: определяет `SessionHealth` по HTML/URL.
  - `_ParsedCard`, `_StoryblocksGalleryParser`: парсер карточек результатов.
  - `StoryblocksSearchAdapterBase`, `StoryblocksVideoSearchAdapter`, `StoryblocksImageSearchAdapter`.

### `browser/downloads.py`
- Роль: Storyblocks download subsystem.
- Функции:
  - `_now()`.
- Типы:
  - `StoryblocksDownloadRequest`, `StoryblocksDownloadRecord`.
  - `StoryblocksDownloadDriver`: protocol.
  - `PlaywrightDownloadDriver`: открывает detail page и нажимает Download.
  - `StoryblocksDownloadManager`: очередь, retry и dedupe загрузок.
- Основные методы:
  - `enqueue()`, `pending_count()`, `run_queue()`, `download_one()`, `_persist_payload()`, `_is_complete_file()`.

### `browser/storyblocks_backend.py`
- Роль: pipeline-facing backend для Storyblocks search/download.
- Типы:
  - `StoryblocksCandidateSearchBackend`.
- Основные методы:
  - `_raise_for_session_state()`: маппит `SessionHealth` в `SessionError` и manual flow.
  - `search()`: проверяет session, открывает browser, делает search, валидирует DOM, парсит candidates, возвращает `ProviderResult`.
  - `download_asset()`: скачивает detail asset через `StoryblocksDownloadManager`.

## `pipeline`

### `pipeline/__init__.py`
- Реэкспорт pipeline services.

### `pipeline/backpressure.py`
- Роль: bounded wrapper над `ThreadPoolExecutor`.
- Типы:
  - `BoundedExecutor`.
- Методы:
  - `submit()`, `map_unordered()`, `shutdown()`, context manager methods.

### `pipeline/ingestion.py`
- Роль: adapter из legacy ingestion в domain models.
- Типы:
  - `ScriptIngestionService`.
- Методы:
  - `ingest(file_path)`: вызывает `legacy_core.ingestion.ingest_script_docx()`, переносит issues в `ScriptDocument` и `ParagraphUnit.validation_issues`.

### `pipeline/intents.py`
- Роль: построение intent и query bundle для абзацев.
- Helper-функции:
  - `_clean_model_text()`: чистка markdown/code fences.
  - `_parse_json_object()`: достать JSON из текста модели.
  - `_normalize_string()`, `_normalize_string_list()`.
  - `_detect_language()`, `_tokenize_words()`, `_unique_strings()`.
  - `_extract_focus_terms()`, `_derive_subject_action_setting()`.
  - `_is_query_too_abstract()`, `_compose_visual_query()`, `_sanitize_queries()`, `_append_photo_hint()`.
- Типы:
  - `ParagraphIntentService`.
- Основные методы `ParagraphIntentService`:
  - `_validate_strictness()`, `build_prompt()`.
  - `_generate_intent_raw()`: Gemini call c retry по transient errors.
  - `_finalize_intent()`: заполняет fallback subject/action/setting, чистит queries и duration.
  - `parse_intent_response()`: JSON -> `ParagraphIntent`.
  - `_build_storyblocks_video_queries()`, `_build_storyblocks_image_queries()`, `_build_free_image_queries()`, `_build_generic_web_image_queries()`.
  - `build_query_bundle()`.
  - `bootstrap_paragraph_intent()`, `bootstrap_document()`: полностью эвристический режим без LLM.
  - `extract_paragraph_intent()`: полный Gemini pipeline для одного абзаца.
  - `build_item_payload()`, `extract_document()`, `build_output_payload()`, `save_intents_json()`.
  - `apply_manual_edit()`: обновляет intent/query bundle после UI-edit.

### `pipeline/orchestrator.py`
- Роль: lifecycle и execution engine для `Run`.
- Функции:
  - `_now()`.
- Типы:
  - `RunControls`: flags pause/cancel.
  - `RunOrchestrator`: основной executor.
- Основные методы:
  - `create_run()`: создает `Run` и `RunCheckpoint`.
  - `pause_after_current()`, `cancel()`, `is_cancel_requested()`.
  - `execute()`: sequential/parallel execution paragraph processor-а, checkpointing, event emission.
  - `resume()`: повторно запускает непроцессенные абзацы.
  - `rerun_selected()`: отдельный rerun по выбранным paragraph numbers.
  - `_cancel_run()`, `_emit()`.

### `pipeline/media.py`
- Роль: центральный media-selection pipeline desktop-версии.
- Функции:
  - `_normalize_text()`.
- Протоколы:
  - `CandidateSearchBackend`: общий контракт поиска кандидатов.
  - `AssetDownloadBackend`: общий контракт скачивания.
- Типы:
  - `MediaSelectionConfig`: runtime config media run.
  - `CallbackCandidateSearchBackend`: оборачивает callback search function.
  - `FreeImageCandidateSearchBackend`: desktop backend для free-image providers.
  - `AssetDeduper`: дедуп по source id/hash/semantic signature.
  - `ParagraphMediaPipeline`: основной paragraph-level engine.
  - `ParagraphMediaRunService`: orchestration вокруг runs/manifests.
- Основные методы `ParagraphMediaPipeline`:
  - registration: `register_backend()`, `register_backends()`, `build_default_free_image_backends()`, `_clear_free_image_backends()`;
  - manifest: `create_manifest()`, `load_manifest()`, `save_manifest()`, `lock_selection()`, `update_summary()`;
  - core processing: `process_paragraph()`, `_collect_results()`, `_collect_image_results()`, `_collect_image_result_list()`, `_search_backend()`, `_queries_for_backend()`, `_resolve_video_backends()`;
  - downloads: `_download_primary_video()`, `_download_selected_image_asset()`, `_download_image_assets()`, `_download_image_asset()`;
  - output/layout: `_shared_video_output_dir()`, `_shared_image_output_dir()`, `_run_download_root()`, `_asset_filename()`, `_asset_extension()`, `_filename_slug()`;
  - selection helpers: `_build_slots()`, `_slots_with_selection()`, `_choose_best_candidate()`, `_top_assets()`, `_asset_rank()`, `_build_deduper()`, `_entry_for()`, `_total_candidates()`, `_sourcing_strategy_payload()`, `_emit()`, `_raise_if_cancelled()`.
- Основные методы `ParagraphMediaRunService`:
  - `create_run()`, `execute()`, `resume()`, `retry_failed_only()`, `rerun_selected()`, `create_and_execute()`.
  - `lock_selection()`, `load_manifest()`, `pause_after_current()`, `cancel()`.
  - внутренние: `_require_project()`, `_require_run()`, `_selection_config_for_run()`.

## `ui`

### `ui/__init__.py`
- Роль: пакетный entry point для desktop UI.
- Функции:
  - `launch_desktop_app(controller)`: загружает `ui.qt_app` и запускает единственный поддерживаемый `Qt` path.
- Реэкспортирует controller, contracts и Qt-only launcher.

### `ui/contracts.py`
- Роль: view-model слой для UI.
- Dataclass-и:
  - `UiNotification`, `UiEventJournalItem`, `UiErrorPayload`, `UiPresetViewModel`, `UiImportableSessionOption`, `UiProjectSummary`, `UiRunHistoryItem`, `UiAssetPreview`, `UiParagraphWorkbenchItem`, `UiSessionPanelViewModel`, `UiRunPreviewViewModel`, `UiRunProgressViewModel`, `UiLiveRunStateViewModel`, `UiQuickLaunchSettingsViewModel`, `UiAdvancedSettingsViewModel`, `UiStateViewModel`.

### `ui/presentation.py`
- Роль: UI labels, translations и theme definitions.
- Типы:
  - `UiThemeSpec`.
- Функции:
  - `normalize_ui_theme()`, `get_ui_theme()`, `label_for_theme()`.
  - `label_for_strictness()`, `strictness_value_from_label()`.
  - `map_label()` и набор `translate_*()` функций для providers/status/stages/health/roles.
  - `yes_no()`, `on_off()`, `translate_error_text()`.

### `ui/controller.py`
- Роль: главный application controller для GUI; это основной integration point между UI и runtime.
- Вспомогательные элементы:
  - `_iso()`.
  - `_BackgroundRunTask`: состояние фонового запуска.
  - `handle_ui_error()`: `Exception` -> `UiNotification`.
  - `_copy_dataclass_values()`: копирование nested settings.
- Основные группы методов `DesktopGuiController`:
  - создание/состояние: `create()`, `session_actions_enabled()`, `_ensure_session_actions_available()`, `build_state()`, `build_live_run_state()`, `_resolve_active_run_id()`;
  - списки/preview: `list_projects()`, `list_run_history()`, `list_presets()`, `build_quick_launch_settings()`, `build_advanced_settings()`, `build_run_preview()`, `build_paragraph_workbench()`, `build_paragraph_detail()`;
  - project/run actions: `open_script()`, `update_paragraph_queries()`, `execute_run()`, `start_run_async()`, `resume_run()`, `resume_run_async()`, `retry_failed_run()`, `retry_failed_run_async()`, `rerun_current_paragraph()`, `rerun_current_paragraph_async()`, `rerun_selected_paragraphs()`, `rerun_selected_paragraphs_async()`, `pause_run()`, `stop_after_current()`, `cancel_run()`, `lock_asset()`, `reject_asset()`;
  - Storyblocks/session actions: `session_panel()`, `discover_storyblocks_sessions()`, `ensure_active_profile()`, `open_storyblocks_browser()`, `check_storyblocks_session()`, `mark_storyblocks_session_ready()`, `clear_storyblocks_session_override()`, `prepare_storyblocks_login()`, `logout_storyblocks()`, `switch_storyblocks_account()`, `clear_storyblocks_profile()`, `import_storyblocks_session_from_path()`, `reimport_storyblocks_session()`;
  - presets/secrets/theme: `save_preset()`, `load_preset()`, `export_preset()`, `import_preset()`, `set_gemini_key()`, `set_provider_api_key()`, `get_provider_api_key()`, `delete_provider_api_key()`, `get_gemini_key()`, `delete_gemini_key()`, `validate_gemini_key()`, `get_ui_theme()`, `set_ui_theme()`;
  - settings/media config helpers: `_provider_api_secret_name()`, `_provider_display_name()`, `_rebuild_free_image_backends()`, `apply_forms_to_settings()`, `_apply_settings_object()`, `_build_settings_snapshot()`, `_media_config_from_forms()`, `_selected_provider_ids()`;
  - progress/logging helpers: `build_event_journal()`, `build_run_progress()`, `format_run_log()`, `export_run_log()`, `_status_text()`, `_eta_text()`, `_session_indicator_tone()`, `_latest_events_by_paragraph()`, `_live_state()`, `_checkpoint_message()`;
  - background execution helpers: `_push_notification()`, `_start_background_run()`, `_run_background_task()`, `_finalize_background_run_if_needed()`;
  - model lookup/render helpers: `_project_summary()`, `_validate_run_request()`, `_selected_assets()`, `_candidate_assets()`, `_asset_preview()`, `_intent_summary()`, `_require_project()`, `_require_run()`, `_safe_load_run()`, `_safe_load_manifest()`, `_require_paragraph()`, `_require_manifest()`, `_require_manifest_entry()`, `_find_asset()`.
- Это главный hotspot для большинства будущих UI-правок.

### `ui/qt_app.py`
- Роль: единственная поддерживаемая PySide6 GUI-реализация.
- Типы:
  - `DesktopQtApp`.
- Основные группы методов:
  - построение UI: `_build_ui()`, `_build_main_tab()`, `_build_api_keys_tab()`, `_build_advanced_tab()`, `_build_session_tab()`, `_build_history_tab()`, `_build_workspace()`, `_quick_form()`, `_advanced_form()`;
  - refresh/render: `refresh()`, `_poll_refresh()`, `_apply_live_state()`, `refresh_preview()`, `_apply_state()`, `_set_session_actions_enabled()`, `_fill_project_list()`, `_fill_history_list()`, `_fill_paragraph_list()`, `_render_preview()`, `_render_run_progress()`, `_fill_journal()`, `_render_current_paragraph_detail()`, `_render_session()`, `_render_paragraph_detail()`;
  - user actions: `on_browse_script()`, `on_browse_output_dir()`, `on_open_script()`, `on_start_run()`, `on_resume_run()`, `on_pause_run()`, `on_stop_after_current()`, `on_abort_run()`, `on_retry_failed()`, preset actions, API-key actions, session actions, selection actions, `on_theme_changed()`;
  - helpers: `_paragraph_signature()`, `_journal_signature()`, `_current_paragraph_number()`, `_current_candidate_asset_id()`, `_apply_quick_form()`, `_apply_advanced_form()`, `_run_session_action()`, `_import_existing_session()`, `_show_notification()`, `_apply_theme()`.
- Функции:
  - `launch_pyside_app()`, `_wrap_layout()`, `_spin_box()`.

## `legacy_core`

### `legacy_core/__init__.py`
- Реэкспорт минимального legacy API.

### `legacy_core/common.py`
- Роль: базовые helpers.
- Функции:
  - `safe_int()`, `safe_float()`.
  - `normalize_whitespace()`.
  - `normalize_keywords()`.
  - `slugify()`.

### `legacy_core/diagnostics.py`
- Роль: provider-statistics helpers для legacy pipelines.
- Функции:
  - `init_provider_stats()`.
  - `bump_provider_stat()`.
  - `build_provider_limit_summary()`.

### `legacy_core/env.py`
- Роль: `.env` discovery/load.
- Функции:
  - `get_env_path()`.
  - `load_dotenv()`.

### `legacy_core/files.py`
- Роль: output path и temp file helpers.
- Функции:
  - `sha256_bytes()`.
  - `resolve_output_json_path()`.
  - `build_run_dir()`.
  - `write_hashed_temp_file()`.

### `legacy_core/ingestion.py`
- Роль: исходный DOCX-ingestion engine.
- Типы:
  - `IngestionIssue`, `ParagraphRecord`, `ScriptIngestionResult`.
- Функции:
  - `_paragraph_has_word_numbering()`, `_paragraph_style_name()`, `_looks_like_heading()`.
  - `normalize_paragraph_payload()`.
  - `ingest_script_docx()`: читает DOCX, извлекает numbered paragraphs и numbering issues.
  - `read_script_paragraphs()`: legacy-friendly payload wrapper.

### `legacy_core/keyword_payload.py`
- Роль: чтение paragraph task-ов из intent/keyword JSON.
- Типы:
  - `ParagraphKeywordTask`.
- Функции:
  - `load_keywords_payload()`.
  - `_extract_queries_from_item()`.
  - `extract_paragraph_tasks()`.

### `legacy_core/licenses.py`
- Роль: нормализация лицензий и policy check.
- Функции:
  - `normalize_license_info()`.
  - `is_license_allowed()`.

### `legacy_core/network.py`
- Роль: безопасные HTTP helpers.
- Типы:
  - `NoRedirectHandler`.
- Функции:
  - `http_get_json()`.
  - `is_public_host()`.
  - `validate_public_url()`.
  - `open_with_safe_redirects()`.
  - `read_limited()`.

### `legacy_core/query_utils.py`
- Роль: query and source helpers.
- Функции:
  - `parse_sources()`.
  - `tokenize()`.
  - `candidate_hint_score()`.
  - `build_query_variants()`.

### `legacy_core/retry.py`
- Роль: общий retry wrapper.
- Функции:
  - `retry_call()`.

### `legacy_core/relevance.py`
- Роль: parsing/caching для LLM relevance checks.
- Функции:
  - `clean_model_text()`.
  - `parse_relevance_response()`.
- Типы:
  - `SimpleRateLimiter`.
  - `ImageRelevanceCache`.
  - `VideoRelevanceCache`.

### `legacy_core/video_tools.py`
- Роль: ffmpeg/ffprobe helpers для legacy video pipeline.
- Функции:
  - `ensure_ffmpeg_tools_available()`.
  - `parse_frame_rate()`.
  - `run_command()`.
  - `probe_video()`.
  - `guess_video_extension()`.
  - `validate_video_quality()`.

### `legacy_core/image_providers.py`
- Роль: legacy implementations image providers.
- Вспомогательные функции:
  - `_strip_html()`, `_install_imghdr_compat()`, `_load_bing_search_func()`.
- Типы:
  - `SearchCandidate`.
  - `PexelsProvider`, `PixabayProvider`, `OpenverseProvider`, `WikimediaProvider`, `BingProvider`.
- Функции:
  - `build_image_providers()`: factory набора legacy image providers.

## Top-level CLI и release tooling

### `keyword_extractor.py`
- Роль: CLI для Gemini-based intent extraction.
- Связи: использует новую `ParagraphIntentService` и `ScriptIngestionService`, но запускается как отдельный script.
- Функции:
  - `get_env_path()`, `load_dotenv()`, `setup_model()`.
  - `read_script_paragraphs()`.
  - `extract_intents_for_script()`.
  - `save_paragraph_intents_json()`.
  - `run_intent_extraction()`.
  - `run_keyword_extraction()`.
  - `parse_arguments()`, `main()`.

### `image_fetcher.py`
- Роль: legacy CLI pipeline для image fetching и Gemini relevance filtering.
- Функциональные блоки:
  - env/runtime: `get_env_path()`, `load_dotenv()`, `_ensure_runtime_dependencies()`, `_install_imghdr_compat()`, `_load_bing_search_func()`;
  - parsing/builders: `_slugify()`, `_safe_int()`, `_normalize_keywords()`, `_extract_paragraph_tasks()`, `_parse_sources()`, `_build_query_variants()`;
  - provider/network wrappers: `_http_get_json()`, `_strip_html()`, `_normalize_license_info()`, `_is_license_allowed()`, `_is_public_host()`, `_validate_public_url()`, `_open_with_safe_redirects()`, `_read_limited()`;
  - provider setup: `_configure_image_provider_search_service()`, `_close_image_provider_search_service()`, `_build_providers()`;
  - download/output helpers: `_normalize_image_bytes()`, `_download_image_candidate()`, `_download_with_retries()`, `_resolve_output_path()`, `_build_run_dir()`, `_build_flat_image_path()`;
  - relevance: `_clean_model_text()`, `_parse_relevance_response()`, `_configure_genai()`, `SimpleRateLimiter`, `RelevanceCache`, `RelevanceEvaluator`, `_evaluate_relevance_parallel()`;
  - pipeline: `_load_keywords_payload()`, `_collect_candidates_for_keyword()`, `_download_candidates_parallel()`, `run_image_fetch()`, `parse_arguments()`, `main()`.
- Важно: этот файл частично уже использует новую provider subsystem (`ImageProviderSearchService`, `ProviderRegistry`), но основной flow остается legacy/CLI.

### `video_fetcher.py`
- Роль: legacy CLI pipeline для video fetching и Gemini relevance filtering.
- Типы:
  - `VideoCandidate`, `ParagraphTask`, `VideoRelevanceResult`.
  - `PexelsVideoProvider`, `PixabayVideoProvider`, `WikimediaVideoProvider`.
  - `SimpleRateLimiter`, `VideoRelevanceCache`, `VideoRelevanceEvaluator`.
- Функциональные блоки:
  - env/runtime: `get_env_path()`, `load_dotenv()`, `_ensure_runtime_dependencies()`;
  - parsing/network wrappers: `_safe_int()`, `_safe_float()`, `_normalize_keywords()`, `_extract_paragraph_tasks()`, `_http_get_json()`, `_strip_html()`, `_normalize_license_info()`, `_is_license_allowed()`, `_parse_sources()`, `_build_query_variants()`, `_is_public_host()`, `_validate_public_url()`, `_open_with_safe_redirects()`, `_read_limited()`;
  - ffmpeg wrappers: `_parse_frame_rate()`, `_run_command()`, `_probe_video()`, `_guess_extension()`, `_validate_video_quality()`;
  - download/output: `_download_video_candidate()`, `_download_with_retries()`, `_resolve_output_path()`, `_build_run_dir()`, `_build_flat_video_path()`, `_cleanup_downloaded_files()`;
  - relevance: `_clean_model_text()`, `_parse_relevance_response()`, `_configure_genai()`, `_evaluate_relevance_parallel()`;
  - pipeline: `_load_keywords_payload()`, `_bump_provider_stat()`, `_collect_candidates_for_keyword()`, `_download_candidates_parallel()`, `run_video_fetch()`, `parse_arguments()`, `main()`.
- Важно: video CLI еще сильнее отделен от новой desktop-архитектуры, чем `image_fetcher.py`.

### `release_tools/portable.py`
- Роль: сборка portable bundle.
- Типы:
  - `PortableBuildResult`.
- Функции:
  - `_utc_stamp()`.
  - `build_portable_bundle()`: копирует runtime-модули, создает workspace, launcher scripts и manifest.
  - `_ignore_names()`, `_write_launchers()`, `_write_manifest()`, `_zip_bundle()`.
  - `main()`.

## Тестовый слой

`tests/` покрывает ключевые зоны:

- `test_ingestion.py`, `test_paragraph_intents.py`: ingestion и intent generation.
- `test_image_provider_architecture.py`, `test_media_pipeline.py`: provider architecture и desktop media pipeline.
- `test_storyblocks_browser_core.py`: browser/session/download core.
- `test_ui_controller.py`: controller-level сценарии.
- `test_core_utilities.py`, `test_env_and_payload.py`, `test_relevance_cache.py`: legacy/shared utilities.
- `test_phase2_architecture.py`, `test_phase9_reliability.py`, `test_phase10_release.py`: архитектурные/regression проверки по фазам проекта.

## Граница между новой и legacy-архитектурой

### Новый слой
- `app`, `browser`, `config`, `domain`, `pipeline`, `providers`, `services`, `storage`, `ui`.
- Это основной слой desktop-приложения и будущих UI/runtime правок.

### Legacy-слой
- `legacy_core/*`, `image_fetcher.py`, `video_fetcher.py`.
- Это старые CLI-пайплайны и переиспользуемые низкоуровневые utilities.

### Точки стыка
- `pipeline/ingestion.py` -> `legacy_core.ingestion`.
- `providers/images/clients.py` -> `legacy_core.image_providers`.
- `providers/images/filtering.py` -> `legacy_core.licenses`, `legacy_core.query_utils`.
- `providers/images/querying.py` -> `legacy_core.common`, `legacy_core.query_utils`.
- `pipeline/media.py` -> `legacy_core.network` для direct downloads.
- `keyword_extractor.py` одновременно использует новую `pipeline`-логику и legacy env/ingestion helpers.

## Наблюдения, важные для будущих правок

1. `DesktopApplication.create_project()` использует `ParagraphIntentService.bootstrap_document()`, а не `extract_document()`.
   - То есть GUI при импорте сценария строит intent/query bundle эвристически, без Gemini.
   - Полный Gemini extraction сейчас живет в отдельном CLI `keyword_extractor.py`.

2. В `app/bootstrap.py` есть два `RunOrchestrator`.
   - `ApplicationContainer.orchestrator` создается с настройками `paragraph_workers` и `queue_size`.
   - Но `ParagraphMediaRunService` получает другой `RunOrchestrator(max_workers=1, queue_size=1)`.
   - Фактическое выполнение run идет через `media_run_service`, то есть сейчас desktop run фактически однопоточный.

3. `MediaSelectionConfig.bounded_downloads` и `bounded_relevance_queue` сейчас не участвуют в реальном исполнении `pipeline/media.py`.
   - Они сериализуются в config/diagnostics, но не управляют concurrency в desktop pipeline.

4. Desktop `pipeline/media.py` не использует Gemini relevance scoring.
   - Этап `relevance` в desktop flow по сути означает эвристическое ранжирование по `rank_hint` и выбор top asset-ов.
   - Gemini-based relevance сейчас есть только в legacy CLI `image_fetcher.py` и `video_fetcher.py`.

5. `ui/controller.py` - самый плотный integration hotspot.
   - Любые правки в run lifecycle, Storyblocks session UX, presets, API keys, preview или paragraph workbench почти наверняка потребуют изменения именно здесь.

6. `browser/session.py` - второй критический hotspot.
   - Здесь завязаны thread ownership, native browser attach, manual override, rescue flow и session diagnostics.
   - Любые изменения Storyblocks auth/login flow лучше начинать с этого файла.

7. `providers/registry.py` и `domain/project_modes.py` определяют provider composition.
   - Если нужно менять режимы проекта, набор доступных provider-ов, fallback-стратегию или opt-in behavior, это главные файлы.

8. `storage/repositories.py` и `domain/models.py` задают persistence contract.
   - Изменения schema run/project/manifest/profile должны быть согласованы между этими файлами и UI/controller layer.

## Модули, которые, скорее всего, понадобятся при будущих задачах

- `ui/controller.py`: почти все пользовательские действия и состояние экрана.
- `pipeline/media.py`: логика подбора/скачивания/manifest-а.
- `browser/session.py`: Storyblocks session/login/manual intervention.
- `browser/storyblocks_backend.py`: интеграция Storyblocks с pipeline.
- `providers/registry.py` и `providers/images/service.py`: provider selection и free-image search.
- `pipeline/intents.py`: query generation и manual edit behavior.
- `app/bootstrap.py`: wiring и замена зависимостей.
- `storage/repositories.py` и `domain/models.py`: любые persisted contract changes.

## Короткий dependency map

- `ui.*` -> `ui.controller` -> `app.runtime.DesktopApplication`.
- `DesktopApplication` -> `ApplicationContainer` -> repositories/services/pipeline/browser.
- `ParagraphMediaRunService` -> `RunOrchestrator` + `ParagraphMediaPipeline`.
- `ParagraphMediaPipeline` -> `ProviderRegistry` + Storyblocks/free-image backends + `ManifestRepository` + `EventBus`.
- `StoryblocksCandidateSearchBackend` -> `BrowserSessionManager` + `StoryblocksSearchAdapter` + `StoryblocksDownloadManager`.
- `FreeImageCandidateSearchBackend` -> `ImageProviderSearchService` -> `WrappedImageSearchProvider` -> `legacy_core.image_providers`.
- `ScriptIngestionService` -> `legacy_core.ingestion`.
- `ParagraphIntentService` -> `services.genai_client` + heuristics/helpers.
- `SettingsManager` -> `SettingsRepository` + `PresetRepository` + `SecretStore`.

## Вывод
Проект уже имеет понятную новую desktop-архитектуру, но внутри нее все еще заметны мосты в legacy-слой. Самые важные точки для будущих изменений: `ui/controller.py`, `pipeline/media.py`, `browser/session.py`, `providers/registry.py`, `pipeline/intents.py`, `storage/repositories.py`. Главная архитектурная особенность на сейчас: GUI-flow и legacy CLI-flow не совпадают по уровню зрелости и по использованию Gemini.
