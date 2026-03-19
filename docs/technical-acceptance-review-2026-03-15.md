# Technical acceptance review: implementation verification

## 1. Executive summary

- Проверка выполнена по `docs/technical-audit-2026-03-14.md`, `docs/project-improvement-plan-2026-03-14.md`, текущему коду, тестам, документации и локальным verification-командам.
- Подтвержденные реальные улучшения есть: Gemini убран с import/open path (`app/runtime.py:58`), wiring orchestrator исправлен (`app/bootstrap.py:157`), broken manifest recovery исправлен (`pipeline/media.py:1797`), listener failures изолированы (`services/events.py:55`), Qt fallback сужен (`ui/__init__.py:19`), валидация image payload возвращена (`pipeline/media.py:62`), Storyblocks diagnostics/reset flow добавлены (`browser/session.py:525`, `ui/controller.py:898`), появился start-from-paragraph и explicit AI enrichment (`ui/controller.py:588`, `ui/controller.py:1973`).
- Но roadmap выполнен только частично. Самые дорогие системные задачи не закрыты: application boundary не выровнен, manifest/polling I/O модель не переделана, provider search остается линейным, dedupe по-прежнему пересчитывается с нуля, cache lifecycle не исправлен, legacy/new split-brain не устранен.
- Архитектурно проект не стал проще. Ключевые hotspot-файлы не уменьшились, а выросли: `ui/controller.py` - 2194 LOC (было 2006), `pipeline/media.py` - 1951 LOC (было 1805), `pipeline/intents.py` - 1297 LOC (было 1261).
- DX улучшен частично: появились `README.md`, `CONTRIBUTING.md`, `pyproject.toml`, `.github/workflows/ci.yml`, package-local README и расширенный `.gitignore`, но нет type checks, нет `docs/ai/*`, нет smoke/perf checks в CI, а часть документации и ссылок уже противоречит реальному состоянию репозитория.
- Критичный итоговый факт: проект сейчас не проходит собственную тестовую приемку. `python -m unittest discover -s tests` дал `Ran 103 tests`, `FAILED (failures=2)` из-за release/docs regressions в `tests/test_phase10_release.py`. `python -m app --smoke --no-gui` проходит. `python -m ruff check .` локально не запускается, потому что `ruff` не установлен в текущем dev env.
- Вывод как technical acceptance review: hardening-wave частично удалась, но реализацию нельзя считать качественно завершившей roadmap. Принимать можно только отдельные исправления, не весь план целиком.

## 2. Что планировалось улучшить

Карта исходных проблем и целевых изменений:

| Проблема из аудита | Планируемое улучшение | Подразумеваемый критерий успеха |
| --- | --- | --- |
| UI freeze на import/open из-за синхронного Gemini | Убрать Gemini с import path, сделать enrichment отдельным действием | Открытие сценария не зависит от сети/AI latency |
| Runtime wiring вводит в заблуждение, run фактически однопоточный | Подать в media run тот же orchestrator, что настроен в container | `paragraph_workers` и `queue_size` реально влияют на execution |
| Broken recovery при отсутствии manifest | Fail-fast или recreate manifest для того же `run_id` | Нет orphaned run и рассинхронизации `run_id` |
| Listener failures валят workflow | Safe publish wrapper | Observability side effects не ломают run |
| Broad Qt fallback скрывает дефекты | Fallback только на отсутствие `PySide6` | Реальные Qt bugs не маскируются |
| Free-image path потерял payload validation | Вернуть MIME/content validation | HTML/битые payload не считаются valid image |
| Storyblocks auth defects плохо диагностируются | Reason codes, diagnostics, reset flow | Support видит причину, UI может безопасно восстановиться |
| Нет start-from-paragraph UX | Экспонировать `selected_paragraphs` и range selector | Можно запускать `N..end` без ручных обходов |
| Нет controllable AI prompt/full-script context | Добавить explicit AI enrichment, manual prompt, full-script option | Фича не тормозит import/start и управляется явно |
| Product knobs смешаны с technical knobs | Развести counts и worker settings | Пользовательские настройки отражают реальное поведение |
| UI пробивает application boundary | Расширить `DesktopApplication`, убрать прямые `container`/repository/pipeline calls | UI становится adapter-ом, а не business orchestrator-ом |
| God modules | Декомпозировать `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` | Снижается cognitive load и change risk |
| Manifest используется как persisted state и live-progress bus | Вынести compact progress snapshot и incremental summary updates | Падает write/read amplification |
| Provider search, dedupe, cache path слабо масштабируются | Fan-out, incremental dedupe, cache TTL/connection reuse | Реальное снижение latency и I/O tax |
| Нет DX/AI guardrails | README, CI, lint, types, docs/ai, subsystem rules | Новый инженер и AI видят понятные правила работы |

