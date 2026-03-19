# Технический аудит проекта `vid-img-downloader`

Дата аудита: 2026-03-14

Проверенные источники:

- runtime-код: `app/`, `browser/`, `config/`, `domain/`, `legacy_core/`, `pipeline/`, `providers/`, `release_tools/`, `services/`, `storage/`, `ui/`
- top-level скрипты: `keyword_extractor.py`, `image_fetcher.py`, `video_fetcher.py`
- документация: `docs/quick-start-ru.md`, `docs/phase-0/*`, `docs/phase-2/architecture.md`, `docs/phase-10/onboarding.md`, `docs/project-analysis.md`
- тесты: `tests/`
- запуск проверки: `python -m unittest discover -s tests` -> `Ran 96 tests in 40.656s`, `OK`
- smoke startup: `python -m app --smoke` -> приложение стартует, workspace и провайдеры поднимаются

Исключено из предмета анализа: `.venv/`, `dist/`, `workspace/`, `output/`, `recordings/`, `tmp_*` как runtime-артефакты, если отдельно не указано иное.

---

## 1. Краткое резюме проекта

Проект - это локальное desktop-приложение на Python для paragraph-first подбора медиа к нумерованному `.docx`-сценарию: импорт текста, построение intent/query bundle через Gemini, поиск ассетов в Storyblocks и бесплатных image providers, скачивание и сохранение результатов в локальный workspace.

Текущее состояние - **гибридный модульный монолит**:

- новая архитектура (`app` / `domain` / `pipeline` / `browser` / `providers` / `services` / `storage` / `ui`) уже выглядит как сервисно-репозиторный desktop-core;
- legacy-слой (`legacy_core/` + `keyword_extractor.py` + `image_fetcher.py` + `video_fetcher.py`) все еще остается частью фактической системы и используется новым кодом напрямую.

Сильные стороны:

- хорошие доменные модели и явные контракты (`domain/models.py`, `domain/project_modes.py`, `ui/contracts.py`);
- атомарная JSON-персистентность (`storage/serialization.py`);
- внятная документация по фазам и продуктовой терминологии (`docs/phase-0/*`, `docs/phase-2/architecture.md`, `docs/phase-10/onboarding.md`);
- неплохая unit/integration test база: 96 тестов прошли успешно;
- есть базовая observability-инфраструктура через `AppEvent`, `EventRecorder`, `JsonLineEventLogger`.

Главные проблемы:

1. **Фактическая производительность и масштабируемость ограничены самим wiring-ом runtime**: media-run оркестратор в runtime принудительно однопоточный, а UI-ручки concurrency вводят в заблуждение.
2. **Ключевые модули стали god objects**: `ui/controller.py` (2006 LOC), `pipeline/media.py` (1805 LOC), `pipeline/intents.py` (1261 LOC), `video_fetcher.py` (2037 LOC), `image_fetcher.py` (1547 LOC).
3. **Нарушены архитектурные границы**: UI-контроллер напрямую ходит в `application.container`, репозитории, pipeline internals и session manager.
4. **Персистентность run/manifest не масштабируется**: manifest целиком пересчитывается и переписывается после каждого абзаца, а UI регулярно перечитывает его целиком.
5. **Legacy/new split-brain** создает дублирование логики и риск расхождения поведения.
6. **Есть конкретные надежностные дефекты**: некорректная самовосстановительная логика при отсутствии manifest, отсутствие изоляции ошибок EventBus listeners, скрывающие ошибки fallback-и.
7. **Слабая engineering-обвязка**: нет root `README`, нет CI/CD, нет lint/type-check config, нет нормального `.gitignore` для артефактов workspace/браузерных профилей.

Общий вывод: **проект уже имеет хорошую основу для production-like desktop workflow, но сейчас его ограничивают не выбор технологий, а wiring, крупные модули, смешение слоев и I/O-модель manifest/workspace**.

---

## 2. Что это за проект и как он устроен

### Тип проекта

- Локальное desktop-приложение на Python под Windows-first сценарий.
- Архитектурно это не веб-сервис и не классический backend/frontend split.
- Внешние интеграции есть, но они вызываются локально из desktop runtime:
  - Gemini (`services/genai_client.py`)
  - Storyblocks через Playwright/native browser automation (`browser/*`)
  - free-image HTTP providers (`providers/images/*`, `legacy_core/image_providers.py`)

### Назначение продукта

По коду и документации (`docs/quick-start-ru.md`, `docs/phase-0/terminology.md`, `docs/phase-2/architecture.md`) продукт решает задачу подбора медиа к сценарным абзацам:

1. импортирует нумерованный `.docx`;
2. превращает каждый абзац в `ParagraphUnit`;
3. строит `ParagraphIntent` и `QueryBundle`;
4. создает `Run` и `RunManifest`;
5. ищет video/image кандидатов;
6. скачивает выбранные ассеты;
7. показывает live-progress, журнал событий и позволяет вручную закреплять/отклонять выбор.

### Основные модули и зоны ответственности

| Подсистема | Где | Ответственность |
| --- | --- | --- |
| Composition root | `app/bootstrap.py` | сборка контейнера зависимостей, workspace, registries, pipeline, browser session services |
| Runtime facade | `app/runtime.py` | high-level операции проекта и run |
| Domain model | `domain/models.py`, `domain/enums.py`, `domain/project_modes.py` | typed-модели, enums, режимы проекта, сериализация |
| Ingestion | `pipeline/ingestion.py`, `legacy_core/ingestion.py` | импорт и валидация нумерованного `.docx` |
| Intent extraction | `pipeline/intents.py`, `services/genai_client.py` | prompt, JSON parsing, эвристики, query bundle, Gemini integration |
| Media pipeline | `pipeline/media.py` | provider search, ranking, dedupe, downloads, manifest update, run service |
| Browser automation | `browser/session.py`, `browser/storyblocks.py`, `browser/storyblocks_backend.py`, `browser/downloads.py` | persistent browser session, Storyblocks auth health, search parsing, downloads |
| Provider layer | `providers/registry.py`, `providers/images/*` | registry провайдеров, query planning, filtering, SQLite cache, legacy adapters |
| Persistence | `storage/workspace.py`, `storage/repositories.py`, `storage/serialization.py` | workspace layout, JSON repositories, atomic writes |
| Cross-cutting services | `services/events.py`, `services/logging.py`, `services/secrets.py`, `services/settings_manager.py` | event bus, JSONL log, secret store, settings/preset management |
| UI | `ui/controller.py`, `ui/contracts.py`, `ui/qt_app.py`, `ui/tk_app.py` | view models, user actions, background run control, Qt/Tk presentation |
| Release tooling | `release_tools/portable.py` | portable bundle сборка |
| Legacy CLI | `keyword_extractor.py`, `image_fetcher.py`, `video_fetcher.py` | старые сценарии запуска и fallback технический слой |

