# План добивки phase 6 после аудита

Источник: `docs/optimization-checklists/task-plans/phase-6-implementation-audit-2026-03-21.md`

## Цель

- закрыть все high-risk зоны из аудита;
- дочистить остатки удаленных функций и старых архитектурных слоев;
- убрать мертвый код, неиспользуемую документацию и ложные legacy-сигналы;
- довести код, тесты и docs до одного согласованного phase-6 контракта.

## Правила выполнения

- выполнять план небольшими change set'ами, не смешивая несколько high-risk направлений в одном diff;
- после каждого большого блока запускать таргетные тесты и grep-проверки;
- legacy read compatibility сохранять там, где она защищает существующие workspace и preset/run payload;
- не удалять `.env.example` как dev-артефакт репозитория; чистить только ложные end-user/runtime references;
- не трогать generated `dist/` вручную; менять только исходники, тесты и docs.

## Фаза 0. Базовая подготовка

### 0.1. Зафиксировать baseline перед cleanup

- [ ] Перечитать `docs/optimization-checklists/task-plans/phase-6-implementation-audit-2026-03-21.md` и выписать все high/medium risk пункты в рабочую заметку.
- [ ] Зафиксировать список ключевых grep-маркеров, которые должны исчезнуть после cleanup:
  - `list_profiles|set_active|select_profile|rename_profile|delete_profile`
  - `provider_group|priority|mixed_image_fallback`
  - `user_locked`
  - `DesktopTkApp|launch_tk_app|tk_app.py`
  - `candidate_assets|selected_assets|lock_asset|reject_asset`
- [ ] Зафиксировать текущий baseline-комплект проверок:
  - `ruff check .`
  - `python -m unittest discover -s tests`
  - `python -m app --smoke --no-gui`
  - `python -m release_tools.portable --output-dir dist/portable-smoke --version baseline-check`

### 0.2. Подготовить sequencing работ

- [ ] Выполнять high-risk блоки в таком порядке: `P6-05 -> P6-02 -> P6-06 -> P6-07 -> docs/dead-code cleanup`.
- [ ] Не начинать doc cleanup раньше, чем будут понятны финальные сигнатуры API и финальный vocabulary.
- [ ] После завершения каждой фазы обновлять этот чеклист или отдельный status log.

## Фаза 1. Закрыть `P6-05` до полного singleton contract

Цель фазы: оставить один managed Storyblocks profile не только на уровне UX, но и на уровне публичного API, тестов и внутренней модели.

### 1.1. Зафиксировать целевой singleton contract

- [ ] Выписать в комментарии к change set или отдельной note точный contract: один managed profile, без выбора active profile, без profile switching.
- [ ] Отдельно зафиксировать, что discovery внешних Chrome/Edge профилей может остаться только как import UX, а не как управление несколькими managed profile.
- [ ] Решить naming cleanup: переименовать `discover_storyblocks_sessions(...)` в название, отражающее импорт внешнего профиля, а не discovery managed sessions.

### 1.2. Убрать multi-profile API из `browser/profiles.py`

- [ ] Найти все production call sites для `list_profiles()`.
- [ ] Найти все production call sites для `rename_profile()`.
- [ ] Найти все production call sites для `delete_profile()`.
- [ ] Найти все production call sites для `set_active()`.
- [ ] Найти все production call sites для `select_profile()`.
- [ ] Если production call sites отсутствуют, удалить `list_profiles()` из `browser/profiles.py`.
- [ ] Если production call sites отсутствуют, удалить `rename_profile()` из `browser/profiles.py`.
- [ ] Если production call sites отсутствуют, удалить `delete_profile()` из `browser/profiles.py`.
- [ ] Если production call sites отсутствуют, удалить `set_active()` из `browser/profiles.py`.
- [ ] Если production call sites отсутствуют, удалить `select_profile()` из `browser/profiles.py`.
- [ ] Оставить только singleton entrypoints:
  - `get_singleton()`
  - `get_or_create_singleton()`
  - `paths_for()`
  - `update_session_health()`
  - `update_storyblocks_account()`
  - `save_profile()`
- [ ] Проверить, что `_select_singleton_profile(...)` больше не зависит от живой semantics `is_active` как runtime-переключателя.

### 1.3. Сузить модель `BrowserProfile`

