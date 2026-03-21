# Аудит реализации phase-6 task plans

Дата проверки: 2026-03-21

## Объем проверки

- Проанализированы планы `p6-00.md` - `p6-08.md` в `docs/optimization-checklists/task-plans/`.
- Сверены код, тесты, документация и portable/release path.
- Проверены ключевые runtime-контракты, backward compatibility и остатки legacy-логики.

## Выполненные проверки

### Автоматические команды

- `ruff check .` -> OK
- `python -m unittest discover -s tests` -> OK, `159` тестов, `121.583s`
- `python -m app --smoke --no-gui` -> OK
- `python -m release_tools.portable --output-dir dist/portable-audit --version audit-20260321` -> OK

### Дополнительные замеры

- Headless smoke startup: `~2.007s`
- Portable bundle build: `~0.648s`

### Что дополнительно проверялось grep/read-аудитом

- отсутствие живого Tk runtime path;
- отсутствие `bing` / `wikimedia` / `generic_web_image` в основном desktop/runtime path;
- остатки `provider_group` / `priority` / `mixed_image_fallback`;
- остатки `pause/resume/checkpoint`;
- остатки multi-profile Storyblocks API;
- остатки quality/relevance image-логики;
- остатки result-workbench логики `lock/reject/candidate_assets`;
- соответствие portable bundle новому минимальному контракту.

## Общий вывод

Приложение находится в хорошем рабочем состоянии: линтер зеленый, полный тестовый набор зеленый, headless smoke проходит, portable bundle собирается. Основные продуктовые упрощения phase 6 реально внедрены: Tk runtime path удален, provider set сокращен, launch profiles внедрены, run lifecycle упрощен, result screen значительно сокращен, portable path приведен к узкому контракту.

При этом phase 6 нельзя считать полностью закрытым по всем task-plan'ам. Наиболее заметные незавершенные зоны:

1. `P6-02` не доведен до полного удаления старого словаря и формы данных вокруг `provider_group`, `priority`, `mixed_image_fallback`.
2. `P6-05` внедрил singleton UX, но не довел singleton contract до конца в публичном API и тестах.
3. `P6-06` заметно упростил image search path, но shared relevance/runtime knobs еще живут в общей media-конфигурации.
4. `P6-07` упростил публичный result UX, но во внутренних моделях остается часть legacy selection state.
5. Документация отстает от кода и местами ссылается на несуществующие файлы.

Итоговая оценка:

- эксплуатационная готовность приложения: высокая;
- готовность phase-6 cleanup как полностью завершенного пакета: средняя;
- архитектурная чистота после phase 6: еще не доведена до конца.

## Статус по планам

| План | Статус | Краткий вывод |
| --- | --- | --- |
| `P6-00` | Частично реализовано | Qt-only runtime внедрен, но docs cleanup не завершен |
| `P6-01` | Реализовано | provider allowlist и legacy normalization доведены до рабочего состояния |
| `P6-02` | Частично реализовано | прямой image path есть, но старая strategy vocabulary еще жива |
| `P6-03` | Реализовано с пробелом по документации | launch profiles и compact UI сделаны, целевые user docs отсутствуют |
| `P6-04` | Реализовано | run lifecycle упрощен и хорошо покрыт тестами |
| `P6-05` | Частично реализовано | singleton UX есть, но multi-profile API все еще публичен |
| `P6-06` | Частично реализовано | query planner/filtering сильно упрощены, shared relevance plumbing еще не добит |
| `P6-07` | Частично реализовано | новый result UX есть, но internal locked-state еще не удален полностью |
| `P6-08` | Реализовано | portable bundle и release tests приведены к узкому контракту |

## Критичные риски

На момент проверки критичных блокеров, которые делают приложение неработоспособным или ломают базовый startup/runtime contract, не обнаружено.

Основания для такой оценки:

- `ruff` зеленый;
- полный test suite зеленый;
- `python -m app --smoke --no-gui` зеленый;
- portable bundle реально собирается;
- живой Tk runtime path в Python-коде не найден.