### Используемый стек

`requirements.txt`:

- `google-genai` - Gemini SDK
- `playwright` - browser automation
- `PySide6` - основной desktop UI
- `python-docx` - ingest `.docx`
- `tenacity` - retry для Gemini
- `bing-image-urls` - opt-in generic image provider
- `Pillow` - legacy image validation path

Дополнительно по коду:

- stdlib `sqlite3` - provider cache
- stdlib `urllib` - HTTP layer в legacy provider adapters
- DPAPI через `ctypes` в `services/secrets.py`
- `tkinter` fallback UI
- системные `ffmpeg/ffprobe` в legacy `video_fetcher.py`

### Структура директорий

Observed top-level layout:

- source: `app/`, `browser/`, `config/`, `domain/`, `legacy_core/`, `pipeline/`, `providers/`, `release_tools/`, `services/`, `storage/`, `ui/`
- tests: `tests/`
- docs: `docs/`
- artifacts present in root: `dist/`, `workspace/`, `output/`, `recordings/`, `tmp_*`, `.venv/`

### Ключевые зависимости между подсистемами

```text
ui -> app.runtime -> app.bootstrap container
ui -> application.container.* (direct access in many places)

pipeline.ingestion -> legacy_core.ingestion
pipeline.intents -> services.genai_client + legacy_core.common
pipeline.media -> providers + browser + storage + services.events + legacy_core.network

providers.images.clients -> legacy_core.image_providers
providers.images.filtering -> legacy_core.licenses + legacy_core.query_utils

browser.storyblocks_backend -> browser.session + browser.storyblocks + browser.downloads
storage.repositories -> domain models
```

### Архитектурный стиль

Фактически проект сочетает несколько стилей:

- **модульный монолит** - высокий confidence;
- **service/repository layering** - высокий confidence;
- **частично clean/DDD-like** через `domain/`, `storage/`, `services/`, `pipeline/` - средний confidence;
- **не hexagonal в строгом смысле**, потому что UI напрямую обходит facade и лезет во внутренности container/repository/pipeline - высокий confidence;
- **migration architecture** между новым package-oriented core и legacy scripts - высокий confidence.

### Как организованы ключевые технические зоны

| Зона | Как сделано сейчас | Оценка |
| --- | --- | --- |
| Frontend | Desktop Qt (`ui/qt_app.py`) + Tk fallback (`ui/tk_app.py`) | функционально, но сильно продублировано |
| Backend | локальный Python runtime без отдельного сервера | соответствует desktop-продукту |
| API | прямые внешние интеграции: Gemini, Storyblocks browser automation, free image HTTP APIs | server-side API как отдельного слоя нет |
| State | `Project`, `Run`, `RunManifest`, in-memory `EventRecorder`, UI view models | модель понятная, но I/O-heavy |
| БД | нет полноценной БД; JSON workspace + SQLite caches | достаточно для MVP/desktop, плохо масштабируется в части manifest |
| Конфигурация | dataclass settings + `workspace/config/settings.json` + `SecretStore` | workable, но много ручного mapping |
| Сборка | portable bundle builder в `release_tools/portable.py` | release-oriented, не developer-oriented |
| Деплой | source bundle + `setup_portable.ps1/.bat` | нет воспроизводимой dependency-lock схемы |
| Тестирование | `unittest` suite в `tests/` | хорошее покрытие core-логики, слабее по UI parity и real integration |
| Логирование | `AppEvent` + `EventRecorder` + JSONL `workspace/logs/app.log` | хороший baseline, но без isolation/metrics |
| Ошибки | typed `AppError` hierarchy в `services/errors.py` | хороший baseline, но местами ошибки маскируются broad fallback-ами |

---

## 3. Анализ архитектуры

### Общая оценка

Архитектура в целом **подходит для desktop paragraph-first продукта**, но сейчас это **не завершенная layered architecture, а промежуточная миграционная система**, где новые границы модулей уже появились, а границы use-case слоя еще не доведены до конца.

Позитивные наблюдения:

- `domain/`, `storage/`, `services/`, `providers/` и `browser/` уже дают неплохую карту ответственности.
- `ui/contracts.py` хорошо задает explicit UI-view-model boundary.
- `docs/phase-0/terminology.md` и `docs/phase-0/mode-matrix.md` фиксируют продуктовые инварианты.

Ключевые архитектурные замечания:

| Проблема | Где проявляется | Почему это проблема | Последствия | Приоритет | Уверенность |
| --- | --- | --- | --- | --- | --- |
| UI слой пробивает application boundary | `ui/controller.py` напрямую использует `application.container.*`; в файле 99 прямых обращений к container | UI становится orchestrator-ом бизнеса, а не adapter-ом; слой use-case размыт | высокая связанность, сложно изолированно тестировать и безопасно менять runtime | высокий | высокая |
| Facade `DesktopApplication` слишком тонкий и не является реальной boundary | `app/runtime.py` покрывает только часть use cases, а UI идет в `project_repository`, `run_repository`, `media_pipeline`, `session_manager` напрямую | из-за этого архитектурные решения расползаются по UI-контроллеру | часть доменной логики живет в UI, возникают утечки абстракций | высокий | высокая |
| Два разных `RunOrchestrator` в composition root | `app/bootstrap.py`: один `RunOrchestrator(max_workers=1, queue_size=1)` передается в `ParagraphMediaRunService`, другой сохраняется как `container.orchestrator` c настройками из `settings` | runtime и тесты смотрят на разные orchestration-paths | настройки concurrency не применяются в реальном media run, тесты дают ложное чувство покрытия runtime wiring | критично | высокая |
| God modules | `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` | один файл решает слишком много задач: orchestration, persistence, formatting, business rules, background threading | высокий cognitive load, сложно локально рефакторить, AI и людям трудно безопасно вносить изменения | высокий | высокая |
| Split-brain между new core и legacy core | `pipeline/ingestion.py -> legacy_core.ingestion`; `providers/images/clients.py -> legacy_core.image_providers`; `providers/images/filtering.py -> legacy_core.query_utils`; root scripts дублируют поведение | source of truth распадается между новым и старым кодом | drift поведения, дублирование багов, рост стоимости изменений | высокий | высокая |
| Дублирование UI реализации | `ui/qt_app.py` и `ui/tk_app.py` реализуют почти те же сценарии разными путями; в Tk есть явное layout/feature drift внутри `_build_advanced_tab()` | любая feature/UI fix должна делаться минимум дважды | расхождение поведения fallback UI и основного UI, тестировать обе ветки сложно | средний | высокая |
| UI использует private internals pipeline | `ui/controller.py:1233-1269` обращается к `media_pipeline._image_backends` | это прямой abstraction leak | изменение internals pipeline ломает UI без компиляторных/типовых гарантий | высокий | высокая |

### Архитектурные антипаттерны