- [ ] Проверить, где реально используется `BrowserProfile.is_active`.
- [ ] Если `is_active` больше нужен только для legacy read, перестать использовать его в runtime-логике.
- [ ] Перенести выбор singleton profile на предсказуемое правило без `is_active`.
- [ ] Решить судьбу поля `is_active`:
  - либо удалить из dataclass и оставить ignore-on-load для legacy payload;
  - либо оставить как legacy field, но убрать все runtime-чтения.
- [ ] Проверить, что `Project.active_browser_profile_id` не участвует в активном runtime path.
- [ ] Если не участвует, пометить поле как legacy-only в docs/commentary и не расширять его дальше.

### 1.4. Перевести `BrowserSessionManager` на single-profile public API

- [ ] Составить список всех public методов `BrowserSessionManager`, где еще есть `profile_id`.
- [ ] Перевести `current_state()` на вызов без `profile_id` в production flow.
- [ ] Перевести `set_health()` на вызов без `profile_id` в production flow.
- [ ] Перевести `set_manual_ready_override()` на вызов без `profile_id` в production flow.
- [ ] Перевести `clear_manual_ready_override()` на вызов без `profile_id` в production flow.
- [ ] Перевести `check_browser_channel()` на вызов без `profile_id` в production flow.
- [ ] Перевести `profile_in_use()` на вызов без `profile_id` в production flow.
- [ ] Перевести `open_browser()` на вызов без `profile_id` в production flow.
- [ ] Перевести `open_native_login_browser()` на вызов без `profile_id` в production flow.
- [ ] Перевести `close_native_browser()` на вызов без `profile_id` в production flow.
- [ ] Перевести `native_browser_running()` на вызов без `profile_id` в production flow.
- [ ] Перевести `close_browser()` на вызов без `profile_id` в production flow.
- [ ] Перевести `check_authorization()` на вызов без `profile_id` в production flow.
- [ ] Перевести `rescue_storyblocks_query()` на вызов без `profile_id` в production flow, если такой метод еще публичен.
- [ ] После перевода production callers удалить `profile_id` из публичных сигнатур, где он больше не нужен.
- [ ] Оставить private resolver singleton profile внутри manager.
- [ ] Проверить, можно ли заменить single-state поля `_active_session`, `_native_browser_session`, `_state` без остаточных dict-based patterns.
- [ ] Сохранить owner-thread checks и thread-safe close/shutdown path.

### 1.5. Упростить import flow и controller naming

- [ ] Проверить, где используется `ChromiumProfileImportService.discover_profiles(...)`.
- [ ] Переименовать UI/controller-level method так, чтобы было ясно: это discovery внешних browser profiles для import, а не discovery managed Storyblocks sessions.
- [ ] Убрать из текстов UI/controller слова `switch profile`, `active profile`, `storyblocks sessions`, если речь идет об импорте внешнего Chrome/Edge профиля.
- [ ] Проверить, что import всегда идет в singleton managed profile и нигде не просачивается target managed profile selection.
- [ ] Удалить или скрыть любые остаточные helper branches, которые предполагают выбор managed profile.

### 1.6. Переписать тесты под singleton contract

- [ ] Переписать `tests/test_ui_controller.py`, чтобы тесты больше не закрепляли multi-profile semantics.
- [ ] Переписать `tests/test_storyblocks_browser_core.py`, чтобы публичные вызовы шли без `profile_id`, где это уже singleton path.
- [ ] Добавить регрессию на то, что controller/session API больше не требует выбора managed profile.
- [ ] Добавить регрессию на shutdown path с native login browser после полного API cleanup.
- [ ] Добавить grep-check на отсутствие production calls `set_active/list_profiles/select_profile`.

### 1.7. Локальная верификация фазы `P6-05`

- [ ] Запустить `python -m unittest tests.test_ui_controller`.
- [ ] Запустить `python -m unittest tests.test_storyblocks_browser_core`.
- [ ] Запустить `python -m unittest tests.test_phase9_reliability`.
- [ ] Выполнить grep по `browser/`, `ui/`, `tests/` на `set_active|select_profile|list_profiles|profile_id`.
- [ ] Проверить, что smoke output по-прежнему содержит только один `storyblocks_profile_id`.

## Фаза 2. Закрыть `P6-02` и удалить старый strategy vocabulary

Цель фазы: сделать registry/catalog и runtime image order максимально прямыми, без старых групп, приоритетов и legacy strategy-флагов.