## Высокие риски

### 1. `P6-05` не доведен до полного singleton contract

Статус: высокий риск архитектурного отката и повторного разрастания session/profile слоя.

Доказательства:

- `browser/profiles.py:51` - публичный `list_profiles()` все еще жив.
- `browser/profiles.py:115` - `rename_profile()` все еще жив.
- `browser/profiles.py:120` - `delete_profile()` все еще жив.
- `browser/profiles.py:132` - `set_active()` все еще жив.
- `browser/profiles.py:145` - `select_profile()` все еще жив.
- `browser/session.py:130` и далее - публичные методы все еще принимают `profile_id`.
- `ui/controller.py:814` - `discover_storyblocks_sessions()` все еще часть controller API.
- `tests/test_ui_controller.py:601` - тесты все еще закрепляют discovery flow.
- `tests/test_storyblocks_browser_core.py:761` - в тестах по-прежнему используется явный `profile_id`.

Что уже сделано хорошо:

- `BrowserProfileRegistry.get_or_create_singleton()` есть в `browser/profiles.py:66`.
- smoke snapshot показывает один `storyblocks_profile_id` через `app/runtime.py:32` и `app/__main__.py:20`.
- shutdown improved: `app/bootstrap.py:68` и `browser/session.py:518` закрывают native login browser.

Риск:

- UX уже singleton, но публичный API приложения еще формально multi-profile.
- Любая следующая доработка легко вернет старую ментальную модель обратно.
- План `P6-05` по факту закрыт только наполовину: поверхностный UX стал проще, но публичная API-поверхность - нет.

Рекомендация:

- завершить singleton cleanup в `browser/profiles.py`, `browser/session.py`, `ui/controller.py`, `tests/test_ui_controller.py`, `tests/test_storyblocks_browser_core.py`;
- убрать из production flow и тестов `set_active/list_profiles/select_profile/profile_id` там, где больше нет продуктового смысла.

### 2. `P6-02` не завершил cleanup старого strategy-словаря

Статус: высокий риск скрытой сложности и поддерживаемости.

Доказательства:

- прямой image path реально внедрен в `pipeline/media.py:2006` - `pipeline/media.py:2116`.
- но `providers/base.py:18` и `providers/base.py:19` все еще содержат `provider_group` и `priority`.
- `providers/registry.py:86` все еще держит `resolve_concurrency_mode(...)` как центральный resolver.
- `providers/registry.py:153`, `providers/registry.py:170`, `providers/registry.py:188`, `providers/registry.py:199`, `providers/registry.py:210` - descriptors все еще размечены старыми группами.
- `domain/project_modes.py:94` и `domain/project_modes.py:107` все еще знают про `mixed_image_fallback`.
- `storage/repositories.py:141` продолжает читать `mixed_image_fallback` из legacy payload.

Что уже сделано хорошо:

- порядок image paths читается из `pipeline/media.py:2081` - `pipeline/media.py:2116`.
- порядок free providers сохраняется тестом `tests/test_image_provider_architecture.py:130` - `tests/test_image_provider_architecture.py:146`.
- mixed path защищен тестами `tests/test_media_pipeline.py`.

Риск:

- поведение стало проще, но форма данных и словарь понятий остались старыми.
- код продолжает выглядеть сложнее, чем реально нужно продукту.
- дальнейшие изменения снова будут тянуть старую стратегическую модель в runtime.

Рекомендация:

- довести `P6-02` до удаления или полной деактивации `provider_group`, `priority`, `mixed_image_fallback` из production semantics;
- оставить legacy read только в узком compat layer, а не в основной runtime-форме.

### 3. `P6-06` не полностью изолировал image path от общей relevance/runtime-механики

Статус: высокий риск с точки зрения остаточной сложности и не до конца реализованного cleanup по производительности.

Доказательства:

- хорошо упрощены `providers/images/querying.py:16` - `providers/images/querying.py:29`.
- хорошо упрощены `providers/images/filtering.py:17` - `providers/images/filtering.py:41`.
- `providers/images/service.py:103` использует только `search_results.sqlite`.
- тесты проверяют отсутствие `metadata.sqlite`: `tests/test_image_provider_architecture.py:148` - `tests/test_image_provider_architecture.py:202`.
- но `pipeline/media.py:169` - `pipeline/media.py:218` все еще хранит `relevance_workers`, `bounded_relevance_queue`, `relevance_timeout_seconds`, `early_stop_quality_threshold` в общей `MediaSelectionConfig`.
- `pipeline/media.py:2368` - `pipeline/media.py:2489` все еще содержит `_rank_candidates(...)` и `paragraph.relevance.degraded` event path.
- `pipeline/intents.py:363` - `pipeline/intents.py:581` все еще содержит тяжелые query-ranking helpers, пусть и уже не на основном image path.
- `ui/launch_profiles.py:23` - `ui/launch_profiles.py:31` и `ui/controller.py:1457` - `ui/controller.py:1478` продолжают протаскивать relevance/quality knobs через launch profile/runtime mapping.

Что уже сделано хорошо:

- image filtering стал license-only;
- image provider query rewrite практически убран;
- image selection в `_prepare_provider_result(...)` не ранжирует image candidates (`pipeline/media.py:2316` - `pipeline/media.py:2326`).

Риск:

- поведение image path уже проще, но кодовая поверхность все еще тяжелее, чем обещает `P6-06`.
- прирост производительности частично достигнут, но не доведен до явного и локального image-only contract.

Рекомендация:

- отделить video-only relevance knobs от image-only runtime-формы;
- довести `MediaSelectionConfig` и launch profiles до явного разделения `video relevance` vs `simple image search`.

## Средние риски

### 1. `P6-07` упростил публичный result UX, но внутренний locked-state еще не дочищен

Доказательства:

- новый flat result projection уже есть: `ui/contracts.py:64` - `ui/contracts.py:89`.
- summary с путями в manifest уже есть: `pipeline/media.py:1738` - `pipeline/media.py:1783`.
- Qt result screen реально показывает paths и downloaded files: `ui/qt_app.py:485` - `ui/qt_app.py:494`.
- legacy normalization уже реализована: `ui/controller.py:2145` - `ui/controller.py:2165`.
- но `domain/models.py:192` все еще содержит `MediaSlot.user_locked`.
- `pipeline/media.py:3096` продолжает сериализовать `user_locked`.
- тесты продолжают покрывать legacy locked payload в compat path: `tests/test_phase9_reliability.py`.

Риск:

- пользовательский UX уже почти полностью очищен, но внутренняя model/persistence-форма еще не до конца совпадает с новым контрактом;
- это не ломает продукт сейчас, но увеличивает риск будущих недоразумений и half-cleanup состояния.

### 2. `P6-00` code cleanup выполнен, но documentation/task-plan cleanup отстает

Доказательства:

- Qt-only runtime есть в `ui/__init__.py:28` - `ui/__init__.py:43`.
- `SUPPORTED_DESKTOP_STACK = "pyside6"` зафиксирован в `config/settings.py:6` и `config/settings.py:107`.
- smoke path возвращает только Qt-oriented snapshot через `app/__main__.py:20` - `app/__main__.py:30`.
- при этом `ui/README.md:4` все еще говорит, что `qt_app.py` и `tk_app.py` сосуществуют.
- `p6-01.md:5`, `p6-03.md:5`, `p6-04-.md:5`, `p6-05.md:5`, `p6-07.md:6` ссылаются на несуществующий `p6-00-qt-only-ui-layer-spec-2026-03-21.md`.
- сам файл плана назван `p6-04-.md`, что выглядит как случайный leftover filename.

Риск:

- продуктовый и архитектурный контракт в коде уже другой, но документация phase 6 продолжает вводить в заблуждение;
- это не runtime blocker, но плохой ориентир для следующих изменений.