- **God Controller**: `ui/controller.py` одновременно управляет state assembly, background thread orchestration, settings persistence, session management, asset curation, preview generation, notifications и error translation.
- **Service Blob**: `pipeline/media.py` одновременно содержит backend registration, search orchestration, ranking, dedupe, download, manifest update, summary update и run service.
- **Migration Entanglement**: `legacy_core` формально legacy, но фактически часть runtime graph.
- **Leaky Abstractions**: UI знает о `ProjectRepository`, `RunRepository`, `ManifestRepository`, `BrowserSessionManager`, private `_image_backends` и details `MediaSelectionConfig`.

### Насколько архитектура масштабируема

- По числу подсистем - **умеренно да**.
- По скорости внесения изменений в ключевые workflow - **скорее нет**.
- По росту размеров manifest/run/workspace - **нет, текущая I/O-модель станет bottleneck**.
- По добавлению новых UI-фич - **дорого из-за dual UI и крупного controller**.
- По isolated testing модулей - **частично да** в domain/services/providers, **хуже** в UI/runtime wiring.

---

## 4. Анализ стека

### Насколько стек соответствует задаче

Для desktop paragraph-media tool стек в целом адекватен:

- Python хорошо подходит для glue-кода, automation и doc/media tooling.
- `PySide6` логичен как richer desktop UI.
- `Playwright` уместен для Storyblocks persistent-profile automation.
- `python-docx` уместен для ingest сценариев.
- `sqlite3` как cache layer для desktop MVP/production-lite тоже уместен.

Проблемы не в выборе базовых технологий, а в том, **как стек собран и обвязан процессами**.

### Замечания по стеку и техрешениям

| Наблюдение | Где | Почему это важно | Влияние | Уверенность |
| --- | --- | --- | --- | --- |
| `requirements.txt` содержит только нижние границы, без lockfile | `requirements.txt` | сборки не воспроизводимы, возможен dependency drift | стабильность релизов и CI ухудшается | высокая |
| Нет `pyproject.toml`, `ruff.toml`, `mypy.ini`, `.pre-commit-config.yaml`, `.github/workflows/*` | по инвентаризации файлов не найдено | нет единых quality gates и automation pipeline | DX и maintainability падают | высокая |
| Typesafety частично формальная | `browser/storyblocks_backend.py` (`search_adapter: Any`), `pipeline/media.py` (`session_manager: Any | None`), `services/genai_client.py` и др. | type hints есть, но ключевые integration points остаются динамическими | выше риск скрытых runtime ошибок | высокая |
| `GeminiModelAdapter` создает новый client на каждый `generate_content()` | `services/genai_client.py:34-72` | это лишний overhead и потеря control point для rate limiting/telemetry | perf/reliability ухудшаются на больших пакетах параграфов | высокая |
| Кэш провайдеров реализован на SQLite, но без TTL/eviction/versioning | `providers/images/caching.py` | stale results и бесконечный рост кэша со временем | reliability/perf debugging осложняются | высокая |
| Build/deploy ориентирован на portable source bundle, а не на reproducible developer workflow | `release_tools/portable.py` | удобно для ручной поставки, но не заменяет инженерный pipeline | средне-высокий DX debt | высокая |
| `Pillow` остается runtime dependency ради legacy image validation, но новый unified pipeline эту валидацию не использует | `requirements.txt`, `image_fetcher.py`, `pipeline/media.py` | зависимость есть, но новая архитектура не получает ее ценность | техдолг и регресс качества ассетов | высокая |

### Оценка по requested technical areas

| Зона | Состояние |
| --- | --- |
| Package/dependency management | `requirements.txt` без pinning/lockfile; dev dependencies и tooling не выделены |
| Build tooling | есть `release_tools/portable.py`, но нет единой dev build/test pipeline |
| Linting/formatting | конфигурации не найдены |
| Typesafety | базовые hints и dataclasses есть, статической проверки нет, `Any` много |
| State management | desktop state собран через JSON repositories + `EventRecorder` + UI view models; концептуально понятно, но тяжело по I/O |
| Data fetching | Storyblocks через browser automation, free providers через stdlib HTTP adapters; retry/validation неполные |
| ORM / DB access | ORM нет; JSON workspace + SQLite cache |
| Background jobs / queues | thread-based only (`BoundedExecutor`, `ThreadPoolExecutor`), без отдельной job infra |
| Caching | есть SQLite cache, но без lifecycle management |
| Observability | JSONL events и in-memory event journal есть; метрик/trace/profiling hooks нет |

### Что нельзя утверждать без допданных

- Актуальность версий зависимостей по внешнему release-cycle я не проверял.
- CVE-скан зависимостей не проводился.
- Конфликты бинарных версий (`PySide6`, `Playwright`, `Pillow`) без CI-матрицы не подтверждены.

---

## 5. Анализ качества кода

### Общая оценка

Кодовая база уже не выглядит хаотичной: видны слои, модели, naming mostly consistent, тесты не декоративные. Но на critical path накопилась **концентрация ответственности в слишком больших файлах**, и это уже стало системной проблемой качества.

### Замечания по качеству кода

| Проблема | Где | Почему это проблема | Последствия | Приоритет | Уверенность |
| --- | --- | --- | --- | --- | --- |
| Слишком большие файлы и функции | `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py`, `ui/qt_app.py`, `ui/tk_app.py`, `image_fetcher.py`, `video_fetcher.py` | сложно держать контекст, растет вероятность боковых эффектов и regression | код тяжелее ревьюить, тестировать и доверять AI-изменениям | высокий | высокая |
| Дублирование mapping/config logic | `storage/repositories.py` вручную сериализует settings; `ui/controller.py` вручную строит snapshot тех же полей | при добавлении новых настроек нужно менять несколько мест | config drift и тихие баги сохранения/загрузки | средний | высокая |
| Hidden side effects при открытии сценария | `ui/controller.py:325-348` -> `app/runtime.py:58-105` | import сценария может инициировать Gemini extraction и fallback без явной UI-коммуникации | пользователь получает разное поведение под одной кнопкой | высокий | высокая |
| Broad fallback-и маскируют реальные ошибки | `ui/__init__.py:19-25`, `app/runtime.py:80-100` | реальные runtime defects могут превращаться в silent degradation | поиск дефектов усложняется, поведение становится непредсказуемым | высокий | высокая |
| UI контроллер знает private state pipeline | `ui/controller.py:1233-1269` | нарушение инкапсуляции | любые внутренние refactor-ы pipeline рискованны | высокий | высокая |
| Legacy CLI scripts остаются огромными и partially overlapping | `image_fetcher.py`, `video_fetcher.py`, `keyword_extractor.py` | старый и новый пути живут параллельно | поддержка стоит дороже, сложно понять canonical path | высокий | высокая |

### Quick wins по качеству кода