### 2.1. Дочистить runtime vocabulary

- [ ] Найти все чтения `provider_group` в runtime-коде.
- [ ] Найти все чтения `priority` в runtime-коде.
- [ ] Найти все чтения `mixed_image_fallback` в runtime-коде.
- [ ] Разделить usage на три категории:
  - active runtime behavior
  - legacy read compatibility
  - мертвый код
- [ ] Удалить все active runtime reads `provider_group`.
- [ ] Удалить все active runtime reads `priority`.
- [ ] Удалить все active runtime reads `mixed_image_fallback`.

### 2.2. Упростить `ProviderDescriptor`

- [ ] Проверить, нужен ли `provider_group` кому-либо после cleanup production callers.
- [ ] Если не нужен, удалить `provider_group` из `providers/base.py`.
- [ ] Проверить, нужен ли `priority` кому-либо после cleanup production callers.
- [ ] Если не нужен, удалить `priority` из `providers/base.py`.
- [ ] Упростить `build_default_provider_registry()` после удаления этих полей.
- [ ] Проверить, что registry остается только каталогом descriptors и capability.

### 2.3. Сузить роль `ProviderRegistry`

- [ ] Проверить, обязателен ли `resolve_concurrency_mode(...)` внутри registry.
- [ ] Если логика concurrency относится к runtime/controller, вынести ее из registry в более подходящий слой.
- [ ] Если enum `ExecutionConcurrencyMode` сохраняется, убедиться, что он не тащит обратно strategy engine semantics.
- [ ] Оставить в registry только:
  - `register()`
  - `get()`
  - `list_all()`
  - `list_by_capability()`
  - `resolve_enabled()`
  - возможно `default_image_descriptors()`, если это еще нужно и не прячет стратегию.

### 2.4. Дочистить settings/project mode compat layer

- [ ] Убрать `mixed_image_fallback` из активной сигнатуры `domain/project_modes.infer_project_mode(...)`.
- [ ] Если нужен legacy mapping, перенести его в `storage/repositories.py` как адресную compat-нормализацию перед вызовом `infer_project_mode(...)`.
- [ ] Проверить, что новый runtime path не принимает решений на основе `mixed_image_fallback`.
- [ ] Переписать тесты settings compatibility так, чтобы они проверяли safe legacy load без возврата старой модели поведения.

### 2.5. Дочистить dead code после strategy cleanup

- [ ] Удалить helper branches и комментарии, которые описывают старую strategy model.
- [ ] Удалить мертвые imports после удаления `provider_group` / `priority`.
- [ ] Удалить устаревшие тестовые ожидания про strategy vocabulary.
- [ ] Выполнить grep по `provider_group|priority|mixed_image_fallback` и зафиксировать допустимые legacy-only остатки.

### 2.6. Локальная верификация фазы `P6-02`

- [ ] Запустить `python -m unittest tests.test_image_provider_architecture`.
- [ ] Запустить `python -m unittest tests.test_media_pipeline`.
- [ ] Запустить `python -m unittest tests.test_ui_controller`.
- [ ] Выполнить grep по `providers/`, `pipeline/`, `storage/`, `ui/`, `tests/` на `provider_group|priority|mixed_image_fallback`.

## Фаза 3. Закрыть `P6-06` и отделить image path от лишней relevance/runtime-механики

Цель фазы: довести простой image path до честного и локального контракта без протаскивания video-style relevance knobs через весь runtime.

### 3.1. Провести инвентаризацию shared knobs

- [ ] Найти все использования `relevance_workers` в image path.
- [ ] Найти все использования `relevance_timeout_seconds` в image path.
- [ ] Найти все использования `early_stop_quality_threshold` в image path.
- [ ] Найти все места, где `launch_profiles.py` и `ui/controller.py` протаскивают эти значения в `MediaSelectionConfig`.
- [ ] Явно разделить, какие knobs нужны только video path, а какие реально участвуют в image path.

### 3.2. Упростить `MediaSelectionConfig`

- [ ] Подготовить целевой список image-only полей для `MediaSelectionConfig`.
- [ ] Подготовить целевой список video-only полей, которые не должны влиять на image path.
- [ ] Либо разделить конфигурацию на image/video части, либо явно маркировать поля как video-only.
- [ ] Убрать из image selection contract зависимость от `relevance_workers`.
- [ ] Убрать из image selection contract зависимость от `relevance_timeout_seconds`.
- [ ] Убрать из image selection contract зависимость от `early_stop_quality_threshold`.

