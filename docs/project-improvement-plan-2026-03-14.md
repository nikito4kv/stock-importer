# План улучшения проекта `vid-img-downloader`

Основание: `docs/technical-audit-2026-03-14.md` и список проблем/запросов заказчика от 2026-03-14.

Статус выводов:
- подтверждено аудитом и кодом - помечено как `подтверждено`;
- требует дополнительных логов/замеров - помечено как `гипотеза`.

## 1. Executive summary

Проект не нуждается в полном переписывании. Ему нужен жесткий, поэтапный курс на стабилизацию runtime, устранение ложных настроек и разрывов слоев, а уже затем - на структурный рефакторинг и оптимизацию I/O.

Главный практический вывод: сейчас основной риск не в технологиях, а в том, как собран runtime. Самая дорогая проблема для бизнеса - зависание при открытии/старте сценария. Это хорошо согласуется с подтвержденным кодом: `ui/controller.py` синхронно вызывает `application.create_project()`, а `app/runtime.py` при импорте может запускать Gemini-extraction по всему документу на UI-пути.

Рекомендуемая стратегия:
- сначала убрать блокировки, непредсказуемые остановки и скрывающие ошибки fallback-и;
- затем выровнять application boundary и разрезать god-модули;
- потом перепроектировать live progress/persistence, чтобы снять I/O bottleneck без rewrite всего продукта;
- параллельно ввести инженерные guardrails, чтобы не наращивать новый долг;
- только после этого идти в долгосрочные улучшения вроде очереди сценариев и возможной эволюции persistence.

Крупные инициативы:

| Инициатива | Expected impact | Implementation cost | Почему это первая/не первая очередь |
| --- | --- | --- | --- |
| Стабилизация `open/start` пути и run lifecycle | Очень высокий | Низкий-средний | Это главный user-facing pain и основной блокер для доверия к продукту |
| Исправление runtime wiring, manifest recovery и safe event handling | Очень высокий | Низкий | Дает быстрый прирост надежности без тяжелой переделки |
| Выделение real application boundary и декомпозиция god-модулей | Высокий стратегический | Средний-высокий | Снижает стоимость всех следующих изменений |
| Разделение live progress и persisted manifest | Очень высокий | Высокий | Это главный performance/scalability узкий участок, но его опасно делать до стабилизации |
| DX/AI guardrails (`README`, CI, lint, typed checks, docs/ai) | Высокий | Низкий-средний | Это ускорит дальнейшую разработку и снизит вероятность новых регрессий |

## 2. Ключевые выводы из аудита

### Карта проблем по инженерным категориям

- Runtime/UX: `open_script()` и import-path блокируют UI; media-run фактически однопоточный; provider search и no-match path ведут себя как линейная очередь; advanced concurrency controls частично вводят в заблуждение.
- Архитектура: `ui/controller.py` пробивает application boundary, `DesktopApplication` слишком тонкий, `pipeline/media.py` и `pipeline/intents.py` стали сервисными blob-модулями, `legacy_core` остается частью реального runtime graph.
- Persistence/I/O: manifest используется одновременно как итоговое хранилище, live-progress source и polling source для UI; отсюда full rewrite/full read и лишний dedupe rebuild.
- Reliability: дефект recovery при отсутствующем manifest, `EventBus` не изолирует listener failures, широкие fallback-и скрывают реальные сбои, новый image-path потерял валидацию payload.
- Security/operational hygiene: секреты обрабатываются двумя путями, non-Windows fallback не является реальной защитой, `.gitignore` не защищает workspace/browser profiles/logs.
- DX/AI: нет root `README.md`, CI, lint/type gates, package-local README, AI guardrails и четкой canonical map между new-core и legacy.

### Как замечания заказчика ложатся на аудит

| Замечание заказчика | Что говорит аудит/код | Статус | Вывод |
| --- | --- | --- | --- |
| 1. Открытие сценария и старт иногда приводят к 10-минутному зависанию | `ui/controller.py` вызывает `application.create_project()`, а `app/runtime.py` при импорте может синхронно прогонять Gemini по документу; manifest/UI polling тоже усиливают тормоза | Подтверждено | Это P0 инцидент; импорт сценария нельзя оставлять на UI-пути с синхронным AI |
| 2. Программа часто глючит | Аудит подтверждает системные reliability-дефекты: missing manifest recovery, listener failures, broad fallback-и, слабые guardrails | Частично подтверждено | Нужно не «чинить по симптомам», а стабилизировать lifecycle и error surfacing |
| 3. Иногда Storyblocks пишет «неверные данные» | В аудите нет прямого root cause; вероятны stale session/profile state, health-check drift или скрытый fallback | Гипотеза | Нужны auth/session diagnostics, явная причина статуса и безопасный reset flow |
| 4. Нужен prompt input + опциональная отправка всего сценария в Gemini | Текущий prompt builder в `pipeline/intents.py` работает по абзацу; feature отсутствует | Подтверждено | Делать как отдельный use case, не на import-path, с token budget, feature flag и явным UX |
| 5. Нужна настройка количества видео/картинок по сервису | `ui/controller.py` хардкодит `supporting_image_limit=1` и `fallback_image_limit=1`; текущие worker knobs не являются media-count knobs | Подтверждено | Нужен отдельный selection contract, а не переиспользование технических concurrency настроек |
| 6. Нужен старт с определенного абзаца | В runtime уже есть `selected_paragraphs` и rerun selected paragraphs | Подтверждено | Это хороший quick win: в основном нужен явный UX и range selector |
| 7. Очень долго обрабатывается ошибка поиска | Provider search идет последовательно, no-match path не ограничен явным budget/timebox | Подтверждено | Нужен fast-fail/no-match budget и телеметрия по latency провайдеров |
| 8. Иногда генерация останавливается посреди сценария | Это совместимо с listener failures, missing manifest recovery, скрытыми fallback-ами и слабой диагностикой | Частично подтверждено | Нужны incident logs, run-stop reason, checkpoint hardening и regression tests |
| 9. Нужна очередь сценариев | В проекте есть paragraph queue внутри orchestrator, но нет app-level queue для нескольких сценариев | Подтверждено | Делать только после выделения application service boundary |
| Вопрос: почему при всех image services сохраняется одна картинка? | Сейчас выбираются не «файлы от каждого сервиса», а победившие asset-ы по strategy; `supporting_image_limit` и `fallback_image_limit` сейчас по умолчанию = 1; при `mixed_image_fallback=True` Storyblocks идет primary, free providers - fallback | Подтверждено | Надо явно документировать текущую логику и вынести counts в продуктовую настройку |