### 3. `P6-03` по коду реализован хорошо, но документационная часть плана не исполнена

Доказательства:

- `UiQuickLaunchSettingsViewModel.launch_profile_id` есть в `ui/contracts.py:171` - `ui/contracts.py:185`.
- `UiAdvancedSettingsViewModel` сокращен до 4 timing fields в `ui/contracts.py:188` - `ui/contracts.py:193`.
- общий resolver есть в `ui/launch_profiles.py`.
- compact preset snapshot есть в `ui/controller.py:1410` - `ui/controller.py:1436`.
- но целевые документы из плана отсутствуют в repo: не найдены `docs/quick-start-ru.md`, `docs/user-manual-ru.md`, `docs/implementation-verification-checklist.md`.

Риск:

- UI и presets уже перешли на новый контракт, а внешние инструкции под него не оформлены так, как обещано в плане.

### 4. Ручная интерактивная Qt/Storyblocks-проверка не была полностью воспроизведена в этой сессии

Что удалось подтвердить:

- headless startup contract подтвержден командой `python -m app --smoke --no-gui`;
- session/browser flow хорошо покрыт тестами `tests/test_storyblocks_browser_core.py` и `tests/test_ui_controller.py`;
- shutdown native browser подтвержден тестом `tests/test_storyblocks_browser_core.py:741` - `tests/test_storyblocks_browser_core.py:768`.

Что не было выполнено вручную в интерактивном GUI:

- реальный login в Storyblocks через живой браузер;
- ручной проход полного GUI сценария с кликами в Qt.

Риск:

- это не кодовый дефект, а ограничение глубины live-верификации в текущей CLI-сессии;
- readiness высокая, но не абсолютная release-signoff без ручного operator pass.

## Низкие риски

### 1. Legacy data-shape местами остается в compat path сознательно или полуосознанно

Примеры:

- `desktop_stack` продолжает жить как legacy field, но жестко нормализуется к `pyside6` (`config/settings.py:96` - `config/settings.py:107`, `storage/repositories.py:162`, `storage/repositories.py:279`).
- `Run.from_dict()` продолжает читать legacy payload и выкидывает `checkpoint` (`domain/models.py:297` - `domain/models.py:308`).
- `BrowserProfile.is_active` все еще живет в модели (`domain/models.py:278`).

Риск:

- сейчас это скорее технический долг, чем operational issue.

### 2. `video_fetcher.py` хранит старый legacy default source set

Доказательства:

- `video_fetcher.py:87` -> `DEFAULT_SOURCES = "pexels,pixabay,wikimedia"`.

Комментарий:

- это не ломает основной desktop runtime phase 6;
- сам `P6-01` прямо разрешал не трогать `video_fetcher.py`;
- но как low-risk inconsistency для сопровождающих это отметить стоит.

### 3. Root-level legacy utilities все еще ссылаются на `.env.example`

Примеры:

- `image_fetcher.py`
- `keyword_extractor.py`
- `video_fetcher.py`

Комментарий:

- portable bundle это не ломает: builder их не ship'ит и `docs/phase-10/onboarding.md` уже не требует `.env.example`;
- риск низкий и относится скорее к legacy CLI tooling, а не к main desktop path.

## Детальная верификация по каждому плану

### `P6-00` - удаление Tk UI слоя

Вердикт: частично реализовано.

Подтверждено:

- `ui/__init__.py:28` - `ui/__init__.py:39` грузит только `ui.qt_app` и делает явную ошибку при отсутствии `PySide6`.
- `config/settings.py:6` и `config/settings.py:107` фиксируют `pyside6` как единственный desktop stack.
- `storage/repositories.py:95` - `storage/repositories.py:163` читают legacy settings, но не возвращают runtime к Tk.
- Python grep по `DesktopTkApp|launch_tk_app|tkinter|tk_app.py` в runtime-коде дал пустой результат.
- `tests/test_phase2_architecture.py` закрепляет Qt-only startup policy.