### 3.3. Изолировать ranking code

- [ ] Проверить, где `_rank_candidates(...)` вызывается для video path.
- [ ] Проверить, нет ли скрытых image вызовов `_rank_candidates(...)`.
- [ ] Если ranking нужен только видео, переименовать helper так, чтобы его video-scope был очевиден.
- [ ] Если `paragraph.relevance.degraded` нужен только для видео, ограничить его emission video branch'ом.
- [ ] Удалить или сузить code paths, которые создают впечатление общего image+video relevance engine.

### 3.4. Дочистить query planner helpers

- [ ] Найти все использования `_rank_query_candidate(...)`.
- [ ] Найти все использования `_sort_query_candidates(...)`.
- [ ] Если helpers больше нужны только для video-like query logic, переименовать/переместить их так, чтобы они не выглядели как часть image path.
- [ ] Если helpers стали мертвыми после cleanup, удалить их.
- [ ] Проверить, что `_sanitize_image_queries(...)` остается единственным image-specific query path.

### 3.5. Дочистить launch profiles и controller mapping

- [ ] Проверить, обязательно ли хранить relevance knobs внутри `ResolvedLaunchProfile`.
- [ ] Если они нужны только для видео, перестать объяснять их как общую runtime-семантику launch profile.
- [ ] Проверить, что image-only runs не получают лишние relevance expectations из controller preview/runtime mapping.
- [ ] Упростить preview text и docs, если там еще остались старые намеки на скрытый relevance layer.

### 3.6. Дочистить мертвый code path и кэш-следы

- [ ] Выполнить grep по `metadata.sqlite` и убедиться, что runtime больше не создает/не использует metadata cache для image path.
- [ ] Выполнить grep по `paragraph.relevance.degraded` и проверить, что событие не относится к image path.
- [ ] Выполнить grep по `_rank_candidates|_rank_query_candidate|_sort_query_candidates` и удалить мертвые остатки после рефактора.

### 3.7. Локальная верификация фазы `P6-06`

- [ ] Запустить `python -m unittest tests.test_image_provider_architecture`.
- [ ] Запустить `python -m unittest tests.test_phase3_cache_network`.
- [ ] Запустить `python -m unittest tests.test_media_pipeline`.
- [ ] Запустить `python -m unittest tests.test_paragraph_intents`.
- [ ] Сравнить время smoke startup до/после cleanup и убедиться, что регрессии нет.

## Фаза 4. Закрыть `P6-07` и удалить внутренний locked/manual-selection state

Цель фазы: привести внутреннюю модель результатов в полное соответствие с уже упрощенным UI-контрактом.

### 4.1. Удалить `user_locked` из runtime-модели

- [ ] Найти все чтения `user_locked`.
- [ ] Найти все записи `user_locked`.
- [ ] Если новых production writes уже нет, удалить `MediaSlot.user_locked` из `domain/models.py`.
- [ ] Добавить ignore-on-load для legacy manifest payload, если старые `user_locked` еще встречаются в истории.
- [ ] Удалить serialization `user_locked` из `pipeline/media.py`.

### 4.2. Дочистить manual-selection leftovers

- [ ] Выполнить grep по `lock_asset|reject_asset|lock_selection|candidate_assets|selected_assets|awaiting_manual_decision`.
- [ ] Для каждого remaining hit определить: compat, test fixture или мертвый код.
- [ ] Удалить production code, который остался только ради старого result workbench.
- [ ] Удалить мертвые helper imports и builder branches вокруг manual-selection flow.

### 4.3. Сузить legacy compatibility

- [ ] Оставить загрузку legacy manifest со старыми status/value полями только на границе deserialization.
- [ ] Убедиться, что новые manifest больше не пишут manual-review данные.
- [ ] Убедиться, что controller/UI не читают старые manual-review поля даже в fallback path.

### 4.4. Переписать тесты под финальный result contract

- [ ] Переписать `tests/test_phase9_reliability.py` так, чтобы legacy locked payload проверялся только как read compatibility.
- [ ] Добавить тест на то, что новый manifest больше не содержит `user_locked`.
- [ ] Добавить тест на то, что result projection продолжает работать только через downloaded files и summary paths.