## 3. Что реально было реализовано

- `DesktopApplication.create_project()` теперь делает только ingest + heuristic bootstrap, без Gemini-вызова на import path (`app/runtime.py:58-95`).
- Добавлен explicit use case `enrich_project_intents()` с manual prompt и full-script context (`app/runtime.py:97-162`, `ui/controller.py:588-610`, `ui/qt_app.py:191-208`, `ui/tk_app.py:234-248`).
- В `bootstrap_application()` больше нет второго fake-orchestrator для runtime path; `ParagraphMediaRunService` получает shared configured orchestrator (`app/bootstrap.py:157-169`).
- `ParagraphMediaRunService.execute()` и `resume()` пересоздают manifest для того же `run_id`, если файл пропал (`pipeline/media.py:1797-1805`, `pipeline/media.py:1837-1845`).
- `EventBus.publish()` изолирует listener exceptions (`services/events.py:55-60`).
- Qt/Tk fallback ограничен `ModuleNotFoundError` по `PySide6` (`ui/__init__.py:19-27`).
- Free-image download path валидирует Content-Type и реальные image bytes через Pillow (`pipeline/media.py:57-95`, `pipeline/media.py:264-300`).
- Добавлены Storyblocks reason codes, diagnostics, reset flow и их отображение в UI (`browser/session.py:525-757`, `browser/storyblocks_backend.py:36-92`, `ui/controller.py:898-1077`).
- Добавлены range-based selected paragraphs и UI-поле для `N..end` (`ui/contracts.py:181-195`, `ui/controller.py:1973-2057`, `ui/qt_app.py:164-185`, `ui/tk_app.py:203-231`).
- Добавлены image count settings для supporting/fallback images (`config/settings.py:38-80`, `ui/controller.py:1475-1494`).
- Добавлен no-match budget как runtime setting/event (`config/settings.py:80`, `pipeline/media.py:1409-1435`).
- Добавлены repo-level guardrails: `README.md`, `CONTRIBUTING.md`, `pyproject.toml`, `.github/workflows/ci.yml`, package README, расширенный `.gitignore`.
- Тестовая база выросла с 96 до 103 тестов и покрывает часть новых fixes (`tests/test_paragraph_intents.py:418`, `tests/test_phase2_architecture.py:118`, `tests/test_phase9_reliability.py:565`, `tests/test_media_pipeline.py:336`, `tests/test_ui_controller.py:357`).

## 4. Матрица: план → реализация → статус → качество

