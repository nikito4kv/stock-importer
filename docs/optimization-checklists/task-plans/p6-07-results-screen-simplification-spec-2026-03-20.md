# P6-07. ТЗ на упрощение экрана результатов и ручного разбора

Исходный пункт плана:
[phase-6-simplification-checklist.md](./phase-6-simplification-checklist.md)

Предварительное условие: [p6-00-qt-only-ui-layer-spec-2026-03-21.md](./p6-00-qt-only-ui-layer-spec-2026-03-21.md) уже выполнен, Tk-слой удален.

## Цель

Свести результат прогона к короткому и понятному контракту:

- общий статус run;
- агрегированные счетчики по абзацам;
- явный путь к папке выгрузки;
- простой список абзацев с итоговым статусом;
- список реально сохраненных файлов для выбранного абзаца.

После P6-07 пользователь должен проверять итог прежде всего через файловую папку
`downloads/`, а не через встроенный mini-workbench для выбора кандидатов.

Из продукта и кода нужно убрать или резко сократить все, что существует только
ради ручного разбора уже найденных ассетов:

- `lock_asset`;
- `reject_asset`;
- `lock_selection`;
- ручной `rerun_current_paragraph` из экрана результатов;
- экран со списками `selected assets` и `candidate assets`;
- отдельные UI-статусы `locked`, `needs_review`, `partial_success` как
  публичный результатный контракт;
- live-state с кандидатами и ручными decision-флагами;
- manifest-поля, нужные только для ручного закрепления, отклонения и разбора.

Главная продуктовая идея P6-07:

- UI показывает, что run завершился и куда смотреть;
- пользователь открывает папку выгрузки и смотрит реальные файлы;
- если результат не устраивает, он запускает новый полный run или заранее
  правит запросы до старта, а не разбирает кандидатов внутри result screen.

## Границы задачи

Входит в P6-07:

- упрощение публичного результатного контракта для `manifest`, `live snapshot` и
  `UiStateViewModel`;
- упрощение result/workbench UI в `Qt`;
- удаление ручных действий `lock/reject/rerun current paragraph` из result flow;
- перевод основного result UX на пути к скачанным файлам и папке выгрузки;
- сокращение summary/status vocabulary до небольшого набора понятных состояний;
- переписывание тестов, которые закрепляют старую result/workbench-модель.

Не входит в P6-07:

- редактирование paragraph intent/query до старта run; это остается отдельной
  функцией и не относится к экрану результата;
- изменение алгоритма выбора медиа внутри пайплайна; это уже P6-06 и соседние
  пункты;
- изменение структуры папки выгрузки `downloads/videos` и `downloads/images`;
- удаление log export как отдельной функции, если он нужен для отладки;
- полная переделка run lifecycle; это уже P6-04.

Практическое решение по scope:

- не изобретать новый сложный storage-формат, если уже хватает
  `AssetCandidate.local_path`;
- сначала ввести простой result projection для UI, потом удалять мертвые поля;
- считать папку выгрузки главным источником истины по результату run;
- если cross-platform action "Открыть папку" окажется дорогим, минимальный
  обязательный результат все равно должен включать явный абсолютный путь к
  папке выгрузки и к подпапкам `videos` / `images`.

## Где сейчас сидит лишняя логика

- Persisted result model и live snapshots:
  [domain/models.py](../../domain/models.py)
- Публичные re-export доменных моделей:
  [domain/__init__.py](../../domain/__init__.py)
- Ручная фиксация selection, `awaiting_manual_decision`, live-state и summary:
  [pipeline/media.py](../../pipeline/media.py)
- Runtime API для ручного закрепления selection:
  [app/runtime.py](../../app/runtime.py)
- UI view-models для workbench, candidate assets и journal:
  [ui/contracts.py](../../ui/contracts.py)
- Controller-сборка result state, manual actions и run log:
  [ui/controller.py](../../ui/controller.py)
- Текстовые метки paragraph/decision status:
  [ui/presentation.py](../../ui/presentation.py)
- Qt result screen, candidate lists и ручные обработчики:
  [ui/qt_app.py](../../ui/qt_app.py)

Основные тесты, которые задача точно затронет:

- [tests/test_ui_controller.py](../../tests/test_ui_controller.py)
- [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py)
- [tests/test_phase9_reliability.py](../../tests/test_phase9_reliability.py)

## Целевой результатный контракт

После P6-07 публичный UI/result contract должен выглядеть так:

- run-status: `running`, `cancelled`, `completed`, `failed`;
- paragraph-status: `pending`, `processing`, `completed`, `no_match`, `failed`,
  `skipped`;
- run summary:
  - `paragraphs_total`
  - `paragraphs_processed`
  - `paragraphs_completed`
  - `paragraphs_no_match`
  - `paragraphs_failed`
  - `downloads_root`
  - `videos_dir`
  - `images_dir`
  - `downloaded_video_files`
  - `downloaded_image_files`
- paragraph result:
  - номер абзаца;
  - простой итоговый статус;
  - при необходимости короткая note/message;
  - список реально сохраненных файлов с `local_path`.

Что не должно быть частью нового публичного contract:

- `user_decision_status`;
- `candidate_assets`;
- `selected_assets` как отдельный chooser-state;
- `fallback_options`;
- `lock/reject` actions;
- отдельный result UX вокруг `locked` и `needs_review`;
- event journal как основной способ понять результат.

## Definition of Done

- В result UI больше нет ручных действий закрепления, отклонения и
  повторного запуска отдельного абзаца.
- Экран результата в `Qt` показывает путь к папке выгрузки и списки
  реально сохраненных файлов, а не кандидатов для выбора.
- `RunManifest.summary` содержит пути к `downloads_root`, `videos_dir`,
  `images_dir` и простые итоговые счетчики.
- Публичный paragraph-result contract использует только упрощенные статусы
  `pending/processing/completed/no_match/failed/skipped`.
- Legacy manifest со статусами `locked`, `partial_success`, `needs_review`
  загружается без падения и нормализуется для нового UI.
- В новых result view-model нет полей `user_decision_status`,
  `candidate_assets`, `selected_assets`, `event_journal`.
- Поиск по коду не находит живых публичных entry points `lock_asset`,
  `reject_asset`, `lock_selection`, `rerun_current_paragraph` в result flow.
- Тесты и линтер проходят после cleanup.

## Пошаговый план

### Шаг 1. Зафиксировать новый продуктовый контракт экрана результатов

Что сделать:

- Явно принять, что экран результатов больше не является интерфейсом ручного
  выбора ассетов.
- Явно принять, что пользователь проверяет итог через файловую папку, а UI
  только показывает статус, счетчики и пути.
- Зафиксировать, что ручные действия `lock/reject/rerun current paragraph` не
  являются частью нового result UX.
- Зафиксировать минимальный состав result UI:
  - статус run;
  - summary counters;
  - абсолютный путь к папке выгрузки;
  - по выбранному абзацу список сохраненных файлов.

Файлы:

- [docs/optimization-checklists/phase-6-simplification-checklist.md](./phase-6-simplification-checklist.md)
- [ui/controller.py](../../ui/controller.py)
- [ui/qt_app.py](../../ui/qt_app.py)

Что должно получиться:

- У команды не остается двусмысленности, нужно ли сохранять ручной result
  workbench "на всякий случай".
- Все следующие изменения делаются под один короткий UX-контракт.

Как проверить:

- В описании задачи и review-комментариях больше не фигурирует формулировка
  "пользователь должен выбрать лучший ассет прямо в UI".
- Основной happy path результата можно описать без слов `candidate`,
  `locked`, `reject`, `manual decision`.

Практический совет:

- Не начинать с удаления виджетов. Если сначала снести кнопки, а потом оставить
  живыми controller/runtime API, получится скрытая мертвая сложность.

### Шаг 2. Упростить публичный vocabulary статусов и summary

Что сделать:

- В [ui/presentation.py](../../ui/presentation.py) заменить публичную модель
  paragraph-status на короткий набор:
  - `pending`
  - `processing`
  - `completed`
  - `no_match`
  - `failed`
  - `skipped`
- Убрать из публичных label maps значения:
  - `locked`
  - `partial_success`
  - `needs_review`
- В [ui/controller.py](../../ui/controller.py) добавить нормализацию legacy
  paragraph-status перед сборкой UI:
  - `selected` -> `completed`
  - `locked` -> `completed`
  - `partial_success` -> `completed`
  - `needs_review` -> `failed`, если у абзаца нет сохраненных файлов
  - `needs_review` -> `completed`, если legacy manifest уже содержит сохраненные
    файлы
- В [pipeline/media.py](../../pipeline/media.py) упростить `update_summary(...)`
  до пользовательских счетчиков результата, а не счетчиков внутреннего выбора.