### 4.5. Локальная верификация фазы `P6-07`

- [ ] Запустить `python -m unittest tests.test_ui_controller`.
- [ ] Запустить `python -m unittest tests.test_media_pipeline`.
- [ ] Запустить `python -m unittest tests.test_phase9_reliability`.
- [ ] Выполнить grep по `user_locked|lock_asset|reject_asset|candidate_assets|selected_assets|awaiting_manual_decision`.

## Фаза 5. Синхронизировать документацию и удалить мертвые docs

Цель фазы: убрать расхождения между кодом и документацией, удалить битые ссылки и неиспользуемые описания удаленных feature-веток.

### 5.1. Закрыть `P6-00` doc drift

- [ ] Исправить `ui/README.md`, убрав упоминание `tk_app.py`.
- [ ] Выполнить grep по docs на `tk_app.py|DesktopTkApp|launch_tk_app|Tk fallback`.
- [ ] Исправить все найденные документы, где Tk описан как активный product path.

### 5.2. Исправить broken links и task-plan drift

- [ ] Заменить ссылки на несуществующий `p6-00-qt-only-ui-layer-spec-2026-03-21.md` в:
  - `p6-01.md`
  - `p6-03.md`
  - `p6-04-.md`
  - `p6-05.md`
  - `p6-07.md`
- [ ] Решить судьбу файла `p6-04-.md`:
  - переименовать в `p6-04.md`, если имя случайное;
  - обновить все ссылки на него;
  - проверить, что в directory listing больше нет артефактного имени с лишним дефисом.
- [ ] Проверить, нет ли других task-plan ссылок на отсутствующие файлы.

### 5.3. Решить судьбу отсутствующих user docs

- [ ] Решить, какой из двух путей выбран:
  - создать реальные `quick-start` / `user manual` / `verification checklist` документы;
  - либо удалить ссылки на них из планов и заменить на существующие docs.
- [ ] Если документы создаются, зафиксировать минимальный scope каждого документа.
- [ ] Если документы не создаются, убрать упоминания несуществующих файлов из plan/checklist документов.

### 5.4. Дочистить docs от удаленных feature-моделей

- [ ] Выполнить grep по docs на `pause|resume|checkpoint|retry failed|rerun current paragraph`.
- [ ] Выполнить grep по docs на `candidate assets|selected assets|manual decision|locked|needs review`.
- [ ] Выполнить grep по docs на `workers|queues|retry budget|relevance timeout` как user-facing UI contract.
- [ ] Выполнить grep по docs на `multiple managed profiles|switch account|active profile` в Storyblocks/session docs.
- [ ] Для каждого совпадения решить: обновить, удалить или явно пометить как legacy/historical note.

### 5.5. Удалить или архивировать неиспользуемые docs

- [ ] Составить список docs, которые больше не участвуют в текущем продукте и не нужны как maintainers-only reference.
- [ ] Для каждого документа принять одно из решений:
  - оставить как актуальный reference;
  - переписать под текущий контракт;
  - перенести в historical/archive раздел;
  - удалить, если документ полностью мертвый.
- [ ] Не удалять документы только потому, что они старые; удалять только те, у которых нет текущего читателя и нет поддержки в коде.

### 5.6. Локальная верификация doc cleanup

- [ ] Выполнить grep по `docs/` на `tk_app.py|pause|resume|candidate assets|user_locked|active profile|workers|retry budget`.
- [ ] Пройтись по `docs/optimization-checklists/task-plans/` и убедиться, что все внутренние ссылки открываются.
- [ ] Проверить, что README/UI docs не обещают уже удаленные части программы.

## Фаза 6. Почистить проект от мертвого кода и legacy-хвостов

Цель фазы: убрать из репозитория то, что уже не является частью поддерживаемой программы, либо явно пометить как legacy/out-of-scope.

### 6.1. Провести inventory root-level legacy tools

- [ ] Проверить статус `image_fetcher.py`.
- [ ] Проверить статус `video_fetcher.py`.
- [ ] Проверить статус `keyword_extractor.py`.
- [ ] Для каждого файла принять решение:
  - поддерживаемый CLI tool;
  - legacy compatibility tool;
  - кандидат на удаление.