| План | Expected goal | Actual result | Статус | Качество | Основные gaps | Основные риски |
| --- | --- | --- | --- | --- | --- | --- |
| Убрать Gemini с import/open path | Убрать главный UX blocker | Gemini действительно вынесен в explicit enrichment action | выполнено качественно | хорошее | Нет замеров latency, нет явного persisted marker-а «heuristic vs Gemini enriched» | Пользователь может запустить heuristic-only проект и не понять, что enrichment не делался |
| Починить runtime orchestrator wiring | Сделать concurrency settings честными | Shared orchestrator реально используется media run service | выполнено качественно | хорошее | Нет perf-baseline до/после | Пользователи по-прежнему могут переоценивать effect других worker knobs |
| Исправить missing manifest recovery | Убрать orphaned run / mismatched `run_id` | Manifest recreates for same `run_id` | выполнено качественно | хорошее | Не покрывает broader schema/versioning risks | Recovery path по-прежнему зависит от full manifest rewrite model |
| Изолировать EventBus listener failures | Logging/recorder side effects не валят run | `publish()` safe, run survives failing listener | выполнено качественно | хорошее | Нет structured counter/alert на listener failure | Ошибки observability продолжают быть noisy и могут остаться незамеченными |
| Сузить Qt fallback | Не скрывать реальные Qt defects | Fallback only on missing `PySide6` | выполнено качественно | хорошее | Нет CI smoke на Qt path | Qt regressions теперь видны, но не автоматически ловятся в CI |
| Вернуть image payload validation | Не пропускать HTML/битые payload | MIME + Pillow validation вернулись | выполнено качественно | хорошее | Нет content normalization/format policy beyond validation | Большие/редкие форматы и future payload edge cases не измерены |
| Добавить Storyblocks diagnostics/reset flow | Объяснять auth failures и дать safe reset | Reason codes, diagnostics, reset flow и UI panel добавлены | выполнено качественно | хорошее | Нет incident corpus и support docs, часть user docs не обновлена | Без production incidents нельзя доказать, что все реальные failure modes закрыты |
| Start-from-paragraph UX | Запускать `N..end` и выбранные ranges | Range selector и validation есть | выполнено качественно | хорошее | Нет user docs под эту фичу | Пользователь discoverability все еще зависит от UI догадки |
| Manual prompt + full-script context | Дать controllable AI enrichment без деградации import | Explicit action, prompt, toggle, budget added | выполнено частично | среднее | Нет cache, token/cost telemetry, user docs, intent provenance | Можно увеличить latency/cost и потерять predictability без измерений |
| Media counts per service/slot | Управлять количеством медиа продуктово, а не worker knobs | Появились только supporting/fallback image limits | выполнено частично | среднее | Нет video count, нет per-provider/per-service counts | Пользователь все еще не получает обещанную модель «по сервису/слоту» |
| No-match timeout/budget | Ускорить surfacing long no-match | Есть общий search budget event | выполнено формально, но слабо | слабое | Budget проверяется между вызовами, а не прерывает медленный backend in-flight | Один зависший provider search все еще может тянуть весь paragraph |
| Развести product knobs и technical knobs | Убрать misleading settings | Counts вынесены, но worker knobs все еще влияют на candidate limits, а не на provider/download pools | выполнено формально, но слабо | слабое | `provider_workers`, `download_workers`, `relevance_workers` остаются misleading | Команда и пользователь будут продолжать неверно интерпретировать «workers» |
| Реальная application boundary | UI не должен ходить в internals container | `DesktopApplication` расширен, но `ui/controller.py` все еще использует `application.container` 101 раз | не реализовано | неудовлетворительное | Async run/session/settings path bypass-ит application layer | Boundary leaks продолжают цементировать связанность и regression risk |
| Декомпозировать god modules | Снизить change hotspots | Критичные файлы не разделены, а стали больше | не реализовано | неудовлетворительное | `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` все еще giant files | Любая следующая фича будет дороже и опаснее |
| Разделить manifest/live progress | Снять full rewrite/full read bottleneck | Модель не изменилась | не реализовано | неудовлетворительное | UI все еще poll-ит каждые 750ms, manifest все еще full-save | Perf/scalability ceiling сохраняется |
| Provider fan-out / incremental dedupe / cache lifecycle | Реально снизить paragraph latency | Search sequential, dedupe rebuild full, cache без TTL/reuse | не реализовано | неудовлетворительное | Только no-match budget частично добавлен | Bottlenecks из аудита остались почти полностью |
| Schema versioning + migration policy | Безопасно эволюционировать persisted payload | В модели появились `schema_version`, но migration helpers/tests не добавлены | выполнено формально, но слабо | слабое | Нет backward-compat tests, no load-time branching | Ложное чувство готовности к schema changes |
| DX/CI/docs/AI guardrails | Снизить вероятность регрессий и упростить navigation | README/CI/Ruff/package README added, но без types/smoke/docs/ai и с broken docs promises | выполнено частично | среднее | `docs/ai/*` отсутствуют, release docs missing, tests red | Tooling выглядит лучше, чем реально защищает |

## 5. Что действительно стало лучше

- Gemini off import path - expected goal: убрать 10-минутные зависания на open/start; actual result: `create_project()` больше не вызывает Gemini и делает только ingest + heuristic bootstrap (`app/runtime.py:58-95`); gap: нет замеров до/после; risk: нет явной индикации, что проект остался heuristic-only.
- Shared runtime orchestrator - expected goal: сделать concurrency settings реальными; actual result: один orchestrator создается и передается в runtime path (`app/bootstrap.py:157-169`), тест это фиксирует (`tests/test_phase2_architecture.py:118-124`); gap: perf impact не измерен; risk: misleading secondary knobs остались.
- Manifest recovery + safe EventBus - expected goal: убрать неконсистентные состояния и падения из-за observability; actual result: recovery делается для того же `run_id`, listener failures не роняют run (`pipeline/media.py:1797-1805`, `services/events.py:55-60`); gap: нет richer incident telemetry; risk: failures логируются, но не агрегируются.
- Image payload validation - expected goal: не пропускать мусорные image payload; actual result: проверяются Content-Type, empty payload, Pillow verify/load (`pipeline/media.py:62-95`); gap: нет format normalization policy; risk: редкие edge cases останутся без coverage.
- Storyblocks diagnostics - expected goal: сделать «неверные данные» объяснимыми; actual result: health/reason_code/diagnostics/reset flow и UI panel действительно появились (`browser/session.py:525-757`, `ui/controller.py:898-1077`); gap: нет support guide и real incident baseline; risk: не все реальные auth-failure modes подтверждены в поле.
- Start-from-paragraph - expected goal: быстро закрыть product gap; actual result: range parser `2..end` и selected paragraphs действительно работают (`ui/controller.py:1973-2057`, `tests/test_ui_controller.py:357-398`); gap: нет user docs; risk: discoverability остается слабой.