- В [ui/contracts.py](../../ui/contracts.py) переименовать
  `paragraphs_matched` в более продуктовый счетчик, например
  `paragraphs_completed`.

Файлы:

- [pipeline/media.py](../../pipeline/media.py)
- [ui/contracts.py](../../ui/contracts.py)
- [ui/controller.py](../../ui/controller.py)
- [ui/presentation.py](../../ui/presentation.py)

Что должно получиться:

- Публичный result UI больше не разговаривает терминами внутреннего селектора.
- Пользователь видит только понятные статусы результата, а не следы старой
  ручной state machine.

Как проверить:

- Поиск по source-коду UI не находит `translate_decision_status(...)` в result
  flow.
- В `Qt` больше не отображаются статусы `locked`, `needs_review`,
  `partial_success`.
- Новый run после завершения показывает только `completed/no_match/failed`
  для paragraph items.

Практический совет:

- Это правильное место для legacy-нормализации. Не нужно хранить в UI знание о
  старых статусах дольше, чем нужно для чтения существующих manifest-файлов.

### Шаг 3. Сделать папку выгрузки явной частью result contract

Что сделать:

- В [pipeline/media.py](../../pipeline/media.py) при построении
  `RunManifest.summary` добавить:
  - `downloads_root`
  - `videos_dir`
  - `images_dir`
  - `downloaded_video_files`
  - `downloaded_image_files`
- Использовать уже существующую логику `_run_download_root(...)`,
  `_shared_video_output_dir(...)`, `_shared_image_output_dir(...)`, а не
  дублировать вычисление путей в UI.
- В [ui/contracts.py](../../ui/contracts.py) добавить поля путей в
  `UiRunProgressViewModel` или отдельный компактный result summary VM.
- В [ui/controller.py](../../ui/controller.py) пробросить эти пути из manifest в
  UI state.

Файлы:

- [pipeline/media.py](../../pipeline/media.py)
- [ui/contracts.py](../../ui/contracts.py)
- [ui/controller.py](../../ui/controller.py)

Что должно получиться:

- Result UI умеет без вычислений из формы показать, где лежат реальные файлы.
- Источник истины о папке результата хранится рядом с manifest summary.

Как проверить:

- После завершения run `manifest.summary` содержит абсолютный путь к
  `downloads_root`.
- `UiRunProgressViewModel` или новый result summary VM содержит те же пути.
- Пользователь видит эти пути без открытия настроек запуска.

Практический совет:

- Не вычислять result path из текущей формы UI. Истина должна браться из
  завершенного run/manifest, иначе история старых run будет показывать
  неправильные пути.

### Шаг 4. Ввести плоскую проекцию "сохраненные файлы" вместо candidate/selected chooser

Что сделать:

- В [ui/contracts.py](../../ui/contracts.py) ввести отдельный простой VM для
  файла результата, например `UiDownloadedFileItem`, с полями:
  - `asset_id`
  - `provider_name`
  - `kind`
  - `role`
  - `title`
  - `local_path`
  - `exists`
- В `UiParagraphWorkbenchItem` заменить:
  - `user_decision_status`
  - `selected_assets`
  - `candidate_assets`
  - `rejection_reasons` как главный result payload
  на короткий result payload:
  - `status`
  - `result_note`
  - `downloaded_files`
- В [ui/controller.py](../../ui/controller.py) добавить один helper,
  который из persisted paragraph result собирает плоский список файлов с
  `local_path`, и использовать только его.
- Не расширять старый `UiAssetPreview` новыми костылями. Текущий тип
  candidate-centric и плохо подходит для result screen.

Файлы:

- [ui/contracts.py](../../ui/contracts.py)
- [ui/controller.py](../../ui/controller.py)

Что должно получиться:

- Result UI работает с файлами результата, а не с preview-моделью кандидатов.
- У каждого элемента в UI есть явный `local_path`.

Как проверить:

- В source-коде `Qt` result screen больше не нужен доступ к `candidate_assets`.
- `UiParagraphWorkbenchItem` больше не содержит `user_decision_status`.
- По выбранному абзацу UI показывает список файлов с абсолютными путями.

Практический совет:

- Это самый безопасный путь. Сначала поменять UI projection, а только потом
  удалять низкоуровневые поля из manifest/live-state.

### Шаг 5. Убрать ручные result-actions из runtime, controller и pipeline

Что сделать:

- В [app/runtime.py](../../app/runtime.py) удалить публичный метод
  `lock_paragraph_selection(...)`.