- [ ] Если файл поддерживается, привести help/defaults/docs в соответствие текущему phase-6 contract.
- [ ] Если файл не поддерживается, добавить явную deprecation note или вынести в отдельный cleanup task на удаление.

### 6.2. Удалить ложные references на уже удаленные части программы

- [ ] Выполнить repo-wide grep по `DesktopTkApp|launch_tk_app|tk_app.py|tkinter`.
- [ ] Выполнить repo-wide grep по `bing|wikimedia|generic_web_image|allow_generic_web_image`.
- [ ] Выполнить repo-wide grep по `pause_after_current|retry_failed_only|resume_media_run|retry_failed_media_run|RunCheckpoint`.
- [ ] Выполнить repo-wide grep по `candidate_assets|selected_assets|lock_asset|reject_asset|rerun_current_paragraph`.
- [ ] Для каждого remaining hit определить, это:
  - реальный production code;
  - legacy compat layer;
  - test fixture;
  - мертвый хвост.
- [ ] Удалить все production и doc references, которые остались только как хвост уже удаленных feature-веток.

### 6.3. Удалить неиспользуемые imports, helper branches и комментарии

- [ ] После каждой фазы запускать `ruff check .` и удалять мертвые imports.
- [ ] После удаления API-поверхностей пройтись по affected файлам и убрать helper methods, оставшиеся без вызовов.
- [ ] Удалить комментарии, которые описывают уже удаленные ветки поведения.
- [ ] Удалить compatibility branches, если выяснится, что они больше не нужны ни для load, ни для tests.

### 6.4. Отдельно разобраться с `.env.example` references

- [ ] Не удалять `.env.example` из репозитория.
- [ ] Проверить, какие end-user/runtime docs еще делают вид, что `.env.example` нужен desktop пользователю.
- [ ] Убрать такие references из portable/onboarding/runtime docs.
- [ ] Для legacy CLI tools оставить `.env.example` reference только если tool реально поддерживается.
- [ ] Если root-level CLI tool больше не поддерживается, не держать `.env.example` как аргумент в его user-facing текстах.

### 6.5. Очистить naming и структуру task-plan каталога

- [ ] Убедиться, что в `docs/optimization-checklists/task-plans/` нет артефактных имен файлов.
- [ ] Привести naming task-plan файлов к единообразию.
- [ ] Убедиться, что audit и remediation документы названы предсказуемо и легко находятся grep'ом.

## Фаза 7. Финальная интеграционная проверка

### 7.1. Полный автоматический прогон

- [ ] Запустить `ruff check .`.
- [ ] Запустить `python -m unittest discover -s tests`.
- [ ] Запустить `python -m app --smoke --no-gui`.
- [ ] Запустить `python -m release_tools.portable --output-dir dist/portable-final --version phase6-final-check`.

### 7.2. Финальные grep-проверки

- [ ] Проверить отсутствие живого Tk path в коде и docs.
- [ ] Проверить отсутствие живого multi-profile API в production code.
- [ ] Проверить отсутствие runtime usage `provider_group|priority|mixed_image_fallback`.
- [ ] Проверить отсутствие новых manifest/runtime references на `user_locked`.
- [ ] Проверить отсутствие result-workbench vocabulary в активном UI/docs path.

### 7.3. Финальный ручной smoke-pass

- [ ] Запустить `python -m app`.
- [ ] Проверить, что стартует только Qt UI.
- [ ] Открыть вкладку сессии и убедиться, что в UX нет managed profile switching semantics.
- [ ] Проверить, что launch form не показывает старые technical knobs.
- [ ] Выполнить run и убедиться, что result screen показывает summary + paths + downloaded files.
- [ ] Проверить, что export/release path не тянет лишние docs и внутренние артефакты.

## Критерии завершения плана

- [ ] High-risk зоны `P6-05`, `P6-02`, `P6-06` закрыты не только по UX, но и по публичному API/runtime shape.
- [ ] `P6-07` не держит внутренний manual-selection state в новых manifest/runtime payload.
- [ ] В кодовой базе нет живых ссылок на уже удаленные feature-ветки.
- [ ] Документация совпадает с текущим behavior и не ссылается на отсутствующие файлы.
- [ ] Мертвый код, мертвые docs и ложные legacy-сигналы либо удалены, либо явно помечены как legacy и изолированы.
- [ ] Полный автоматический прогон зеленый после cleanup.