## 3. Что в проекте уже хорошо

- `domain/models.py`, `domain/project_modes.py` и `ui/contracts.py` уже дают хорошую карту бизнес-сущностей; это надо сохранить как основу эволюции.
- `storage/serialization.py` с atomic write - правильный baseline для desktop persistence; ломать его без новой модели данных не нужно.
- `providers/registry.py` и режимы проекта дают адекватную основу для расширения provider layer без микросервисного overengineering.
- Тестовая база (`python -m unittest discover -s tests`) не декоративная; это хороший фундамент для безопасного рефакторинга.
- `services/errors.py`, `AppEvent`, `EventRecorder`, `JsonLineEventLogger` уже дают правильные семена для typed error handling и observability.
- `services/secrets.py` на Windows и сетевые ограничения в `legacy_core/network.py` - хорошие security baselines; их нужно развивать, а не заменять модной новой подсистемой.

Что не нужно ломать без причины:
- paragraph-first модель;
- provider registry;
- pause/resume/checkpoint как продуктовый паттерн;
- workspace-подход как локальную desktop abstraction;
- существующий тестовый стиль на `unittest`, пока нет веской причины миграции.

## 4. Критические проблемы

| Проблема | Severity | Влияние на продукт | Влияние на скорость разработки | Влияние на стабильность/масштабируемость | Риск изменения | Cost | Expected return |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Блокирующий `open/start` путь с синхронным Gemini | P0 | Очень высокое | Высокое | Высокое | Средний | Средний | Очень высокий |
| Runtime orchestration wiring не соответствует настройкам | P0 | Высокое | Среднее | Очень высокое | Низкий | Низкий | Очень высокий |
| Некорректная recovery-логика при отсутствующем manifest | P0 | Высокое | Среднее | Очень высокое | Низкий | Низкий | Очень высокий |
| Listener failures могут уронить рабочий pipeline | P0 | Высокое | Среднее | Очень высокое | Низкий | Низкий | Очень высокий |
| Full manifest rewrite/read как core runtime pattern | P1 | Среднее-высокое | Среднее | Очень высокое | Средний-высокий | Высокий | Высокий |
| `ui/controller.py` и `pipeline/media.py` как god-модули | P1 | Среднее | Очень высокое | Высокое | Средний | Средний-высокий | Высокий стратегический |
| Broad fallback-и маскируют реальные ошибки (`Qt -> Tk`, `Gemini -> heuristics`) | P1 | Высокое | Среднее | Высокое | Низкий | Низкий | Высокий |
| Нет payload validation для unified free-image download path | P1 | Среднее-высокое | Низкое | Высокое | Низкий | Низкий | Высокий |
| Storyblocks auth/session failure плохо диагностируется | P1 | Высокое | Среднее | Среднее | Низкий-средний | Средний | Высокий |
| Legacy/new split-brain и отсутствие guardrails | P1 | Среднее | Очень высокое | Высокое | Средний | Средний-высокий | Высокий стратегический |

## 5. Quick wins

| Quick win | Что сделать | Expected impact | Implementation cost | Как проверить |
| --- | --- | --- | --- | --- |
| Убрать Gemini с синхронного import-path | Импортировать документ без AI, а enrichment запускать отдельной задачей | Очень высокий | Средний | Время открытия сценария становится предсказуемым; UI не зависает |
| Починить orchestrator wiring | Передавать в `ParagraphMediaRunService` тот же orchestrator, что настроен в container | Высокий | Низкий | Настройки `paragraph_workers`/`queue_size` реально влияют на run |
| Исправить missing manifest recovery | Fail-fast или восстанавливать manifest для того же `run_id`, а не создавать новый run | Высокий | Низкий | Regression test на отсутствующий manifest проходит |
| Изолировать listener exceptions | Safe publish wrapper в `services/events.py` | Высокий | Низкий | Ошибка логгера больше не валит run |
| Сузить `Qt -> Tk` fallback | Fallback только если нет `PySide6`, не на любой `Exception` | Средний-высокий | Низкий | Qt-баги становятся видимыми, а не маскируются |
| Вернуть image payload validation | Переиспользовать Pillow-проверку или общий validator | Высокий | Низкий-средний | Битые/HTML payload-ы не попадают в output |
| Добавить Storyblocks auth diagnostics | Явно логировать тип ошибки: cookie expired / auth wall / network / selector drift | Высокий | Низкий-средний | Ошибка «неверные данные» становится объяснимой |
| Ввести no-match timeout/budget | Ограничить долгие линейные поиски и быстрее surfacing failure | Высокий | Средний | Время до no-match существенно сокращается |
| Экспонировать start-from-paragraph через UI | Использовать уже существующий `selected_paragraphs` | Средний-высокий | Низкий | Можно запускать диапазон `N..end` без архитектурной переделки |
| Документировать текущую image selection semantics | Явно объяснить, что «все сервисы» != «по одному файлу с каждого сервиса» | Средний | Низкий | Уменьшается путаница у пользователя и в support |

## 6. Системные проблемы