- В [pipeline/media.py](../../pipeline/media.py) удалить или сделать internal-only
  и затем убрать:
  - `lock_selection(...)`
  - special-case `paragraph.awaiting_manual_decision`
  - ветки, где `entry.user_decision_status == "locked"` влияет на runtime
- В [ui/controller.py](../../ui/controller.py) удалить:
  - `lock_asset(...)`
  - `reject_asset(...)`
  - `rerun_current_paragraph(...)`
  - `rerun_current_paragraph_async(...)`
  - `rerun_selected_paragraphs(...)`
  - `rerun_selected_paragraphs_async(...)`
- Если P6-04 уже внедрен и часть этих API уже исчезла, использовать этот шаг как
  grep-based cleanup остаточных следов.

Файлы:

- [app/runtime.py](../../app/runtime.py)
- [pipeline/media.py](../../pipeline/media.py)
- [ui/controller.py](../../ui/controller.py)

Что должно получиться:

- Result screen больше не имеет скрытого application API для ручного разбора.
- В manifest/runtime больше нет жизненно важных веток вокруг `locked`.

Как проверить:

- Поиск по коду не находит публичных методов `lock_asset`, `reject_asset`,
  `lock_paragraph_selection`, `rerun_current_paragraph`.
- В event stream больше не появляется `paragraph.awaiting_manual_decision`.
- Завершенный run не требует manual action, чтобы считаться понятным
  пользователю.

Практический совет:

- Не смешивать это с удалением query-editing до старта run.
  `update_paragraph_queries(...)` остается отдельной и полезной функцией.

### Шаг 6. Сократить manifest и live snapshot под новый result contract

Что сделать:

- В [domain/models.py](../../domain/models.py) удалить или вывести из публичного
  result contract поля, которые нужны только старому ручному workbench:
  - `fallback_options`
  - `user_decision_status`
  - `LiveAssetSnapshot`
  - `selected_assets` и `candidate_assets` внутри live snapshot
- Если полное удаление сразу слишком рискованно, делать в два подшага:
  - сначала перестать читать их из UI/controller;
  - потом удалить dataclass-поля и builder-логику.
- В [pipeline/media.py](../../pipeline/media.py) переписать
  `_build_live_run_state_snapshot(...)` так, чтобы live state содержал только
  компактные paragraph statuses и, при необходимости, уже сохраненные файлы.
- Проверить `SerializableModel.from_dict(...)` в
  [domain/models.py](../../domain/models.py): старые manifest-файлы с лишними
  полями должны продолжать читаться без отдельной миграции.

Файлы:

- [domain/models.py](../../domain/models.py)
- [domain/__init__.py](../../domain/__init__.py)
- [pipeline/media.py](../../pipeline/media.py)

Что должно получиться:

- Persisted manifest и live snapshot становятся заметно короче.
- UI больше не зависит от runtime candidate-state.
- Legacy manifest продолжает загружаться, а новые manifest не пишут лишние
  поля старого ручного workbench.

Как проверить:

- Поиск по коду не находит новых чтений `fallback_options`,
  `user_decision_status`, `candidate_assets`.
- Старый manifest с этими полями загружается через `from_dict()` без падения.
- Новый manifest после run не содержит data, нужную только для lock/reject flow.

Практический совет:

- Удалять поля из dataclass безопасно только после того, как UI перестал их
  читать. Иначе получится половинчатый refactor с трудноуловимыми падениями.

### Шаг 7. Переписать controller build-state под компактный result flow

Что сделать:

- В [ui/controller.py](../../ui/controller.py) переписать:
  - `build_state(...)`
  - `build_live_snapshot(...)`
  - `build_live_run_state(...)`
  - `build_paragraph_workbench(...)`
  - `build_run_progress(...)`
- Убрать из этих методов сборку:
  - `event_journal` как обязательного result payload;
  - `candidate_assets`;
  - `selected_assets`;
  - `user_decision_status`.
- Оставить только:
  - run summary;
  - compact paragraph items;
  - downloaded-file list для выбранного абзаца;
  - экспорт лога как отдельную опциональную функцию.
- В `format_run_log(...)` и `export_run_log(...)` можно оставить полные данные,
  но UI больше не должен зависеть от них как от основного экрана результата.

Файлы:

- [ui/controller.py](../../ui/controller.py)

Что должно получиться:

- Controller перестает тащить result workbench как отдельную подсистему.
- State-модели становятся ближе к реальному UX: "что произошло" и "где файлы".