- Разделить `ui/controller.py` минимум на `project_actions`, `run_actions`, `session_actions`, `settings_actions`, `state_builders`.
- Разделить `pipeline/media.py` минимум на `selection`, `downloads`, `manifest_updates`, `run_service`, `ranking/dedupe`.
- Вынести settings serialization/deserialization в единый serializer вместо ручного дубля в repository и UI snapshot builder.
- Убрать доступ UI к private `_image_backends`, дать публичный query-method уровня `DesktopApplication` или `MediaPipeline`.

### Что выглядит хорошо

- `domain/models.py` и `ui/contracts.py` задают понятные контракты данных.
- `storage/serialization.py` аккуратно реализует atomic write.
- `services/errors.py` вводит typed error taxonomy.
- `domain/project_modes.py` хорошо фиксирует бизнес-инварианты режимов.

---

## 6. Анализ производительности

### Важное замечание

Точных production-метрик в репозитории нет. Ниже - не выдуманные цифры, а **наблюдаемые bottleneck-гипотезы, подтверждаемые кодом**.

### Наиболее вероятные bottlenecks

| Bottleneck | Где | Почему это bottleneck | Последствия | Приоритет | Уверенность |
| --- | --- | --- | --- | --- | --- |
| Реальный media-run однопоточный | `app/bootstrap.py:157-169` | `ParagraphMediaRunService` получает `RunOrchestrator(max_workers=1, queue_size=1)` независимо от настроек | throughput ограничен одним абзацем за раз | критично | высокая |
| UI concurrency controls вводят в заблуждение | `ui/controller.py:1392-1401` + `pipeline/media.py` | `provider_workers`, `download_workers`, `relevance_workers` не становятся worker pools; они влияют только на candidate limits/diagnostics | пользователь/команда думают, что тюнят производительность, но почти не тюнят ее | критично | высокая |
| Provider search выполняется строго последовательно | `pipeline/media.py:1064-1109`, `1179-1203` | цикл идет backend -> query -> blocking search без parallel fanout | медленные внешние поиски суммируются линейно | высокий | высокая |
| Full manifest rewrite после каждого абзаца | `pipeline/media.py:454-456`, `719`, `742-766`, `1024-1062`; `storage/serialization.py:34-50` | каждый апдейт summary вызывает полную сериализацию и atomic rewrite manifest | I/O amplification, рост latency на больших run | критично | высокая |
| Full manifest read на UI polling | `ui/qt_app.py:64-67`, `446-464`; `ui/tk_app.py:64`; `ui/controller.py:148-212` | UI каждые 750ms перечитывает run/manifest и собирает live state | лишний CPU/I/O noise, особенно на больших manifest | высокий | высокая |
| Dedupe state пересчитывается с нуля для каждого абзаца | `pipeline/media.py:524-526`, `1513-1534` | `_build_deduper()` сканирует уже накопленный manifest каждый раз | квадратичный рост стоимости на длинных сценариях | высокий | высокая |
| SQLite cache открывает новое connection на каждый get/set | `providers/images/caching.py:48-128` | на каждый cache hit/miss идет `sqlite3.connect()` + close | лишний overhead на search-heavy runs | средний | высокая |
| Gemini intent extraction выполняется синхронно при import проекта | `ui/controller.py:325-348` -> `app/runtime.py:58-105` | импорт `.docx` может блокировать UI на время модели | медленный startup/UX freeze на больших документах | высокий | высокая |
| Gemini client не переиспользуется | `services/genai_client.py:34-72` | создание нового клиента на каждый вызов | лишний overhead и слабый control point для retry/rate limit | средний | высокая |
| Event logger открывает файл на каждое событие | `services/logging.py:9-17` | high-frequency events = high-frequency open/write/close | лишний I/O overhead | низкий | средняя |

### Дополнительный performance-риск

Новый free-image runtime path **не валидирует скачанные изображения по содержимому**:

- новый путь: `pipeline/media.py:199-243`
- старый путь с валидацией через Pillow: `image_fetcher.py:449-489`

Это скорее reliability/data-quality issue, но косвенно влияет и на perf: система может считать успешной загрузку мусорного payload, а затем тратить ресурсы на повторные ручные операции.

### Что и как профилировать дальше

Чтобы сделать аудит точнее, нужно собрать хотя бы такие метрики:

1. `time_to_import_project` с Gemini key и без него.
2. `time_per_paragraph` по этапам: provider search / download / persist.
3. median/p95 размера `manifest.json` и числа `paragraph_entries`.
4. сколько раз за run вызывается `save_manifest()` и каков суммарный объем записанных байт.
5. cache hit ratio по каждому image provider.
6. среднее число candidate queries на paragraph для Storyblocks/free image mode.
7. доля no-match / failed / manual-review paragraphs.

Практические profiling steps:

- обернуть таймингами `ParagraphMediaPipeline.process_paragraph()` и `save_manifest()`;
- логировать размер manifest перед записью;
- считать call count для `_build_deduper()`;
- добавить latency metrics на `StoryblocksCandidateSearchBackend.search()` и `ImageProviderSearchService.search_provider()`;
- отдельно измерить UI polling cost при run с 50/100/200 paragraphs.

---

## 7. Анализ масштабируемости и поддерживаемости

### Насколько легко добавлять новые фичи

Средняя оценка: **умеренно сложно**.

Что помогает:

- новые пакеты и явные domain models;
- provider registry и project modes;
- workspace model с отдельными repositories;
- неплохая тестовая база для core behavior.

Что мешает:

- UI-логика и use cases смешаны;
- `pipeline/media.py` слишком централизован;
- legacy/new paths удваивают точки изменения;
- dual UI делает каждую UI-фичу минимум двухкратной по стоимости.

### Основные барьеры для роста

| Барьер | Где | Почему мешает росту | Уверенность |
| --- | --- | --- | --- |
| Shared mutable manifest как live source of truth | `pipeline/media.py` | усложняет безопасную параллельность и быстрый incremental update | высокая |
| UI зависит от internals runtime | `ui/controller.py` | каждое изменение в core может требовать ручного перепрошива UI controller | высокая |
| Слабая canonical boundary между new и legacy | `legacy_core/*` + root scripts + new packages | команда тратит время на вопрос "какой путь теперь главный?" | высокая |
| Workspace на JSON-файлах без incremental index | `storage/repositories.py` | рост run/project history увеличивает read cost | средняя |
| Нет contributor guardrails | нет CI/lint/type gates | с ростом команды будет расти variability качества | высокая |

### Онбординг нового разработчика

Сейчас онбординг **скорее средний, чем хороший**:

- плюсы: много phase-docs, есть onboarding и quick start;
- минусы: нет root developer README, нет единого contributor flow, нет объяснения source-of-truth между legacy/new code, нет automation pipeline quality gates.

### Готовность к росту нагрузки