## 6. Что реализовано частично

- Explicit AI enrichment / manual prompt / full-script context - expected goal: управляемый AI без деградации startup; actual result: отдельная кнопка, prompt, toggle и budget есть (`ui/qt_app.py:191-208`, `ui/tk_app.py:234-248`, `app/runtime.py:97-162`); gap: нет cache, token/cost telemetry, user docs и persistent provenance; risk: фича улучшает controllability, но может ухудшить predictability latency/cost.
- Media counts - expected goal: product-level control over media quantity; actual result: появились только `supporting_image_limit` и `fallback_image_limit` (`config/settings.py:78-80`, `ui/contracts.py:191-194`, `ui/controller.py:1481-1494`); gap: нет отдельного video count и per-provider/per-service counts; risk: запрос заказчика закрыт слабее, чем заявлено.
- No-match handling - expected goal: быстрее surfacing long search failures; actual result: есть общий time budget event (`pipeline/media.py:1409-1435`); gap: нет per-provider timeout/cancel, нет подтвержденных p95 improvements; risk: budget не спасает от долгого blocking backend call.
- DX/tooling - expected goal: repo-level guardrails; actual result: добавлены `README.md`, `CONTRIBUTING.md`, `pyproject.toml`, CI, package README, `.gitignore`; gap: нет types, smoke/perf checks, docs/ai, local dev install path для `ruff`; risk: tooling улучшен визуально сильнее, чем реально защищает quality.
- Schema versioning - expected goal: безопасная эволюция persisted payload; actual result: `schema_version` появился в dataclasses (`domain/models.py:144`, `domain/models.py:230`, `domain/models.py:273`, `domain/models.py:300`); gap: нет migrations и load-path branching (`storage/repositories.py:31-39`); risk: это формальная, а не рабочая стратегия совместимости.

## 7. Что реализовано слабо или неправильно

- Application boundary - expected goal: UI only via application use cases; actual result: `ui/controller.py` по-прежнему ходит напрямую в repositories, session manager, media pipeline, settings manager и even background run wiring; gap: sync path частично переведен на `DesktopApplication`, async path нет (`ui/controller.py:620-655`, `ui/controller.py:714-795`); risk: inconsistent execution paths и высокая связанность сохраняются.
- Settings serialization unification - expected goal: один serializer/deserializer вместо ручного дубля; actual result: repository и UI snapshot все еще дублируют одну и ту же mapping-логику (`storage/repositories.py:209-272`, `ui/controller.py:1407-1473`); gap: change has to be made in multiple places; risk: future config drift.
- Misleading worker knobs - expected goal: разделить product knobs и technical knobs; actual result: `provider_workers`, `download_workers`, `relevance_workers` по-прежнему называются workers, но в `MediaSelectionConfig` конвертируются в candidate limits и bounded queue sizes (`ui/controller.py:1487-1490`), при этом в `pipeline/media.py` нет ни одного отдельного pool/executor; risk: optimization theater и ложные ожидания команды.
- Documentation contract - expected goal: contributor-facing docs and AI rules; actual result: `README.md` ссылается на `docs/ai/`, но такой директории нет (`README.md:43`); risk: documentation trust erosion и navigation drift.
- Release/docs hygiene - expected goal: docs/release path должен быть согласован; actual result: portable builder и tests ожидают `docs/phase-10/*`, но root `docs/phase-10` отсутствует, из-за чего full suite красная (`tests/test_phase10_release.py:16-49`, `release_tools/portable.py:158-166`); risk: release path фактически сломан.

## 8. Что не реализовано