Как проверить:

- `build_state()` и `build_live_snapshot()` можно описать без упоминания
  `event_journal`, `candidate_assets`, `user_decision_status`.
- `build_paragraph_workbench()` для выбранного абзаца возвращает путь к файлам,
  а не список кандидатов из `provider_results`.

Практический совет:

- Сначала упростить controller, потом UI. Если UI переписать первым, controller
  продолжит собирать мертвые payload и объем кода почти не уменьшится.

### Шаг 8. Переписать Qt экран результата под summary + paths + files

Что сделать:

- В [ui/qt_app.py](../../ui/qt_app.py) удалить:
  - `selected_assets_list`
  - `candidate_assets_list`
  - обработчики `on_lock_asset(...)`
  - обработчики `on_reject_asset(...)`
  - обработчики `on_rerun_current_paragraph(...)`
  - обработчики `on_rerun_selected_paragraphs(...)`
- Заменить result-detail на компактный блок:
  - статус абзаца;
  - короткая note/message;
  - список сохраненных файлов;
  - пути к папке run/downloads.
- Если journal еще жив после P6-04, убрать его из центральной result layout и
  оставить только export path.
- Предпочтительно добавить одну простую action-кнопку:
  `Открыть папку выгрузки`.
  Если OS-open path не готов в этом пункте, показать как минимум абсолютный путь
  и не блокировать P6-07 на кнопке.

Файлы:

- [ui/qt_app.py](../../ui/qt_app.py)

Что должно получиться:

- `Qt` дает простой способ понять результат без ручного triage.
- Основной экран результата больше не похож на операторскую панель ручного
  triage.

Как проверить:

- В Qt отсутствуют виджеты candidate/selected asset chooser.
- В Qt отсутствуют кнопки/меню lock, reject, rerun current.
- Для выбранного абзаца виден список файлов с путями.

Практический совет:

- Не пытаться сохранить старую layout-структуру любой ценой. Result screen после
  P6-07 должен быть заметно короче, а не просто показывать другие данные в тех
  же больших блоках.

### Шаг 9. Обновить summary и тексты UI под файловую проверку результата

Что сделать:

- В [ui/controller.py](../../ui/controller.py) и
  [ui/presentation.py](../../ui/presentation.py) заменить тексты результата на
  продуктовые:
  - вместо "Найдено" использовать "С абзацами с файлами" или "Завершено";
  - вместо "Нужна проверка" использовать `no_match` или `failed`;
  - вместо "кандидаты" использовать "сохраненные файлы".
- Переименовать визуальные элементы вроде `run_checkpoint_label`, если они
  фактически показывают summary counters, а не checkpoint/result review state.
- Убедиться, что preview/run-progress/result detail текстом подталкивает к
  проверке папки выгрузки, а не к ручному перебору ассетов внутри UI.

Файлы:

- [ui/controller.py](../../ui/controller.py)
- [ui/presentation.py](../../ui/presentation.py)
- [ui/qt_app.py](../../ui/qt_app.py)

Что должно получиться:

- Язык интерфейса перестает отражать старую архитектуру ручного разбора.
- Пользователь понимает, что итог run нужно смотреть в папке с медиа.

Как проверить:

- В UI нет слов `candidate`, `locked`, `manual decision`, `needs review`.
- Сводка результата упоминает папку выгрузки и сохраненные файлы.

Практический совет:

- Не оставлять старые названия полей и виджетов вроде `checkpoint` или
  `selected_assets`, если смысл уже другой. Такие имена быстро возвращают старую
  архитектуру обратно.

### Шаг 10. Переписать тесты под новый result contract

Что сделать:

- В [tests/test_ui_controller.py](../../tests/test_ui_controller.py) удалить или
  переписать тесты, которые закрепляют старый workbench:
  - `test_controller_updates_workbench_runs_and_lock_reject_actions`
  - ожидания по `candidate_assets`
  - ожидания по `selected_assets`
  - ожидания по `user_decision_status == "locked"`
- В [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py) удалить
  или переписать:
  - `test_user_locked_selection_is_preserved_in_manifest`
  - проверки `locked_paragraphs` и manual-selection persistence, если они больше
    не являются продуктовым контрактом
- В [tests/test_phase9_reliability.py](../../tests/test_phase9_reliability.py)
  добавить/обновить backward-compatible сценарии загрузки legacy manifest:
  - старые поля manual workbench не ломают `from_dict()`
  - старые статусы `locked`, `partial_success`, `needs_review` нормализуются для
    нового UI