- Рост функциональности: **средняя**.
- Рост объема run/history/workspace: **ниже средней** из-за manifest I/O модели.
- Рост числа интеграций/провайдеров: **средняя**, если first-class provider API будет вынесен из legacy.
- Рост команды: **ниже средней**, пока не выправлены boundaries и tooling.

---

## 8. Анализ надежности и безопасности

### Позитивные наблюдения

| Что уже хорошо | Где | Комментарий |
| --- | --- | --- |
| Atomic JSON write | `storage/serialization.py` | снижает риск битых файлов при нормальной записи |
| Typed error taxonomy | `services/errors.py` | помогает нормализовать error handling |
| SSRF-like защита при внешних image downloads | `legacy_core/network.py:52-144` | есть проверка public host и safe redirects |
| Windows DPAPI для секретов | `services/secrets.py` | для win32 это правильный baseline |
| Browser session thread ownership guards | `browser/session.py` | снижает риск unsafe reuse Playwright handles |
| License filtering | `providers/images/filtering.py` + `legacy_core/licenses.py` | есть базовая защита от нежелательных лицензий |

### Основные замечания

| Проблема | Где | Почему это проблема | Последствия | Приоритет | Уверенность |
| --- | --- | --- | --- | --- | --- |
| Listener failures не изолированы от основного потока | `services/events.py:44-53` | ошибка логгера/recorder может подняться в рабочий pipeline | run может упасть из-за observability side effect | высокий | высокая |
| Некорректная логика при отсутствии manifest | `pipeline/media.py:1656-1663` | `execute(run_id)` создает новый run/manifest через `create_run()`, но продолжает выполнять старый run | несогласованность `run_id`, orphaned manifest, некорректный `active_run_id` у проекта | критично | высокая |
| Broad fallback на Tk скрывает ошибки Qt path | `ui/__init__.py:19-25` | fallback срабатывает на любой import/runtime error, а не только на отсутствие PySide6 | дефекты Qt UI могут silently mask-иться | высокий | высокая |
| Gemini failures при import сценария маскируются | `app/runtime.py:80-105` | исключение логируется warning-ом и silently заменяется heuristic bootstrap path | непредсказуемое качество import/result без явного уведомления пользователю | высокий | высокая |
| Free-image download path не валидирует payload как изображение | `pipeline/media.py:199-243` | HTML/error page/битый файл могут считаться успешной image download | некачественные или невалидные ассеты в manifest/output | высокий | высокая |
| Секреты обрабатываются двумя разными механизмами | desktop path: `services/secrets.py`; legacy CLI path: `.env` через `legacy_core/env.py` и root scripts | правила хранения ключей не едины | больше риск человеческой ошибки и утечек | средний | высокая |
| Non-Windows fallback для `SecretStore` = base64, не защита | `services/secrets.py:33-61` | это obfuscation, а не secure storage | для non-win окружений модель безопасности слабая | средний | высокая |
| Workspace/browser profile артефакты не игнорируются `.gitignore` | `.gitignore` содержит только `.env`, `__pycache__/`, `output/`; при этом root содержит `workspace/`, `dist/`, `.venv/`, `recordings/`, `tmp_*` | возможен случайный commit runtime data, browser profile data, caches, logs | высокий | высокая |

### Auth/Authz и доступ к данным

- Классических auth/authz слоев приложения как server-side системы здесь нет; это desktop single-user tool. Поэтому часть enterprise-пунктов неприменима.
- Реально чувствительная зона - Storyblocks session/browser profile data и API keys.
- Из-за локального `workspace/browser_profiles/` и `workspace/secrets/` особенно важно не тащить workspace в VCS и не смешивать dev/runtime данные в root.

### Устойчивость к сбоям

Сильные места:

- pause/resume/checkpoint model существует;
- JSON writes atomic;
- tests покрывают core run/session flows.

Слабые места:

- при проблемах observability listener-ов run может рухнуть;
- manifest storage и run storage слабо versioned;
- self-healing path при отсутствующем manifest дефектен;
- fallback-логика иногда скрывает реальные сбои.

---

## 9. Анализ DX

### Что хорошо

- Есть end-user документация и onboarding: `docs/quick-start-ru.md`, `docs/phase-10/onboarding.md`.
- Есть архитектурная и продуктовая документация: `docs/phase-0/*`, `docs/phase-2/architecture.md`.
- Тесты запускаются стандартной командой и сейчас проходят.

### Что плохо

| Наблюдение | Почему это DX-проблема | Уверенность |
| --- | --- | --- |
| Нет root `README.md` | новый разработчик не получает one-page карту проекта и quick dev start | высокая |
| Нет CI/CD config | качество не проверяется автоматически на ветке/PR | высокая |
| Нет lint/type-check/pre-commit tooling | стиль и ошибки ловятся поздно и вручную | высокая |
| `.gitignore` почти пустой | root быстро засоряется runtime артефактами и большими директориями | высокая |
| Документация фрагментирована по фазам | полезно для истории, но хуже для day-to-day contributor flow | средняя |
| `pytest` cache присутствует, но pytest config не найден | tooling signal mixed и неформализован | средняя |
| Не найден git repo / история коммитов недоступна | нельзя оценить коммит-стиль, branch policy, release discipline | высокая |

### Качество setup

Для пользователя release path описан.

Для разработчика не хватает явного ответа на вопросы:

- как создать dev env;
- чем запускать тесты/смоки/portable build;
- какой UI path считать основным при локальной разработке;
- какие папки считать source of truth и какие legacy modules трогать нельзя без миграции.

### Оценка тестовой инфраструктуры

- `unittest` suite полезный и не декоративный.
- Есть хорошие тесты для media pipeline, browser/session core, UI controller, architecture baseline.
- Но не найдено:
  - real CI execution;
  - UI parity tests для `qt_app.py` / `tk_app.py`;
  - performance regression tests;
  - contract tests на workspace migration/versioning.

---

## 10. Анализ пригодности проекта для AI-assisted development

### Насколько проект удобен для AI сейчас

Плюсы:

- package boundaries названы понятно: `browser`, `pipeline`, `storage`, `providers`, `ui`, `domain`;
- domain contracts явные;
- есть фазовая документация и glossary;
- бизнес-понятия (`Project`, `Run`, `ParagraphUnit`, `Mode`) формализованы.

Минусы:

- слишком большие файлы на critical path;
- новый и legacy код сосуществуют без жесткой canonical boundary;
- нет package-local README с краткими правилами подсистем;
- часть документации и UI-строк на русском, часть архитектурных описаний на английском;
- нет repo-level AI instructions/guardrails.

### Оценка по requested AI criteria