- Незавершенная миграция: `legacy_core` формально legacy, но фактически остается частью production path. Пока нет canonical boundary, любое изменение требует проверки двух миров.
- Разрыв между UI и runtime semantics: часть UI-настроек выглядит продуктовой, но влияет лишь на внутренние лимиты/диагностику, а не на реальную concurrency или selection policy.
- Manifest как универсальный объект: он одновременно служит persisted state, live progress bus и read model для UI. Это и есть главный структурный источник I/O amplification.
- Слишком крупные модули на critical path: они делают локальные правки дорогими, повышают regression risk и ухудшают AI-assisted development.
- Скрытые деградации вместо явных ошибок: broad fallback-и и silent heuristics-path делают поведение непредсказуемым и мешают support/debug.
- Нет инженерных guardrails: без CI/lint/type gates архитектурный долг будет расти быстрее любой команды.

## 7. Архитектурные улучшения

### Рекомендуемые архитектурные изменения

| Улучшение | Практический подход | Expected impact | Implementation cost | Trade-offs |
| --- | --- | --- | --- | --- |
| Ввести реальную application boundary | Расширить `DesktopApplication`/application services до полного use-case API и запретить прямые container/repository calls из UI | Очень высокий | Средний | Потребуется серия небольших refactor PR, а не один большой |
| Декомпозировать `ui/controller.py` | Разделить на `project_actions`, `run_actions`, `session_actions`, `settings_actions`, `state_builders` | Высокий | Средний | Нужны controller-level regression tests до переноса |
| Декомпозировать `pipeline/media.py` | Выделить `selection_service`, `download_service`, `manifest_service`, `run_service`, `ranking/dedupe` | Очень высокий | Средний-высокий | Без предварительных seam-tests риск регрессий заметный |
| Декомпозировать `pipeline/intents.py` | Выделить `prompt_builder`, `response_parser`, `query_builder`, `document_executor` | Высокий | Средний | Позволит безопасно добавить prompt/context feature |
| Формализовать legacy adapters | Оставить `legacy_core` только за adapter boundary, постепенно thin-wrapper-ить старые скрипты поверх нового core | Высокий стратегический | Высокий | Медленнее, чем быстрый rewrite, но намного безопаснее |
| Определить политику по Tk fallback | Либо Qt-only support, либо thin fallback без parity promise | Средний-высокий | Средний | Нужен продуктовый выбор, иначе cost UI-фич останется двойным |

### Архитектурные правила, которые нужно внедрить

- UI не ходит напрямую в `application.container`, repositories, pipeline internals и private fields.
- Новая фича сначала классифицируется: UI / application use case / provider integration / persistence change.
- `legacy_core` вызывается только через адаптеры нового слоя.
- Любое изменение persisted payload требует `schema_version`, migration note и regression tests.
- Очереди и фоновые задачи живут в application/runtime слое, не в UI-контроллере.
- Product knobs и technical knobs не смешиваются: media counts, retry policy, concurrency и provider priority должны быть разными сущностями.

## 8. Улучшения качества кода

| Направление | Что менять | Expected impact | Implementation cost |
| --- | --- | --- | --- |
| Уменьшить размер change hotspots | Резать файлы 1.2k-2k LOC на стабильные модули по ответственности | Высокий | Средний-высокий |
| Убрать ручной дубль settings mapping | Один serializer/deserializer для `ApplicationSettings` в `storage/repositories.py`, UI snapshot и preset paths | Средний-высокий | Средний |
| Убрать доступ к private pipeline state | Вместо `_image_backends` - публичный query API уровня application/pipeline boundary | Высокий | Низкий-средний |
| Сузить broad exceptions | Заменить `except Exception` на typed error flow и явное user-visible degradation | Высокий | Низкий |
| Ввести локальные module contracts | Короткие `README.md`/ADR на `ui/`, `pipeline/`, `providers/`, `browser/` | Средний | Низкий |
| Усилить regression coverage на seams | Отдельные тесты на import-path, run recovery, provider selection, settings serialization | Высокий | Средний |

Практическая refactoring strategy:
- сначала зафиксировать поведение regression tests;
- затем выделить pure helpers и state builders;
- потом перенести orchestration в application services;
- только после этого двигать тяжелую логику selection/persistence между файлами.

Что лучше не трогать без тестов/метрик:
- `pipeline/media.py` selection/download/persist участок;
- import-path с Gemini;
- checkpoint/resume lifecycle;
- Storyblocks browser/session flow.

## 9. Улучшения производительности

| Bottleneck | Что менять | Expected impact | Implementation cost | Безопасный rollout |
| --- | --- | --- | --- | --- |
| Синхронный import + Gemini | Делать ingestion отдельно, AI enrichment - отдельным background/manual action | Очень высокий | Средний | Сначала feature flag и сравнение latencies |
| Реальный однопоточный run | Исправить wiring, потом включать bounded concurrency по факту, а не по UI-иллюзии | Высокий | Низкий-средний | После regression tests на run lifecycle |
| Последовательный provider search | Добавить bounded fan-out по провайдерам и explicit no-match budget | Высокий | Средний | Включать по флагу и мерить per-provider latency |
| Full manifest rewrite/read | Выделить compact progress snapshot и incremental summary updates | Очень высокий | Высокий | Сначала shadow-write snapshot рядом с текущим manifest |
| Dedupe rebuild с нуля | Держать incremental dedupe state на run lifecycle | Средний-высокий | Средний | Сравнивать результат выбора до/после на тестовых сценариях |
| SQLite connection churn | Reuse connection/service lifetime + TTL/cleanup | Средний | Низкий-средний | Включить на image providers и мерить hit ratio/latency |
| Gemini client recreate на каждый вызов | Переиспользовать client/model adapter и добавить единый rate-limit/telemetry point | Средний | Низкий | Безопасно внедряется локально |
| Долгий no-match surfacing | Вводить provider budgets, ранний выход, reason aggregation и явные retryable/non-retryable ошибки | Высокий | Средний | Сначала на free-image path |