Новые тесты, которые надо добавить:

- тест на то, что `manifest.summary` содержит `downloads_root`, `videos_dir`,
  `images_dir`;
- тест на result projection, который возвращает только сохраненные файлы с
  `local_path`;
- тест на `build_paragraph_workbench(...)` без `candidate_assets`;
- тест на нормализацию legacy paragraph-status;
- тест на то, что UI controller не публикует `lock/reject/rerun current`
  actions в result flow.

Файлы:

- [tests/test_ui_controller.py](../../tests/test_ui_controller.py)
- [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py)
- [tests/test_phase9_reliability.py](../../tests/test_phase9_reliability.py)

Что должно получиться:

- Тесты описывают новый продуктовый результатный контракт, а не старый manual
  review workbench.
- Удаленные API не остаются жить только потому, что они закреплены тестами.

Как проверить:

- Поиск по `tests/` не находит новых ожиданий `candidate_assets`,
  `user_decision_status`, `lock_asset`, `reject_asset`.
- Есть отдельный тест на legacy-load старого manifest и его UI-нормализацию.

Практический совет:

- Сначала добавить тесты на новый result projection и legacy-нормализацию,
  потом удалять ручные ветки. Это защитит от случайного поломки старой истории
  run.

### Шаг 11. Финальная верификация

Что сделать:

- Прогнать статический поиск по удаляемым идентификаторам.
- Прогнать таргетные unit tests.
- Прогнать полный test suite.
- Сделать ручную smoke-проверку результата через реальную папку выгрузки.

Команды проверки:

- `ruff check .`
- `python -m unittest tests.test_ui_controller`
- `python -m unittest tests.test_media_pipeline`
- `python -m unittest tests.test_phase9_reliability`
- `python -m unittest discover -s tests`
- `Get-ChildItem app,domain,pipeline,ui,tests -Recurse -File | Select-String -Pattern 'lock_asset|reject_asset|lock_selection|candidate_assets|selected_assets|user_decision_status|fallback_options|paragraph\\.awaiting_manual_decision|rerun_current_paragraph|rerun_selected_paragraphs'`

Ручная smoke-проверка:

- Запустить `python -m app`.
- Открыть проект и выполнить run.
- После завершения убедиться, что result UI показывает:
  - итоговый статус run;
  - путь к `downloads_root`;
  - summary counters;
  - список сохраненных файлов по выбранному абзацу.
- Открыть папку выгрузки и убедиться, что путь из UI соответствует реальным
  файлам.
- Проверить, что в UI нет candidate chooser, lock/reject actions и ручного
  rerun одного абзаца.

Ожидаемый результат:

- Пользователь понимает результат по одному экрану и файловой папке.
- Результат больше не зависит от отдельной подсистемы ручного triage.
- Код result-flow заметно уменьшается по объему и числу специальных веток.

## Риски и как их не пропустить

- Риск: старые manifest-файлы со статусами `locked` и `needs_review` сломают
  UI после упрощения status vocabulary.
  Контроль: отдельный regression test на legacy manifest load и normalization.

- Риск: result UI перестанет показывать путь к реальным файлам и останется
  "только статус без результата".
  Контроль: обязательное сохранение `downloads_root` / `videos_dir` /
  `images_dir` в `manifest.summary`.

- Риск: из UI уберут кнопки, но controller/runtime API останутся живыми.
  Контроль: статический поиск по `lock_asset`, `reject_asset`,
  `lock_selection`, `rerun_current_paragraph`.

- Риск: controller сохранит сборку `candidate_assets`, хотя UI уже перестанет их
  рендерить.
  Контроль: отдельный test и grep на `candidate_assets` после cleanup.

- Риск: попытка сделать "идеальный новый storage" раздует scope и остановит
  внедрение.
  Контроль: сначала сделать flat downloaded-file projection поверх уже
  существующих `local_path`, потом удалять мертвые поля.

## Краткий итог для исполнителя

P6-07 нужно делать как cleanup по результатному контракту, а не как косметику
одного окна. Главная задача: убрать из цепочки
`pipeline -> manifest -> live snapshot -> controller -> Qt -> tests`
все, что обслуживает ручной разбор кандидатов, и оставить короткий result UX:
простой статус, понятные счетчики, путь к папке выгрузки и список реально
сохраненных файлов. Основной способ понять итог run должен быть файловая папка,
а не встроенный asset workbench.