Не завершено:

- `ui/README.md:4` устарел и прямо противоречит текущему коду.
- несколько планов phase 6 ссылаются на отсутствующий prerequisite file.

### `P6-01` - упрощение provider-системы

Вердикт: реализовано.

Подтверждено:

- allowlist в `storage/repositories.py:31` - `storage/repositories.py:48`.
- defaults в `config/settings.py:54` - `config/settings.py:69`.
- registry ограничен 5 провайдерами в `providers/registry.py:146` - `providers/registry.py:216`.
- free-image allowlist в `domain/project_modes.py:5` - `domain/project_modes.py:10`.
- query bundle больше не содержит `generic_web_image` в `pipeline/intents.py:951` - `pipeline/intents.py:977`.
- UI free providers сокращены до `Pexels`, `Pixabay`, `Openverse` в `ui/qt_app.py:335` - `ui/qt_app.py:350`.
- legacy normalization подтверждена тестом `tests/test_image_provider_architecture.py:204` - `tests/test_image_provider_architecture.py:257`.

Остаточный нюанс:

- legacy standalone `video_fetcher.py` имеет старые defaults, но это в плане явно было допустимо как out-of-scope.

### `P6-02` - упрощение provider order/strategy logic

Вердикт: частично реализовано.

Подтверждено:

- image search paths теперь читаются из одного места: `pipeline/media.py:2081` - `pipeline/media.py:2116`.
- explicit Storyblocks-first contract зафиксирован прямо в коде (`pipeline/media.py:2099` - `pipeline/media.py:2103`).
- free provider order сохраняется без пересортировки (`tests/test_image_provider_architecture.py:130` - `tests/test_image_provider_architecture.py:146`).

Не завершено:

- `ProviderDescriptor` все еще несет `provider_group` и `priority`.
- `mixed_image_fallback` продолжает фигурировать в legacy inference.
- concurrency/registry слой все еще выглядит strategy-shaped.

### `P6-03` - упрощение настроек интерфейса

Вердикт: реализовано по коду, частично не закрыто по документации.

Подтверждено:

- compact form contracts в `ui/contracts.py`.
- shared launch-profile resolver в `ui/launch_profiles.py`.
- controller mapping через resolved profile в `ui/controller.py:1245` - `ui/controller.py:1480`.
- Qt selector `Профиль запуска` и conditional `Custom` block в `ui/qt_app.py:141` - `ui/qt_app.py:187`, `ui/qt_app.py:374` - `ui/qt_app.py:382`.
- preset compat и round-trip защищены тестами `tests/test_ui_controller.py:732` - `tests/test_ui_controller.py:815`.

Не завершено:

- целевые user docs из плана отсутствуют в репозитории.

### `P6-04` - упрощение run lifecycle

Вердикт: реализовано.

Подтверждено:

- `RunStatus` сокращен до `running/completed/failed/cancelled` в `domain/enums.py:18` - `domain/enums.py:22`.
- legacy paused/ready/draft нормализуются в `domain/models.py:297` - `domain/models.py:308`.
- polling terminal set упрощен в `ui/polling.py:5`.
- runtime API дает `rerun_full_media_run(...)` в `app/runtime.py:277` - `app/runtime.py:288`.
- pause/resume/public retry API в рабочем коде больше нет.
- тесты зеленые, включая reliability coverage.

### `P6-05` - упрощение browser/session subsystem

Вердикт: частично реализовано.

Подтверждено:

- singleton profile entrypoint есть.
- session tab в Qt уже переведен на singleton-style UX.
- import идет в singleton managed profile (`browser/profile_import.py:141` - `browser/profile_import.py:186`).
- shutdown native login browser реализован.

Не завершено:

- multi-profile public API все еще существует;
- `profile_id` все еще протекает через session manager;
- discovery flow все еще живет в controller и тестах.

### `P6-06` - максимальное упрощение image search