- Реальная application service boundary для project/run/session/settings operations не реализована.
- Декомпозиция `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` не выполнена.
- Разделение live progress и persisted manifest не выполнено.
- Incremental summary updates и compact progress snapshot не добавлены.
- Sequential provider search не заменен bounded fan-out.
- Incremental dedupe state не добавлен (`pipeline/media.py:1653-1674`).
- Cache TTL/eviction/versioning/connection reuse не реализованы (`providers/images/caching.py:48-128`).
- Type-check gate, pre-commit, dev scripts и smoke/perf checks в CI не добавлены.
- `docs/ai/context-map.md`, `docs/ai/codegen-rules.md`, `AGENTS.md` и ADR-package отсутствуют.
- Legacy/new canonical boundary не доведена: runtime по-прежнему зависит от `legacy_core` (`pipeline/ingestion.py:6`, `providers/images/clients.py:7`, `providers/images/filtering.py:6`, `providers/images/querying.py:5`).
- Run incident panel, reasoned stop telemetry и checkpoint diagnostics в том объеме, который планировался, не появились.
- Политика по Tk fallback/Qt-only support не зафиксирована.

## 9. Новые проблемы и regressions

- Репозиторий в текущем состоянии не проходит полный unittest suite: `tests/test_phase10_release.py` падает из-за отсутствующих `docs/phase-10/onboarding.md` и `docs/phase-10/release-checklist.md`. Это уже не старый долг, а текущий regression acceptance.
- Ключевые god modules стали больше, чем на момент аудита. Это ухудшение, а не просто «неисправленный долг».
- `README.md` обещает `docs/ai/`, которой нет. Это создает ложное ощущение наличия repo guardrails.
- `docs/quick-start-ru.md:66-71` и `docs/user-manual-ru.md:730-737` описывают output path как `<run_id>`, но тесты фиксируют новую модель на базе project slug (`tests/test_media_pipeline.py:382-490`). Пользовательская документация уже расходится с поведением.
- Документированная dev-история неполна: README предлагает `pip install -r requirements.txt`, но этого недостаточно для локального запуска lint, потому что `ruff` не входит в `requirements.txt` и локально отсутствует.
- Explicit enrichment улучшил UX, но создал новый risk surface: в коде нет устойчивой индикации, был ли проект обогащен Gemini или остался на heuristic bootstrap. Для пользователя это может выглядеть как «система просто работает так же», хотя качество intent/query bundle отличается.

## 10. Архитектурная оценка изменений

- `Application boundary` - что изменили: добавили новые методы в `DesktopApplication` (`app/runtime.py:97-269`); почему это хорошо: появился нормальный entry point хотя бы для explicit AI enrichment и части run use cases; реальный эффект: частичный; оставшийся риск: `ui/controller.py` все еще дергает `application.container` 101 раз, то есть boundary по сути продолжает пробиваться.
- `Use-case ownership` - что изменили: synchronous `execute_run()` идет через `DesktopApplication.execute_media_run()` (`ui/controller.py:612-632`); почему это хорошо: это движение в правильную сторону; реальный эффект: ограниченный; риск: async run path, retry path и session/settings path продолжают жить напрямую через `container`, что создает две параллельные архитектуры поведения.
- `Abstraction leak around free-image providers` - что изменили: UI больше не читает `_image_backends` напрямую, вместо этого использует public `available_free_image_provider_ids()` (`pipeline/media.py:412-417`, `ui/controller.py:1305-1314`); почему это хорошо: это реальное уменьшение одного leak; реальный эффект: локально положительный; риск: UI все еще rebuild-ит backends напрямую через media pipeline и tests продолжают залезать во внутренности.
- `God modules` - что изменили: по сути почти ничего, кроме маленького extraction в `ui/presentation.py`; почему это плохо: основной cognitive load не уменьшился; реальный эффект: скорее отрицательный, потому что файловая масса выросла; риск: любой следующий change на critical path остается дорогим и brittle.
- `Legacy/new split` - что изменили: стратегически ничего; почему это плохо: new-core все еще зависит от `legacy_core` на ingestion/query/filter/provider client path; реальный эффект: канонический source of truth по-прежнему размыт; риск: drift поведения и высокая цена рефакторинга сохраняются.
- `UI duplication` - что изменили: Qt и Tk получили новые feature-parity additions (range selector, Gemini control, counts); почему это хорошо: пользовательские фичи не остались только в одной ветке; реальный эффект: parity улучшена точечно; риск: поддерживать две толстые UI-ветки все еще дорого, а policy по support matrix нет.

## 11. Оценка качества кода после изменений