Отдельный ответ по image services:
- текущая логика не «скачивает по одной картинке с каждого сервиса»;
- она собирает кандидатов по strategy и сохраняет ограниченное число выбранных asset-ов;
- в текущем UI лимиты изображений захардкожены как `1` supporting + `1` fallback;
- поэтому следующий шаг - не «подкрутить worker count», а ввести отдельную продуктовую модель counts per slot/provider.

## 10. Улучшения масштабируемости и поддерживаемости

| Направление | Практическое изменение | Expected impact | Implementation cost |
| --- | --- | --- | --- |
| Явная live-progress модель | Отделить read model прогресса от итогового manifest | Очень высокий | Высокий |
| Явная schema versioning policy | Добавить `schema_version` в `Project`, `Run`, `RunManifest` и migration helpers | Высокий | Средний |
| Canonical provider contract | Новые providers добавлять только через registry + adapter + tests | Средний-высокий | Средний |
| App-level очередь сценариев | Ввести очередь проектов/runs в application layer, не смешивая с paragraph queue | Высокий | Средний-высокий |
| Range-based execution | Старт с абзаца `N`, диапазоны и rerun-from-failure поверх `selected_paragraphs` | Средний-высокий | Низкий-средний |
| Legacy deprecation plan | Сделать новый core единственным source of truth, а CLI - thin wrappers | Высокий стратегический | Высокий |

Что можно делать параллельно:
- schema versioning policy;
- docs/ADR и contributor rules;
- app-level queue design;
- provider contract cleanup.

Что нельзя делать параллельно с тяжелым refactor без страховки:
- крупную переделку `pipeline/media.py` и смену persistence формата;
- массовое включение concurrency до фикса manifest/event path.

## 11. Улучшения reliability/security

| Проблема | Что изменить | Expected impact | Implementation cost | Примечание |
| --- | --- | --- | --- | --- |
| Listener failures валят run | Safe publish + error capture/logging | Высокий | Низкий | Базовый hardening |
| Missing manifest recovery broken | Fail-fast или recovery в пределах того же `run_id` | Очень высокий | Низкий | Обязательный regression test |
| Broad fallback скрывает реальные дефекты | Явная деградация с user-visible статусом и reason code | Высокий | Низкий | Особенно важно для Qt/Gemini |
| Storyblocks login issue не диагностируется | Health model: cookie/session/auth wall/network/DOM drift + reset session flow | Высокий | Средний | Нужны реальные incident logs |
| Free-image payload может быть мусором | Валидация содержимого, MIME, decode, optional normalization | Высокий | Низкий-средний | Лучше reuse existing legacy logic |
| Секреты живут двумя путями | Единая policy: desktop runtime через SecretStore, legacy path только через adapter/compat | Средний-высокий | Средний | Снизит human error |
| Workspace и browser profiles не игнорируются VCS | Расширить `.gitignore`, вынести runtime hygiene в docs | Высокий | Низкий | Закрывает operational/security gap |
| Нет версии схемы persisted payload | `schema_version` + migration tests + backup policy | Высокий | Средний | Нужен перед крупными изменениями формата |

Где нужен feature flag / постепенный rollout:
- новый progress snapshot store;
- provider fan-out parallelism;
- full-script Gemini context;
- app-level scenario queue.

## 12. Улучшения DX

| Улучшение | Что именно сделать | Expected impact | Implementation cost |
| --- | --- | --- | --- |
| Root `README.md` | Dev start, source-of-truth map, основные команды, supported paths | Высокий | Низкий |
| Quality gates | `pyproject.toml`, `ruff`, `mypy`/`pyright` хотя бы на core modules, `pre-commit` | Высокий | Низкий-средний |
| CI | Запуск `python -m unittest discover -s tests`, smoke startup, lint, types | Высокий | Средний |
| Workspace hygiene | Нормальный `.gitignore`, cleanup docs, runtime dirs policy | Средний-высокий | Низкий |
| Dev scripts | Единые команды `test`, `smoke`, `lint`, `typecheck`, `portable-build` | Средний | Низкий |
| Документация для контрибьюторов | `CONTRIBUTING.md`, release checklist, incident/debug guide | Средний-высокий | Низкий |
| Тестовая матрица | Controller tests, workspace migration tests, perf smoke tests, auth/session smoke tests | Высокий | Средний |

Рекомендуемый минимум quality gates без лишней сложности:
- оставить `unittest` как основной test runner;
- добавить `ruff` для базовой дисциплины;
- типизировать сначала `app/`, `domain/`, `services/`, `storage/`, потом `pipeline/`;
- CI запускать быстро, а тяжелые интеграции держать как opt-in nightly/manual jobs.

## 13. Как подготовить проект к более качественной AI-assisted разработке

### Что мешает AI сейчас

- слишком большие файлы на critical path;
- неполная canonical boundary между new-core и legacy;
- отсутствие package-local README и explicit subsystem rules;
- смешение product knobs и technical knobs;
- нет repo-level AI guardrails и context map.

### Что добавить в репозиторий

| Артефакт | Содержимое | Expected impact | Implementation cost |
| --- | --- | --- | --- |
| `AGENTS.md` или расширенный `CONTRIBUTING.md` | Правила изменения слоев, запреты на shortcuts, checklist для persisted changes | Высокий | Низкий |
| `docs/ai/context-map.md` | Source of truth по подсистемам, canonical execution paths, запретные обходы | Высокий | Низкий |
| `docs/ai/codegen-rules.md` | Где добавлять provider/use case/session action/tests | Высокий | Низкий |
| `README.md` в `ui/`, `pipeline/`, `providers/`, `browser/` | Краткие boundaries и anti-patterns | Высокий | Низкий |
| ADR-пакет | Paragraph-first model, Storyblocks persistent profile, legacy deprecation plan | Средний-высокий | Низкий-средний |