| Критерий | Оценка | Комментарий |
| --- | --- | --- |
| Понятность структуры для AI | средняя/хорошая | директории хорошие, но runtime path размазан между new и legacy |
| Явные архитектурные границы | средняя | формально есть, фактически UI и legacy их ломают |
| Контекст в именах файлов/папок | хорошая | naming mostly explicit |
| Размер файлов | плохая | 1.2k-2k LOC файлы мешают безопасному анализу |
| Неоднозначные зоны | высокая неоднозначность | `ui/controller.py`, `pipeline/media.py`, `legacy_core` vs new packages |
| Локальные README/ADR | недостаточно | есть phase-docs, но нет subsystem README/ADR map |
| Стабильные контракты | хорошие | `domain/models.py`, `ui/contracts.py`, `domain/project_modes.py` |

### Что стоит добавить в репозиторий для AI-разработки

1. `AGENTS.md` или `CONTRIBUTING.md` с правилами изменения слоев:
   - UI не ходит в repositories напрямую
   - `legacy_core` - только через adapter layers
   - новые use cases добавлять через application/service boundary
   - любые изменения `RunManifest` требуют update migration/tests
2. `docs/ai/context-map.md`:
   - source of truth по каждой подсистеме
   - список запрещенных shortcuts (`application.container.*` из UI, private field access)
3. `docs/ai/codegen-rules.md`:
   - куда добавлять новый provider
   - куда добавлять новый session action
   - как писать test coverage для pipeline/UI/controller
4. `README.md` в `pipeline/`, `browser/`, `ui/`, `providers/` с короткими rules-of-thumb.
5. ADR на ключевые решения:
   - paragraph-first model
   - почему Storyblocks идет через persistent profile
   - canonical migration plan away from `legacy_core`

### Полезные шаблоны промтов для этого репозитория

#### Анализ бага

```text
Ты работаешь в репозитории vid-img-downloader.
Задача: локализовать баг в подсистеме <module>.

Сначала:
1. Определи canonical path выполнения для этого сценария.
2. Явно отдели new-core путь от legacy path.
3. Найди, где создаются/читаются domain-модели и side effects.
4. Не предлагай фикс, пока не перечислишь наблюдаемые факты по коду и тестам.

Нужно вернуть:
- вероятную причину;
- конкретные файлы и методы;
- минимальный безопасный fix;
- какие тесты добавить/обновить.
```

#### Рефакторинг

```text
Ты рефакторишь код в vid-img-downloader.

Правила:
- не меняй продуктовое поведение без явного запроса;
- не усиливай связность между `ui`, `pipeline`, `storage`, `browser`;
- если трогаешь `legacy_core`, объясни почему новый слой не покрывает кейс;
- сначала выдели seams/interfaces, потом двигай логику.

Нужно вернуть:
- текущие responsibilities файла;
- target decomposition;
- пошаговый refactor plan;
- список regression tests.
```

#### Новая фича

```text
Добавь фичу в vid-img-downloader, сохраняя paragraph-first архитектуру.

Сначала определи:
- это UI feature, application use case, provider integration или persistence change;
- какие domain-модели/контракты меняются;
- есть ли риск затронуть legacy path.

Требования к результату:
- не ходить из UI напрямую в repository/pipeline internals;
- обновить тесты уровня controller/service;
- если меняется persisted payload, добавить версионирование или migration note.
```

#### Написание тестов

```text
Напиши тесты для изменения в vid-img-downloader.

Сначала:
- перечисли affected execution paths;
- раздели unit tests, integration-like tests и UI-controller tests;
- используй существующий стиль `unittest`.

Нужно покрыть:
- happy path;
- failure path;
- checkpoint/resume/cancel, если код касается run lifecycle;
- regression case на найденный баг.
```

#### Оптимизация производительности

```text
Оптимизируй производительность в vid-img-downloader без потери надежности.

Сначала:
1. Найди реальные I/O и serialization hotspots.
2. Проверь, не является ли bottleneck wiring/config bug.
3. Отдельно оцени manifest writes, UI polling, provider search fanout и cache access.

Верни:
- конкретный bottleneck;
- измеримый план проверки;
- safe optimization with rollback strategy;
- какие метрики добавить.
```

#### Архитектурное ревью

```text
Проведи архитектурное ревью vid-img-downloader как desktop production-like системы.

Проверь:
- не лезет ли UI в internals runtime;
- есть ли duplicate source of truth между new-core и legacy;
- не стал ли модуль god object;
- насколько change isolated и testable.

Формат:
- проблема;
- где;
- почему;
- последствия;
- priority;
- safe remediation path.
```

---

## 11. Список основных проблем

1. **Runtime concurrency wiring сломан концептуально** - реальные media runs всегда однопоточные, несмотря на настройки и UI.
2. **`ui/controller.py` стал god object и нарушает layering**, напрямую управляя internals container/repositories/pipeline.
3. **`pipeline/media.py` одновременно orchestrator, selector, downloader, persistence layer и event publisher**.
4. **Manifest persistence не масштабируется**: full summary recompute + full-file rewrite после каждого абзаца.
5. **Legacy/new split-brain** затрудняет сопровождение и ведет к drift поведения.
6. **Есть конкретный дефект в `ParagraphMediaRunService.execute()` при отсутствующем manifest**.
7. **EventBus listener failures могут ломать основной workflow**.
8. **Новый free-image pipeline потерял валидацию payload, которая была в legacy path**.
9. **Широкие fallback-и скрывают реальные ошибки** (`Qt -> Tk`, `Gemini -> heuristics`).
10. **DX-инфраструктура слабая**: нет CI, lint/type gates, root README и нормального `.gitignore`.

---

## 12. Quick wins

| Quick win | Где | Почему это быстро | Эффект | Уверенность |
| --- | --- | --- | --- | --- |
| Подать в `ParagraphMediaRunService` тот же orchestrator, что лежит в container settings path | `app/bootstrap.py` | локальный wiring fix | сразу снимает самый грубый runtime/perf mismatch | высокая |
| Исправить broken recovery при отсутствии manifest | `pipeline/media.py` | локальный bug fix | убирает риск несогласованных run/manifest | высокая |
| Изолировать listener exceptions в `EventBus.publish()` | `services/events.py` | локальный fix | повышает надежность без большой переделки | высокая |
| Расширить `.gitignore` | `.gitignore` | один файл | снижает риск утечек и мусора в репозитории | высокая |
| Убрать broad `except Exception` при выборе Qt/Tk fallback | `ui/__init__.py` | локальный fix | перестанут маскироваться реальные Qt defects | высокая |
| Добавить root `README.md` с dev start и source-of-truth map | root | документационный fix | ускорит onboarding и AI navigation | высокая |
| Добавить validation скачанных free images через Pillow в unified pipeline | `pipeline/media.py` | локальный/runtime fix | резко улучшит качество и надежность ассетов | высокая |
| Ясно переименовать advanced knobs или связать их с реальной concurrency | `ui/controller.py`, `ui/*_app.py` | mostly UI/config fix | убирает misleading UX для команды и пользователя | высокая |

---

## 13. Системные проблемы