Вердикт: частично реализовано.

Подтверждено:

- provider-specific rewrite практически убран (`providers/images/querying.py`).
- quality-prefilter/metadata cache path убран (`providers/images/filtering.py`, `providers/images/service.py`).
- image query sanitization выделена в `_sanitize_image_queries(...)` (`pipeline/intents.py:584` - `pipeline/intents.py:619`).
- `provider_queries` для image bucket'ов упрощены (`pipeline/intents.py:951` - `pipeline/intents.py:977`).

Не завершено:

- shared media config все еще тащит image-adjacent relevance knobs;
- общий ranking code все еще жив в `pipeline/media.py`;
- heavy query-ranking helpers еще присутствуют в `pipeline/intents.py`.

### `P6-07` - упрощение result/workbench flow

Вердикт: частично реализовано.

Подтверждено:

- новый downloaded-files projection есть.
- result UI ориентирован на `downloads_root`, `videos_dir`, `images_dir` и фактически сохраненные файлы.
- legacy paragraph statuses нормализуются.

Не завершено:

- `user_locked` еще остается во внутренних моделях и сериализации.

### `P6-08` - сокращение portable/release слоя

Вердикт: реализовано.

Подтверждено:

- builder allowlist в `release_tools/portable.py:11` - `release_tools/portable.py:35`.
- ship only `docs/phase-10/onboarding.md` в `release_tools/portable.py:26`.
- manifest отражает новый узкий контракт в `release_tools/portable.py:210` - `release_tools/portable.py:232`.
- release tests соответствуют новому контракту (`tests/test_phase10_release.py`, `tests/test_release_tools_portable.py`).
- собранный bundle содержит корректный `portable_manifest.json` и не тянет лишние release docs.

## Оценка работоспособности, качества и производительности

### Работоспособность

- Базовый startup contract исправен.
- Headless smoke проходит и возвращает ожидаемый snapshot:
  - `workspace_root`
  - `providers`
  - `storyblocks_profile_id`
- Полный test suite проходит без ошибок.

### Качество

- Regression coverage выглядит хорошей для phase-6 shape: есть тесты на settings normalization, run compat, singleton session shutdown, result projection и portable bundle.
- Код в самых важных направлениях уже стал существенно проще, чем был до phase 6.
- Главная проблема качества сейчас не падающий runtime, а недочищенный architectural debt.

### Производительность

Позитивные сигналы:

- headless smoke startup около `2s` - это хороший показатель для desktop bootstrap без GUI;
- portable bundle собирается очень быстро (`~0.65s`);
- image path действительно стал легче за счет удаления metadata cache и quality-prefilter.

Ограничения оценки:

- в репозитории нет полноценного benchmark suite, поэтому точную продуктовую performance delta по phase 6 посчитать нельзя;
- часть heavy relevance/runtime plumbing еще жива в shared media path, поэтому потенциал дальнейшего упрощения все еще есть.

## Финальная оценка готовности

### Готово к использованию уже сейчас

- приложение в целом запускаемо и стабильно по автоматическим проверкам;
- phase-6 shape уже хорошо читается в runtime;
- portable delivery path рабочий;
- основные пользовательские улучшения реально видны в коде и тестах.

### Что мешает считать phase 6 полностью завершенной

- не добит `P6-05` singleton cleanup;
- не добит `P6-02` strategy vocabulary cleanup;
- не добит `P6-06` final image/runtime cleanup;
- не добит `P6-07` internal locked-state cleanup;
- docs phase 6 отстают от кода.

## Рекомендуемый порядок добивки

1. Закрыть `P6-05` до конца: singleton API, controller, tests.
2. Закрыть `P6-02`: убрать остаточную strategy vocabulary из runtime shape.
3. Закрыть `P6-06`: отделить video relevance knobs от image path.
4. Закрыть `P6-07`: убрать `user_locked` и остатки internal manual-selection state.
5. Синхронизировать docs/task-plans с фактическим состоянием кода.