### Инженерные правила, которые улучшат AI-assisted development

- Не держать новые runtime-модули на critical path больше ~300-500 LOC без отдельного обоснования.
- Не добавлять новые зависимости на `legacy_core` из новых модулей.
- Любой новый use case должен иметь entry point в application boundary и минимум один regression test.
- Любое изменение `RunManifest`/`Project`/`Run` обязано обновлять schema/migration notes и тесты сериализации.
- Для provider features фиксировать: queries -> ranking -> download -> persist contract.
- Для UI features фиксировать: UI action -> application use case -> state update path.
- Для AI-фич не привязывать expensive model calls к import/start path.

### Как сделать так, чтобы AI меньше ошибался

- дробить `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` на логические модули до начала новых фич;
- явно маркировать canonical path в docs, чтобы AI не лез в `legacy_core`, когда менять нужно new-core;
- фиксировать локальные README рядом с кодом, а не только phase-docs в `docs/`;
- добавить шаблоны промтов для bugfix/refactor/feature/test/perf work;
- оставлять в коде только необходимые комментарии о non-obvious invariants, а не декоративные комментарии.

## 14. Пошаговый roadmap по фазам

### Phase 0 - что нужно подтвердить/измерить перед изменениями

| Проблема | Почему это важно | Что изменить/измерить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Неясно, где именно тратится время при `open/start` | Это главная боль заказчика | Добавить тайминги `open_script`, `create_project`, `_populate_document_intents`, first UI response | `ui/controller.py`, `app/runtime.py` | Локальный фикс/измерение | Низкий | Низкая | Разделим freeze на факты и гипотезы | Получить traces на 10/50/100 paragraph docs |
| Нет данных по no-match path | Без этого сложно ускорить «долгие ошибки поиска» | Логировать latency по провайдерам и total time-to-no-match | `pipeline/media.py`, `providers/*`, `browser/*` | Локальный фикс/измерение | Низкий | Низкая | Data-driven optimization вместо гадания | Снять p50/p95 no-match по каждому mode/provider |
| Нет кореляции mid-run stop с типами ошибок | Иначе будут продолжаться «глюки» без root cause | Записывать run-stop reason, stage, provider, exception class, checkpoint state | `pipeline/media.py`, `services/events.py` | Локальный фикс/observability | Низкий | Низкая | Будет понятен dominant failure mode | В логах каждый abort имеет reason code |
| Непонятно, почему Storyblocks иногда дает invalid credentials | Без telemetry нельзя чинить надежно | Ввести auth diagnostic events и session-health snapshot | `browser/session.py`, `browser/storyblocks_backend.py` | Локальный фикс/observability | Низкий | Средняя | Быстрый переход от жалоб к воспроизводимым кейсам | 5-10 incident logs дают явную причину |
| Текущая image selection semantics непрозрачна | Это уже вызывает confusion у заказчика | Зафиксировать current mode/strategy/count behavior в docs и диагностике | `providers/registry.py`, `ui/controller.py`, `pipeline/media.py` | Введение контракта/правила | Низкий | Низкая | Снижение путаницы и неверных ожиданий | Для каждого mode виден ожидаемый count/source |

Параллельно можно делать:
- `.gitignore` cleanup;
- root `README.md` skeleton;
- сбор базовых incident logs от заказчика.

### Phase 1 - критические исправления

| Проблема | Почему это важно | Что именно нужно изменить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Import сценария блокирует UI | Главный production-like blocker | Разделить ingestion и AI enrichment; import делать без Gemini, enrichment запускать отдельно и явно показывать статус | `app/runtime.py`, `ui/controller.py` | Замена подхода/локальный refactor | Средний | Средняя | Больше нет 10-минутных зависаний на open/start | `time_to_import_project` стабилен и не зависит от Gemini |
| Реальный run однопоточный вопреки настройкам | Пользователь и команда получают ложную картину производительности | Передавать container orchestrator в `ParagraphMediaRunService`, убрать дублирующий single-thread orchestrator | `app/bootstrap.py` | Локальный фикс | Низкий | Низкая | Настройки concurrency начинают работать по-честному | Regression test на runtime wiring проходит |
| Missing manifest recovery broken | Может ломать состояние run | Fail-fast или восстановление manifest для того же `run_id`; исключить создание orphaned run | `pipeline/media.py` | Локальный фикс | Низкий | Низкая | Меньше неконсистентных состояний и «самопроизвольных» остановок | Тест на missing manifest и resume green |
| Listener failures ломают workflow | Observability не должна убивать продуктовый path | Safe publish wrapper, отдельное логирование listener errors | `services/events.py` | Локальный фикс | Низкий | Низкая | Run не падает из-за логгера/recorder | Тест на failing listener проходит |
| Broad Qt fallback скрывает реальные баги | Иначе дефекты продолжают жить скрыто | Fallback только на отсутствие `PySide6`; все прочее поднимать как явную ошибку | `ui/__init__.py` | Локальный фикс | Низкий | Низкая | Уменьшается silent degradation | Qt defects становятся видны в smoke/startup |
| Free-image payload не валидируется | Мусорный файл может считаться успешной загрузкой | Вернуть Pillow/content validation в unified path | `pipeline/media.py` | Локальный фикс | Низкий | Низкая-средняя | Повышается качество и надежность результатов | Regression test на HTML payload/битое изображение |
| Storyblocks auth failures плохо объясняются | Жалоба заказчика повторяется | Добавить reasoned auth diagnostics и безопасный session reset | `browser/*`, `ui/controller.py` | Локальный фикс | Низкий-средний | Средняя | Ошибки логина становятся воспроизводимыми и объяснимыми | Support log показывает точный тип сбоя |