- Читаемость и локальная понятность улучшились в отдельных местах: explicit enrichment path, public free-image availability method, Storyblocks diagnostics path.
- Но hotspots не стали управляемыми. `ui/controller.py`, `pipeline/media.py`, `pipeline/intents.py` остались change hotspots и даже выросли по размеру. Это сильный отрицательный сигнал для reviewability, testability и AI safety.
- Сложность не была «размазана по более удачной структуре»; в основном она осталась в тех же файлах. `ui/presentation.py` - полезный, но очень маленький extraction, который не решает системную проблему `controller`.
- Дублирование settings mapping осталось: repository save/load и UI snapshot builder дублируют практически одну и ту же схему. Это прямой maintainability defect, а не косметика.
- В UI по-прежнему много broad exception handling в event-handlers. Для desktop UX это местами допустимо, но при текущем объеме controller это затрудняет разделение user errors, integration errors и programming errors.
- Тесты в ряде мест продолжают нормализовать доступ к internals (`tests/test_ui_controller.py:159-199`). Это плохой сигнал: тестовая база помогает удержать поведение, но одновременно закрепляет leaky architecture.

## 12. Оценка reliability/security

- Reliability улучшилась по нескольким реальным направлениям: safe EventBus, correct missing-manifest recovery, restored image validation, narrowed Qt fallback, explicit Storyblocks reset flow.
- `python -m app --smoke --no-gui` проходит, то есть базовый startup path не сломан.
- Но reliability acceptance не может быть полной, пока full test suite красная. Release path сейчас broken by definition.
- Security/hygiene частично улучшены `.gitignore`, но не решены системно: `services/secrets.py:33-61` на non-Windows все еще дает только base64 obfuscation, legacy CLI path по-прежнему работает через `.env`, а runtime free-image backends все еще умеют fallback на env vars (`pipeline/media.py:447-449`). Единая secret policy не доведена до конца.
- Error surfacing улучшился для Storyblocks, но почти не улучшился для mid-run stop diagnostics: `Run.last_error` и event journal по-прежнему дают только ограниченную информацию без exception class / reason code / checkpoint context.
- Silent Gemini fallback из import path фактически устранен не через лучший error flow, а через смену product behavior: Gemini просто больше не участвует в import. Это правильный practical fix, но user-visible provenance still missing.

## 13. Оценка производительности

Разделение по уровню доказанности:

- Подтвержденное улучшение: expensive Gemini network call убран с import/open path. По коду это реальное, а не косметическое улучшение (`app/runtime.py:58-95`). Без runtime benchmark нельзя дать цифру, но основной блокирующий I/O действительно исключен.
- Подтвержденное улучшение: paragraph-level concurrency wiring теперь честный, потому что runtime path использует shared orchestrator (`app/bootstrap.py:157-169`, `pipeline/orchestrator.py:136-185`).
- Вероятное, но не доказанное улучшение: `GeminiModelAdapter` теперь кеширует client внутри adapter instance (`services/genai_client.py:35-40`). Это уменьшает повторное создание SDK client в одном enrichment call, но не решает broader rate limiting/telemetry concerns.
- Спорное изменение: no-match budget (`pipeline/media.py:1409-1435`) выглядит как performance fix, но фактически работает только между backend calls. Если конкретный backend search завис, budget его не прервет. Это partial optimization, а не полноценное решение.
- Не улучшено: provider search все еще sequential (`pipeline/media.py:1143-1185`, `pipeline/media.py:1278-1300`), manifest summary все еще full recompute (`pipeline/media.py:1091-1129`), manifest все еще full-save после каждого paragraph (`pipeline/media.py:513`, `pipeline/media.py:570`, `pipeline/media.py:786`, `pipeline/media.py:833`), UI все еще poll-ит каждые 750ms с reload run/manifest (`ui/qt_app.py:509-528`, `ui/tk_app.py:1329-1346`, `ui/controller.py:142-213`), dedupe все еще rebuild from manifest each paragraph (`pipeline/media.py:1653-1674`), cache still opens fresh sqlite connection per get/set (`providers/images/caching.py:48-128`).
- Optimization theater, которое нельзя засчитывать как успех: `provider_workers`, `download_workers`, `relevance_workers` по названию выглядят как concurrency controls, но в реальности в `MediaSelectionConfig` это только numeric limits (`ui/controller.py:1487-1490`), а в `pipeline/media.py` нет отдельных worker pools.
- Метрик до/после нет. Следовательно performance-часть плана нельзя считать закрытой beyond a few structural fixes.

## 14. Оценка maintainability/scalability

- Для on-call/support и локальных багфиксов проект стал немного лучше: есть reason codes для Storyblocks, explicit enrichment path, больше regression tests.
- Для безопасного развития core workflow проект почти не стал лучше. Основная стоимость change по-прежнему сидит в `ui/controller.py` и `pipeline/media.py`.
- Масштабируемость runtime почти не улучшилась, потому что главный I/O bottleneck (`manifest` как storage + live bus + UI read model) сохранен полностью.
- Масштабируемость команды улучшена лишь частично: новый инженер быстрее поймет repo-level карту из `README.md`, но быстро упрется в giant files, leaky boundaries и missing `docs/ai/*`.
- Feature growth path все еще дорог: dual UI, legacy/new split, duplicated config logic и unclear boundary продолжают повышать regression risk.