### 1. Незавершенная миграция архитектуры

Суть:

- `legacy_core` номинально legacy, но по факту остается частью runtime graph.
- new-core еще не стал единственным source of truth.

Проявления:

- `pipeline/ingestion.py` использует `legacy_core.ingestion`;
- provider/image filtering/query planning тянут legacy helpers;
- root scripts продолжают жить и тестироваться.

Последствие:

- каждый крупный change нужно ментально проверять в двух мирах.

### 2. Слишком крупные точки изменения

Суть:

- несколько файлов аккумулировали слишком много ответственности.

Последствие:

- стоимость любого безопасного изменения непропорционально растет;
- AI-assisted development получает слишком широкий context window и чаще ошибается.

### 3. Persistence используется и как storage, и как live progress bus

Суть:

- manifest одновременно является persisted state, live progress source и data source для UI polling.

Последствие:

- лишние full rewrites/full reads становятся нормой;
- улучшение производительности упирается в формат хранения, а не только в threading.

### 4. Отсутствуют engineering guardrails

Суть:

- нет CI/lint/type/pre-commit pipeline;
- нет contributor contract;
- `.gitignore` не покрывает реальные артефакты проекта.

Последствие:

- архитектурный долг будет расти быстрее команды.

---

## 14. Детальный roadmap оптимизации по приоритетам

### A. Критично

#### A1. Починить runtime orchestration wiring

- Проблема: runtime media runs идут через отдельный однопоточный `RunOrchestrator`, не через настроенный orchestrator контейнера.
- Где: `app/bootstrap.py`, косвенно `tests/test_phase2_architecture.py`.
- Влияние: false performance ceiling, misleading settings, тесты не отражают реальный runtime path.
- Сложность: низкая/средняя.
- Ожидаемый эффект: сразу корректно заработают `paragraph_workers` и `queue_size`; станет возможна честная perf-настройка.
- Что делать practically:
  1. передавать в `ParagraphMediaRunService` уже созданный orchestrator из container;
  2. удалить дублирующее создание второго orchestrator;
  3. добавить тест именно на runtime media_run_service wiring, а не только на `container.orchestrator`.
- Тип исправления: локальный fix + небольшой refactor.

#### A2. Переделать модель персистентности run/manifest

- Проблема: manifest целиком переписывается после каждого абзаца, summary пересчитывается полным проходом, UI читает его целиком по polling.
- Где: `pipeline/media.py`, `storage/serialization.py`, `ui/controller.py`, `ui/qt_app.py`, `ui/tk_app.py`.
- Влияние: I/O bottleneck, плохая масштабируемость на длинных сценариях, замедление UI.
- Сложность: средняя/высокая.
- Ожидаемый эффект: заметное ускорение больших runs и уменьшение write amplification.
- Что делать practically:
  1. отделить live progress state от final manifest;
  2. обновлять summary incrementally;
  3. либо хранить paragraph-level entries отдельно, либо перевести run state на SQLite/local DB table;
  4. UI live polling читать только компактный progress snapshot.
- Тип исправления: refactor, местами перепроектирование.

#### A3. Убрать дефектную recovery-логику и повысить надежность event pipeline

- Проблема: `execute(run_id)` при отсутствующем manifest создает новый run/manifest, а `EventBus` не изолирует listener failures.
- Где: `pipeline/media.py`, `services/events.py`.
- Влияние: риск неконсистентного состояния run и падения workflow из-за logging side effect.
- Сложность: низкая/средняя.
- Ожидаемый эффект: выше предсказуемость runtime и легче incident handling.
- Что делать practically:
  1. в `execute()` либо явно fail fast при отсутствующем manifest, либо создавать manifest для того же `run_id`, не новый run;
  2. обернуть listeners в safe publish layer c error capture/logging;
  3. добавить regression tests на missing manifest и failing event listener.
- Тип исправления: локальный fix.

#### A4. Вернуть валидацию ассетов и закрыть workspace hygiene/security gap

- Проблема: unified free-image path не валидирует payload как реальное изображение, а runtime/browser/workspace артефакты не игнорируются VCS.
- Где: `pipeline/media.py`, `.gitignore`, `workspace/`, `browser/profile_import.py`.
- Влияние: риск мусорных ассетов, риск случайной утечки browser profile/session data.
- Сложность: низкая/средняя.
- Ожидаемый эффект: выше качество output и ниже operational/security risk.
- Что делать practically:
  1. reuse Pillow-based validation/normalization из legacy path или вынести новый shared validator;
  2. добавить в `.gitignore`: `.venv/`, `dist/`, `workspace/`, `recordings/`, `tmp_*/`, `.pytest_cache/`, `.ruff_cache/`;
  3. документировать, что browser profiles и workspace не должны попадать в VCS.
- Тип исправления: локальный fix.

### B. Высокий приоритет

#### B1. Ввести реальную application service boundary

- Проблема: UI контроллер напрямую ходит в internals container.
- Где: `ui/controller.py`, `app/runtime.py`.
- Влияние: высокая связанность и архитектурный drift.
- Сложность: средняя.
- Ожидаемый эффект: проще тестировать, рефакторить и добавлять use cases.
- Что делать practically:
  1. расширить `DesktopApplication` до реального use-case API;
  2. вынести `project`, `run`, `session`, `settings` operations в отдельные application services;
  3. запретить новые `application.container.*` access из UI.
- Тип исправления: refactor.

#### B2. Разбить god modules на smaller stable units

- Проблема: `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` стали too large.
- Где: перечисленные файлы.
- Влияние: высокий cognitive load, сложный review, плохая AI-аналитика.
- Сложность: средняя/высокая.
- Ожидаемый эффект: лучше test isolation, выше predictability, ниже regression risk.
- Что делать practically:
  1. в `pipeline/media.py` выделить `selection_service`, `download_service`, `manifest_service`, `run_service`, `ranking/dedupe`;
  2. в `pipeline/intents.py` выделить `prompt_builder`, `response_parser`, `query_builder`, `document_executor`;
  3. в `ui/controller.py` разнести actions/builders по отдельным модулям.
- Тип исправления: refactor.

#### B3. Завершить границы миграции new-core vs legacy-core

- Проблема: legacy слой остается рабочей dependency-цепочкой нового runtime.
- Где: `pipeline/ingestion.py`, `providers/images/*`, root scripts.
- Влияние: drift поведения и дорогая поддержка.
- Сложность: высокая.
- Ожидаемый эффект: один canonical path, понятнее architecture и AI navigation.
- Что делать practically:
  1. составить список legacy функций, все еще используемых новым runtime;
  2. решением за решением переносить их в `providers/`, `pipeline/`, `services/`;
  3. помечать legacy entrypoints как compatibility-only и постепенно thin-wrapper-ить их поверх нового core.
- Тип исправления: refactor/частичная перепись.

#### B4. Сделать import сценария неблокирующим и предсказуемым