Зависимости:
- сначала instrumentation из Phase 0;
- потом Phase 1 можно релизить отдельным hardening release.

### Phase 2 - быстрые улучшения с высоким ROI

| Проблема | Почему это важно | Что именно нужно изменить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Нет start-from-paragraph UX | Это прямой запрос заказчика и уже почти поддержано runtime | Добавить поле `start paragraph` / диапазон `N..end` поверх `selected_paragraphs` | `ui/*_app.py`, `ui/controller.py`, `app/runtime.py` | Локальный фикс/UI feature | Низкий | Низкая | Быстрый продуктовый выигрыш без глубокого refactor | Можно стартовать с абзаца `N`, тесты на selected range green |
| Нет product-level counts per service/slot | Пользователь не может управлять количеством медиа | Ввести отдельные настройки количества видео/изображений по slot/provider и убрать зависимость от worker knobs | `ui/contracts.py`, `ui/controller.py`, `pipeline/media.py`, `config/settings.py` | Введение контракта/правила | Средний | Средняя | Понятное и предсказуемое поведение выбора медиа | Preview и manifest отражают заданные counts |
| Нет prompt field + optional full-script context | Это важная запрошенная feature | Добавить explicit prompt action + checkbox `attach full script context`, с token budget, cache и feature flag | `ui/*_app.py`, `pipeline/intents.py`, `services/genai_client.py` | Выделение слоя/feature flag | Средний | Средняя | Рост controllability AI generation без деградации import/start | Feature включается вручную и не тормозит import |
| No-match path слишком долгий | Медленные ошибки раздражают сильнее обычных ошибок | Ввести per-provider timeout budget, early exit и понятный aggregated error | `pipeline/media.py`, `providers/*`, `browser/*` | Локальный фикс/refactor | Средний | Средняя | Быстрее понятен факт отсутствия результата | Замер p95 no-match падает |
| Mid-run stops плохо объясняются | Нужна надежность и supportability | Добавить run incident panel, reason codes, better resume guidance | `ui/controller.py`, `ui/*_app.py`, `pipeline/media.py` | Локальный фикс/DX | Низкий-средний | Средняя | Пользователь понимает, что делать после сбоя | Каждый failed run имеет actionable message |
| Текущие advanced knobs misleading | Это уже путает команду и заказчика | Переименовать/развести product settings и technical settings | `ui/contracts.py`, `ui/controller.py`, `ui/*_app.py` | Введение контракта/правила | Низкий | Низкая | Меньше ложных ожиданий от UI | UX copy соответствует реальному runtime поведению |

Параллельно можно делать:
- root `README.md`;
- `.gitignore`;
- docs по current image strategy.

### Phase 3 - структурный рефакторинг

| Проблема | Почему это важно | Что именно нужно изменить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| UI пробивает application boundary | Это главный источник связанности | Расширить `DesktopApplication` до use-case API: project/run/session/settings/media selection operations | `app/runtime.py`, `ui/controller.py` | Выделение слоя/модуля | Средний | Средняя | UI перестает быть orchestrator-ом бизнеса | Новые UI changes не требуют repository/pipeline access |
| `ui/controller.py` - god object | Высокий cognitive load и regression risk | Разбить controller на action modules и state builders | `ui/controller.py` | Рефакторинг/реструктуризация | Средний | Средняя | Проще локально менять UI behavior и тестировать | Файл/модули уменьшаются, controller tests сохраняют поведение |
| `pipeline/media.py` слишком централизован | Любая правка рискованна | Вынести selection/download/manifest/run/dedupe в отдельные сервисы | `pipeline/media.py` | Рефакторинг/выделение слоя | Средний-высокий | Высокая | Снижение change risk на core path | Service-level tests проходят, API seam становится явным |
| `pipeline/intents.py` перегружен | Невозможно безопасно развивать prompt/context features | Выделить prompt builder, response parser, document executor | `pipeline/intents.py` | Рефакторинг | Средний | Средняя | Проще добавлять Gemini features и тесты | Prompt tests и parser tests isolated |
| Settings mapping размазан | Это источник тихих конфиг-багов | Один serializer/deserializer для настроек и preset/export/import flows | `storage/repositories.py`, `ui/controller.py` | Рефакторинг | Низкий-средний | Средняя | Конфигурация становится предсказуемой | Snapshot/load/save consistency tests green |
| Legacy/new split-brain | Поддержка остается дорогой | Завести migration backlog и adapters map; новые changes - только в new-core | `legacy_core/*`, `pipeline/*`, `providers/*`, root scripts | Реструктуризация | Средний | Высокая | Один source of truth и меньше drift | Каждая legacy зависимость имеет owner и migration plan |

Нельзя начинать эту фазу без:
- regression tests на Phase 1-2 fixes;
- замеров из Phase 0;
- явного списка canonical paths.

### Phase 4 - улучшения производительности и масштабируемости