## 15. Оценка DX и тестовой инфраструктуры

- Что реально стало лучше: появился root `README.md`, `CONTRIBUTING.md`, `.github/workflows/ci.yml`, `pyproject.toml`, package README, расширенный `.gitignore`.
- Что реально проверено: `python -m app --smoke --no-gui` проходит; test suite вырос до 103 тестов и покрывает часть ключевых fixes.
- Критичный минус: `python -m unittest discover -s tests` сейчас падает. Пока это не исправлено, говорить о «введенных quality gates» как о завершенном результате нельзя.
- Локальная developer story неполная: команда из README (`pip install -r requirements.txt`) не ставит `ruff`, поэтому локальный lint path сломан из коробки.
- CI слишком узкий относительно плана: нет smoke startup, нет types, нет perf smoke, нет migration tests.
- Документация частично противоречит коду: нет `docs/ai/*`, нет root `docs/phase-10/*`, user docs устарели по output path и не описывают новые AI/range features.
- Тесты стали лучше по риску, а не только по количеству: особенно полезны `tests/test_paragraph_intents.py:418-474`, `tests/test_phase9_reliability.py:565-600`, `tests/test_ui_controller.py:357-398`. Но coverage на perf/release/docs/contracts по-прежнему недостаточна.

## 16. Оценка пригодности проекта для AI-assisted development

- Что реально помогает AI: `README.md` с architecture map, `CONTRIBUTING.md` с layer rules, package README, explicit enrichment use case, более явные UI contracts (`ui/contracts.py`).
- Что выглядит структурно, но почти не помогает: очень тонкие package README без конкретных seams/anti-pattern examples; `schema_version` без migrations; маленький extraction `ui/presentation.py` при сохранении giant controller.
- Что все еще мешает AI больше всего: giant hotspot files, UI boundary leaks, отсутствие `docs/ai/context-map.md`, отсутствие `docs/ai/codegen-rules.md`, legacy/new split-brain, tests that validate internals instead of stable public seams.
- Итог: проект стал немного понятнее для навигации, но не стал заметно безопаснее для AI-изменений. Улучшение больше документационное, чем архитектурное.

## 17. Ложные улучшения / спорные изменения / complexity tax

- `schema_version` в моделях - выглядит как migration readiness, но без migration helpers и backward-compat tests это формальная надпись, а не working policy.
- `provider_workers` / `download_workers` / `relevance_workers` - выглядят как performance knobs, но не управляют реальными pools в media pipeline. Это classic misleading control surface.
- `README.md` и `CONTRIBUTING.md` - полезны, но наличие ссылок на отсутствующие `docs/ai/*` и несогласованных release docs показывает, что часть improvements носит cosmetic character и не доведена до эксплуатации.
- `ui/presentation.py` - полезное локальное улучшение, но его нельзя засчитывать как решение проблемы `ui/controller.py`.
- Explicit AI enrichment - это сильный practical fix для startup UX, но без intent provenance/UX marker он создает новый когнитивный долг: system behavior quality changes, а пользователю это явно не показано.
- Package README и CI добавляют ощущение инженерной зрелости, но пока full suite red, а types/smoke отсутствуют, это еще не полноценные guardrails.

## 18. Что нужно доработать в первую очередь

1. Вернуть репозиторий в green state: либо восстановить `docs/phase-10/*`, либо синхронизировать release tests и portable bundle expectations. Пока `tests/test_phase10_release.py` красный, acceptance blocked.
2. Закрыть broken documentation contracts: убрать/добавить `docs/ai/*`, обновить user docs по output path, start-from-paragraph, explicit AI enrichment, Storyblocks reset diagnostics.
3. Довести application boundary: убрать direct `application.container.*` из async run path, session/settings path и manual selection path, а не только из нескольких sync methods.
4. Развести product settings и technical settings по смыслу, а не только по полям: убрать misleading semantics у worker knobs.
5. Добавить intent provenance/state: UI должен явно показывать, что текущий project использует heuristic bootstrap или Gemini-enriched intents.
6. Добавить smoke + types в CI и починить local dev path для lint.

## 19. Что стоит переписать или упростить