- Проблема: `open_script()` может синхронно запускать Gemini extraction.
- Где: `ui/controller.py`, `app/runtime.py`.
- Влияние: UI freeze и неочевидное поведение.
- Сложность: средняя.
- Ожидаемый эффект: лучше perceived performance и DX пользователя.
- Что делать practically:
  1. на import делать только ingestion + heuristic bootstrap;
  2. AI enrichment запускать background task-ом по кнопке или lazy-on-run;
  3. явно показывать пользователю, был ли использован Gemini или fallback heuristics.
- Тип исправления: refactor.

#### B5. Ввести базовые engineering gates

- Проблема: нет CI/lint/type/pre-commit.
- Где: repo-level.
- Влияние: растет вероятность архитектурного и качественного расползания.
- Сложность: низкая/средняя.
- Ожидаемый эффект: быстрее обратная связь и меньше регрессий.
- Что делать practically:
  1. добавить `pyproject.toml` или отдельные config files;
  2. выбрать минимум: `ruff`, `pytest` или оставить `unittest` + runner script, `mypy/pyright` на core modules;
  3. завести CI на `tests`, lint и smoke startup.
- Тип исправления: локальный process/tooling fix.

### C. Средний приоритет

#### C1. Нормализовать cache lifecycle

- Проблема: provider cache без TTL/eviction/versioning и с connection churn.
- Где: `providers/images/caching.py`, `providers/images/service.py`.
- Влияние: stale results, лишний overhead, трудные debugging scenarios.
- Сложность: средняя.
- Ожидаемый эффект: стабильнее поведение и предсказуемее perf.
- Что делать practically:
  1. добавить timestamps/version columns;
  2. ввести TTL/cleanup command;
  3. держать reuse connection или connection pool на жизненный цикл сервиса.
- Тип исправления: refactor.

#### C2. Добавить schema versioning и migration policy для workspace data

- Проблема: `Project` / `Run` / `RunManifest` / browser profile JSON живут без явной версии схемы.
- Где: `domain/models.py`, `storage/repositories.py`.
- Влияние: upgrade risk при изменении persisted fields.
- Сложность: средняя.
- Ожидаемый эффект: безопаснее эволюция форматов.
- Что делать practically:
  1. добавить `schema_version` в persisted root entities;
  2. завести migration helpers;
  3. покрыть старые/новые payload shapes тестами.
- Тип исправления: refactor.

#### C3. Сократить UI duplication

- Проблема: Qt и Tk реализации поддерживаются параллельно и уже расходятся.
- Где: `ui/qt_app.py`, `ui/tk_app.py`, `ui/__init__.py`.
- Влияние: двойная стоимость UI support.
- Сложность: средняя.
- Ожидаемый эффект: проще сопровождать UI.
- Что делать practically:
  1. либо объявить Qt единственным поддерживаемым path;
  2. либо вынести общий declarative state binding layer, а Tk оставить thin fallback;
  3. ограничить fallback только случаем реально отсутствующего PySide6, а не любого exception.
- Тип исправления: refactor/локальное упрощение.

#### C4. Расширить observability до метрик и профилирования

- Проблема: сейчас есть только events/logs, но нет latency/size metrics.
- Где: `services/events.py`, `pipeline/media.py`, `providers/images/service.py`, `browser/storyblocks_backend.py`.
- Влияние: сложно предметно оптимизировать.
- Сложность: средняя.
- Ожидаемый эффект: performance work станет data-driven.
- Что делать practically:
  1. логировать duration per provider/paragraph;
  2. писать размер manifest и count записей;
  3. агрегировать cache hit ratio.
- Тип исправления: локальный refactor.

#### C5. Убрать ручное дублирование settings serialization

- Проблема: settings mapping размазан между repository и UI snapshot builder.
- Где: `storage/repositories.py`, `ui/controller.py`.
- Влияние: конфигурационный drift.
- Сложность: средняя.
- Ожидаемый эффект: меньше багов при расширении настроек.
- Что делать practically:
  1. сделать единый serializer/deserializer для `ApplicationSettings`;
  2. reuse его в repository, preset export/import и UI snapshot.
- Тип исправления: refactor.

### D. Низкий приоритет / позже

#### D1. Буферизовать JSONL event logging

- Проблема: open/write/close на каждое событие.
- Где: `services/logging.py`.
- Влияние: небольшой, но постоянный I/O overhead.
- Сложность: низкая.
- Ожидаемый эффект: чуть меньше I/O шума.
- Что делать practically: buffered writer или dedicated logging queue/thread.
- Тип исправления: локальный fix.

#### D2. Привести язык документации и UI guidance к более явной bilingual strategy

- Проблема: docs и код mixed RU/EN, что мешает части команды и AI.
- Где: `docs/*`, `ui/*`, phase docs.
- Влияние: средний knowledge friction.
- Сложность: низкая/средняя.
- Ожидаемый эффект: легче искать контекст и писать промты/ревью.
- Что делать practically: glossary-first bilingual docs, unified naming guide.
- Тип исправления: документационный refactor.

#### D3. Оценить перевод run/project store с JSON на SQLite после стабилизации core

- Проблема: JSON workspace удобен, но не идеален для growth.
- Где: `storage/*`, `pipeline/media.py`.
- Влияние: сейчас не блокирует MVP, но может быть полезно позже.
- Сложность: высокая.
- Ожидаемый эффект: лучше query/update characteristics при росте истории.
- Что делать practically: рассматривать только после cleanup layering и manifest model.
- Тип исправления: возможная будущая перепись persistence.

---

## 15. Какие данные еще нужны для более точного аудита

Чтобы перейти от качественного аудита к прицельному performance/reliability plan, не хватает:

1. **Production-like workload данных**
   - среднее/максимальное число абзацев в одном сценарии;
   - средний размер `manifest.json` и `project.json`;
   - доля Storyblocks-only vs free-image-only runs.

2. **Реальных performance метрик**
   - время import проекта;
   - время run на 10 / 50 / 100 / 200 абзацев;
   - latency по провайдерам;
   - cache hit ratio.

3. **Операционных данных**
   - реальные логи неуспешных runs;
   - частые user-facing ошибки;
   - support feedback по session/login flow.

4. **Инженерного контекста**
   - какой путь считается canonical: desktop only или CLI scripts тоже product-supported;
   - план по deprecation `legacy_core`;
   - target team size и expected development velocity.

5. **Безопасностного контекста**
   - допустимо ли хранить browser profile/session data локально;
   - есть ли требования по шифрованию не только на Windows;
   - нужен ли formal secret management policy.

6. **Процессного контекста**
   - git history / issue history / release cadence;
   - CI logs;
   - crash reports и frequency инцидентов.

Без этих данных можно уверенно делать структурный и кодовый рефакторинг, но нельзя честно обещать количественный performance uplift в процентах.