| Проблема | Почему это важно | Что именно нужно изменить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Manifest как live bus и persisted state | Это главный I/O bottleneck | Выделить compact progress snapshot store и incremental summary updates | `pipeline/media.py`, `storage/*`, `ui/*_app.py` | Замена подхода/выделение слоя | Средний-высокий | Высокая | Сильное снижение write/read amplification | Снижаются `save_manifest()` count/bytes и UI polling cost |
| Provider search линейный | Увеличивает время run и no-match | Добавить bounded parallel fan-out и отдельный budget per provider | `pipeline/media.py`, `providers/*`, `browser/*` | Рефакторинг | Средний | Средняя | Ниже latency на длинных runs | p50/p95 time_per_paragraph падает |
| Dedupe rebuild с нуля | На длинных сценариях дает квадратичный рост | Хранить dedupe state инкрементально в lifecycle run | `pipeline/media.py` | Локальный refactor | Средний | Средняя | Стабильнее throughput на 50/100/200 paragraphs | Call count/latency dedupe снижаются |
| Cache lifecycle слабый | Stale data и лишний overhead | TTL/eviction/versioning + connection reuse | `providers/images/caching.py` | Рефакторинг | Низкий-средний | Средняя | Предсказуемее perf и debugging | Есть cache hit ratio и cleanup policy |
| UI polling слишком тяжелый | Лишний CPU/I/O шум | Перевести UI на compact snapshot или event-driven refresh | `ui/qt_app.py`, `ui/tk_app.py`, `ui/controller.py` | Замена подхода | Средний | Средняя | Снижается нагрузка на UI path | Polling cost измеримо падает |
| Нужна очередь сценариев | Следующий продуктовый шаг после стабилизации | Ввести app-level queue service с bounded workers и cancel/retry policy | `app/runtime.py`, новый application queue service, `ui/*_app.py` | Выделение слоя/модуля | Средний | Средняя-высокая | Можно безопасно запускать несколько сценариев последовательно | Queue state и cancel/retry покрыты тестами |

Безопасная миграция:
- сначала shadow snapshot рядом с текущим manifest;
- затем переключить UI на snapshot read;
- только потом оптимизировать manifest writes.

### Phase 5 - DX, тесты, документация, процессы

| Проблема | Почему это важно | Что именно нужно изменить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Нет root developer map | Онбординг и AI navigation замедлены | Добавить `README.md` с dev start, architecture map и source-of-truth sections | root | Введение контракта/документации | Низкий | Низкая | Быстрый onboarding и меньше неверных правок | Новый инженер поднимает проект по README |
| Нет CI/lint/type gates | Ошибки ловятся слишком поздно | Добавить `pyproject.toml`, `ruff`, typed checks, CI workflow | root | Локальный process/tooling fix | Низкий-средний | Средняя | Меньше регрессий и дрейфа | На ветке проходят tests/lint/types |
| Нет package-local docs и AI rules | Сложно безопасно менять крупные подсистемы | Добавить subsystem README, `docs/ai/context-map.md`, `docs/ai/codegen-rules.md` | `ui/`, `pipeline/`, `providers/`, `browser/`, `docs/ai/` | Введение контракта/правила | Низкий | Низкая | Лучше и люди, и AI понимают кодовую базу | Новый change follows documented path |
| Нет perf regression safety net | Можно «оптимизировать» на глаз | Добавить perf smoke checks и latency counters для ключевых сценариев | tests, CI/manual scripts | Локальный process/tooling fix | Низкий-средний | Средняя | Оптимизации становятся измеримыми | Есть baseline и сравнение до/после |
| Нет migration/contract tests для persistence | Опасно менять схему | Добавить tests на schema versioning, backward compatibility и recovery | `tests/`, `storage/*` | Локальный process/tooling fix | Низкий-средний | Средняя | Безопаснее эволюция данных | Старые payload shapes успешно читаются |
| Документация фрагментирована | Исторически полезно, но day-to-day неудобно | Собрать contributor-facing docs поверх phase-docs, а не вместо них | `docs/` | Реструктуризация документации | Низкий | Низкая | Лучше operational use и onboarding | Есть короткий docs index и понятный flow |

### Phase 6 - долгосрочные архитектурные улучшения

| Проблема | Почему это важно | Что именно нужно изменить | Где проявляется | Тип изменения | Риск | Сложность | Ожидаемый эффект | Как проверить |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Непонятна стратегия Tk fallback | Поддержка UI остается дорогой | Принять решение: Qt-only support или thin fallback без parity promise | `ui/*` | Замена подхода/правила | Средний | Средняя | Снижается cost новых UI features | Политика зафиксирована в docs и release notes |
| Legacy still in runtime graph | Это мешает эволюции new-core | Довести legacy до compatibility wrapper статуса | `legacy_core/*`, root scripts | Реструктуризация | Средний-высокий | Высокая | Меньше drift и проще AI navigation | New features больше не затрагивают legacy |
| JSON workspace может стать узким местом при росте истории | Сейчас это не P0, но может стать P2/P3 позже | Рассмотреть частичный перевод run/project read model на SQLite только после стабилизации contracts | `storage/*`, `pipeline/media.py` | Возможная замена подхода | Высокий | Высокая | Более устойчивый рост истории и быстрые reads | Есть spike с замерами и rollback plan |
| Full-script Gemini context может стать дорогим/тяжелым | Фича нужна, но без budget легко ухудшить UX и стоимость | Ввести caching, token budget, truncation strategy и explicit user control | `pipeline/intents.py`, `services/genai_client.py`, `ui/*_app.py` | Улучшение существующего подхода | Средний | Средняя | Полезная AI feature без деградации runtime | Latency/cost укладываются в budget |
| Нужна более зрелая run orchestration модель | Для очереди сценариев и будущих интеграций | Эволюционно выделить run-planner/app queue service поверх текущего orchestrator | `app/`, `pipeline/orchestrator.py` | Выделение слоя/модуля | Средний | Средняя-высокая | Платформа становится готовой к росту фич | Queue/cancel/retry semantics покрыты тестами |

### Первые 10 конкретных задач для начала улучшения проекта