- `ui/controller.py` - разрезать минимум на `project_actions`, `run_actions`, `session_actions`, `settings_actions`, `state_builders`, `background_tasks`. Сейчас это главный architectural blocker.
- `pipeline/media.py` - вынести отдельно search/selection/download/manifest/run-lifecycle/dedupe seams. Пока это единый service blob.
- `pipeline/intents.py` - выделить prompt builder, response parser, query builder и document executor. Сейчас даже полезные AI-фичи добавляются в слишком большой файл.
- Settings snapshot/save/load - собрать в единый serializer/deserializer вместо дубля между `storage/repositories.py` и `ui/controller.py`.
- `providers/images/caching.py` - упростить модель доступа за счет service lifetime connection reuse и явной lifecycle policy вместо repeated connect/close на каждый get/set.

## 20. Что нужно дополнительно проверить метриками, тестами и runtime-данными

- `time_to_import_project` до и после изменения import path, на 10/50/100 paragraph docs.
- Реальный throughput benefit от `paragraph_workers` после wiring fix: p50/p95 `time_per_paragraph` и `time_to_complete_run`.
- `no_match_budget_seconds`: сколько реально сокращается p95 no-match time, особенно при медленном backend.
- Количество и объем `save_manifest()` операций за run; стоимость UI polling при 50/100/200 paragraphs.
- Частота `paragraph.failed` / `run.failed` по reason classes и checkpoint context.
- Storyblocks auth incidents по новым `reason_code`: expired / login_required / challenge / blocked / transient_navigation.
- Full-script context cost profile: latency, token budget, failure rate, user value, cache hit ratio (если cache будет добавлен).
- Backward-compat tests на persisted payload, если `schema_version` предполагается использовать всерьез.

## 21. Итоговая оценка качества реализации

| Критерий | Оценка | Почему |
| --- | --- | --- |
| Соответствие плану | слабо | Закрыта только часть hardening и несколько product quick wins; архитектурные и performance phases в основном не реализованы |
| Качество исполнения | удовлетворительно | Несколько конкретных fixes сделаны хорошо, но есть broken docs, red tests и много partial implementations |
| Глубина решения проблем | слабо | Убраны симптомы в отдельных местах, но системные причины сложности и I/O bottlenecks почти не тронуты |
| Архитектурная адекватность | слабо | Boundary leaks, giant files, legacy/new split и dual UI остаются; controller по-прежнему business orchestrator |
| Качество кода | слабо | Hotspot files выросли, duplication остался, complexity не снижена |
| Безопасность изменений | удовлетворительно | `.gitignore`, image validation и Storyblocks diagnostics улучшают baseline, но secret policy все еще не едина |
| Надежность | удовлетворительно | EventBus, manifest recovery и explicit enrichment path реально помогают, но release path сломан, test suite красная |
| Maintainability | слабо | Менять project дальше все еще дорого и рискованно из-за тех же hotspot-ов и слабых boundaries |
| Performance value | удовлетворительно | Самый дорогой synchronous AI call убран с import path и orchestrator wiring исправлен, но core runtime bottlenecks почти не изменены и не измерены |
| DX value | удовлетворительно | README/CI/.gitignore/package docs добавлены, но types/smoke/perf checks отсутствуют, а tests не green |
| Value для AI-assisted development | слабо | Навигация стала немного понятнее, но giant files, missing docs/ai и leaky architecture по-прежнему делают AI changes рискованными |

Итоговый verdict: частичное принятие hardening-исправлений возможно, но реализация в целом не соответствует критерию «план улучшений качественно завершен».

## 22. Следующий практический план действий

1. Сначала вернуть проект в доказуемо рабочее состояние: починить `tests/test_phase10_release.py`, согласовать portable bundle и root docs, добиться green test suite.
2. Сразу после этого закрыть broken repo contracts: либо добавить `docs/ai/*`, либо убрать ссылки на них; синхронизировать README, quick start и user manual с реальным поведением.
3. Затем довести hardening до логического завершения: добавить intent provenance, smoke в CI, локально воспроизводимый lint path, structured run failure telemetry.
4. После stabilization перейти к boundary work: убрать direct `application.container.*` из UI controller, начиная с async run/session/settings paths.
5. Только затем делать structural refactor `ui/controller.py` и `pipeline/intents.py`; `pipeline/media.py` резать уже после выделения seam-tests.
6. После выравнивания boundaries переходить к настоящим performance задачам: manifest/progress split, sequential search fan-out, incremental dedupe, cache lifecycle.
7. Параллельно формализовать AI guardrails и subsystem rules в реальных `docs/ai/*` и package-level guidance, а не только в общих обещаниях.