| # | Задача | Цель | Ожидаемый эффект | Риск | Зависимость от других задач | С чего начать |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Добавить тайминги и reason codes на `open/start` и run-stop | Превратить жалобы в измеримые кейсы | Появится точный baseline по freeze и mid-run stop | Низкий | Нет | Обернуть `open_script`, `create_project`, `_populate_document_intents`, `process_paragraph`, `save_manifest()` |
| 2 | Убрать Gemini с синхронного import-path | Снять главный UX blocker | Открытие сценария перестанет зависеть от Gemini latency | Средний | 1 | Разделить ingestion и enrichment в `app/runtime.py` |
| 3 | Починить runtime orchestrator wiring | Убрать ложный performance ceiling | Настройки concurrency начнут работать по факту | Низкий | 1 | Исправить `app/bootstrap.py` и добавить regression test |
| 4 | Исправить missing manifest recovery | Убрать неконсистентные run states | Меньше «самопроизвольных» остановок и битых run | Низкий | 1 | Зафиксировать ожидаемое поведение в тесте и поправить `pipeline/media.py` |
| 5 | Изолировать `EventBus` listener failures | Нельзя позволять логгеру валить run | Повысится надежность без архитектурной переделки | Низкий | 1 | Добавить safe publish wrapper и test на failing listener |
| 6 | Вернуть валидацию image payload | Исключить мусорные ассеты | HTML/битые payload-ы перестанут считаться успехом | Низкий | Нет | Переиспользовать legacy image validation в unified path |
| 7 | Добавить Storyblocks auth/session diagnostics и reset flow | Локализовать жалобу про «неверные данные» | Support и разработка увидят реальную причину auth failure | Низкий-средний | 1 | Зафиксировать типы auth ошибок и вывести их в UI/logs |
| 8 | Ускорить no-match path и сделать ошибку явной | Снять долгую «ошибку поиска» | Пользователь быстрее узнает, что результата нет | Средний | 1, 3 | Ввести per-provider timeout budget и aggregated error result |
| 9 | Добавить start-from-paragraph на базе `selected_paragraphs` | Быстро закрыть важную feature gap | Можно безопасно запускать сценарий с нужного абзаца | Низкий | 2 | Добавить UI field и range translation в controller |
| 10 | Ввести explicit AI prompt action с опцией full-script context и отдельно описать current image-count semantics | Закрыть два частых product gaps без деградации startup | Пользователь контролирует AI prompt, а ожидания по количеству медиа становятся прозрачными | Средний | 2, 3 | Сначала документировать current behavior, затем добавить feature flag и settings contract |

## 15. Что измерить и проверить перед крупными изменениями

- `time_to_import_project` с Gemini key и без него; отдельно `time_to_first_ui_response`.
- `time_to_start_run` и `time_per_paragraph` по стадиям: provider search, ranking, download, persist.
- p50/p95 времени no-match для каждого режима и провайдера.
- число и размер `save_manifest()` операций на run; медиана/p95 размера `manifest.json`.
- стоимость UI polling: CPU/I/O при 50/100/200 абзацах.
- cache hit ratio по image providers и доля stale/expired cache entries.
- число abort/stop/fail по reason code и стадии run.
- частота и тип Storyblocks auth/session failures.
- среднее число успешно скачанных asset-ов на абзац по режимам и провайдерам.
- если добавляется full-script Gemini context: token usage, latency, fail rate, cache reuse rate.

Чего еще не хватает для точных решений:
- реальные incident logs от заказчика по mid-run stop;
- фактическая длина сценариев и распределение по режимам;
- продуктовый ответ: Qt-only или Qt+Tk support policy;
- план поддержки legacy CLI scripts как продукта или только как compatibility layer.

## 16. Какие изменения делать нельзя без дополнительной подготовки

- Нельзя сразу мигрировать весь workspace с JSON на SQLite до фикса boundaries, schema versioning и измерений реальной боли.
- Нельзя удалять `legacy_core` до появления адаптеров, migration map и regression coverage на canonical flows.
- Нельзя массово включать новую concurrency-модель до исправления manifest/event path и появления latency/error telemetry.
- Нельзя встраивать full-script Gemini context в import/start path; это должно быть только явное действие пользователя или background feature под флагом.
- Нельзя строить очередь сценариев внутри `ui/controller.py`; сначала нужен application-level queue service.
- Нельзя менять persisted payload без `schema_version`, migration tests и backup/recovery notes.
- Нельзя принимать решение по Tk fallback без продуктовой позиции по supported UI matrix.
- Нельзя делать большой «чистый рефакторинг» `pipeline/media.py` без предварительных seam-tests и фиксации текущего поведения.

## 17. Итоговый порядок действий команды

1. Зафиксировать baseline: instrumentation, incident logs, reproducible freeze/no-match/auth cases.
2. Выпустить hardening wave: async import path, orchestrator wiring fix, missing manifest fix, safe EventBus, narrow Qt fallback, image validation.
3. Сразу после hardening подтвердить эффект цифрами: open time, no-match latency, fail reason distribution.
4. Закрыть самые дешевые продуктовые gaps с высоким ROI: start-from-paragraph, понятные media counts semantics, explicit no-match surfacing, Storyblocks auth diagnostics.
5. Вынести application boundary и разрезать `ui/controller.py`/`pipeline/intents.py`; только потом начинать тяжелый refactor `pipeline/media.py`.
6. После стабилизации contracts перейти к live-progress/persistence redesign и bounded provider parallelism.
7. Параллельно навести инженерную дисциплину: `README`, `.gitignore`, CI, lint/type gates, package README, docs/ai, contributor rules.
8. Когда runtime стабилен и измерим, переходить к app-level queue сценариев, legacy deprecation и, при подтвержденной необходимости, к эволюции persistence.

Рекомендуемое разделение по потокам работ:
- Поток A: runtime stabilization и customer incidents;
- Поток B: DX/CI/docs/AI guardrails;
- Поток C: structural refactor после закрепления поведения тестами;
- Поток D: performance/persistence redesign только после Phase 0-3.

Короткий принцип принятия решений дальше:
- practical engineering value > theoretical purity;
- сначала устранять скрытые сбои и ложные настройки;
- потом уменьшать стоимость изменений;
- только затем оптимизировать глубокую архитектуру и хранение данных.
