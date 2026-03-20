# Phase 2 Remediation Plan

Цель документа: разложить найденные проблемы Phase 2 на атомарные, проверяемые шаги исправления.

Исходные проблемы:
1. Timeout-обёртка возвращает управление, но не останавливает фактическую работу.
2. `pause_after_current` и `cancel` некорректны при `paragraph_workers > 1`.
3. Storyblocks timeout/retry политика не является единой конфигурацией на уровне run.
4. Guard на Storyblocks-конкурентность в сервисе не использует уже вычисленный concurrency mode.

## Принципы исправления

- Исправлять сначала семантику остановки и cleanup, потом UI/guard-логику.
- Каждый шаг должен менять одну ответственность системы или добавлять одну проверку.
- После каждого изменения должен существовать наблюдаемый признак: тест, событие, инвариант или grep-результат.
- Нельзя вводить новый параллелизм до тех пор, пока не доказана корректность остановки старого.

## Рекомендуемый порядок выполнения

1. P2-R1: убрать ложные timeout'ы с продолжающейся фоновой работой.
2. P2-R2: выровнять Storyblocks policy и сделать её run-scoped.
3. P2-R3: переработать pause/cancel для многопараграфного исполнения.
4. P2-R4: выровнять service/UI guard по одному источнику истины.
5. P2-R5: прогнать интеграционный regression-pass по всем четырём темам.

---

## P2-R1. Timeout больше не должен оставлять живую работу в фоне

### Анализ проблемы

Сейчас `_call_with_timeout()` создаёт отдельный `daemon`-поток и ждёт `Event` до дедлайна. Если дедлайн истёк, функция возвращает timeout, но исходная операция продолжает выполняться в фоне. Для обычного CPU-кода это создаёт thread leak и поздние побочные эффекты. Для Storyblocks это опаснее: после timeout поток всё ещё может трогать browser session, страницу и файловую систему, уже вне контроля основного pipeline.

Это ломает сразу несколько инвариантов:

- timeout не равен остановке работы;
- после timeout возможны поздние записи в manifest/output;
- retry может стартовать новую попытку поверх ещё работающей старой;
- cancel/pause не могут считаться управляемыми, пока фоновые операции продолжаются.

### Целевое состояние

- По истечении timeout pipeline либо гарантированно прекращает ждать операцию и знает, что операция больше не мутирует shared state, либо явно переводит backend/session в состояние reset/recreate.
- Для каждого типа операций есть свой способ остановки: synchronous timeout на уровне клиента, cooperative cancellation, или controlled executor без detached threads.
- В кодовой базе больше нет generic helper'а, который маскирует незавершённую работу под "timeout completed".

### Чеклист

1. [ ] Зафиксировать все call sites `_call_with_timeout()` и классифицировать их по типу операции: search, download, relevance-helper.
Проверка: `Select-String -Path 'pipeline/media.py' -Pattern '_call_with_timeout\\('` показывает только ожидаемые вызовы и сам helper.

2. [ ] Зафиксировать для каждого call site требуемую семантику остановки: `hard stop`, `cooperative stop`, `backend-level timeout`.
Проверка: в плане/комментарии для каждого call site указана одна конкретная стратегия, без "решить позже".

3. [ ] Для free-image search/download убрать зависимость от generic thread-timeout и перенести timeout на уровень конкретного backend/client, где уже есть сетевой API с параметром timeout.
Проверка: поиск и download free-image backends принимают/используют stage timeout напрямую, без вызова `_call_with_timeout()`.

4. [ ] Для relevance-ranking убрать вложенный timeout-helper и оставить только bounded executor + `Future.wait(...)` с дедлайном на уже управляемых worker threads.
Проверка: relevance path больше не вызывает `_call_with_timeout()`, а timeout обрабатывается только через `wait(..., timeout=...)`.

5. [ ] Для Storyblocks search/download определить явную политику после timeout: reset page/session или recreate browser owner before next attempt.
Проверка: в коде есть один явный cleanup path для Storyblocks timeout, и он вызывается до retry/next provider.

6. [ ] Ввести отдельный helper уровня pipeline, который не запускает detached `daemon`-потоки, а только описывает timeout outcome для уже контролируемых executors или backend-level APIs.
Проверка: в `pipeline/media.py` больше нет `Thread(..., daemon=True)` для timeout-логики.

7. [ ] Удалить или полностью вывести из использования `_call_with_timeout()`.
Проверка: grep по `_call_with_timeout` находит либо 0 usages, либо только deprecated stub с `raise NotImplementedError`.

8. [ ] Добавить regression test: timed-out image download не должен создавать файл позже дедлайна.
Проверка: тест сначала фиксирует timeout, затем после дополнительной паузы убеждается, что ожидаемый файл так и не появился.

9. [ ] Добавить regression test: timed-out Storyblocks/mock backend не должен выполнять повторную попытку поверх ещё живой первой попытки.
Проверка: тест считает конкурентные вызовы backend и подтверждает, что после timeout предыдущая попытка cleanup'нута до retry.

10. [ ] Добавить regression test на отсутствие runaway worker threads после серии timeout'ов.
Проверка: тест сравнивает число активных рабочих потоков до и после пачки timeout-сценариев с допустимым малым дельта-порогом.

11. [ ] Добавить observability-событие для timeout cleanup path.
Проверка: event journal содержит отдельное событие вида `provider.timeout.cleaned_up` или эквивалентное, а payload указывает stage и backend.

12. [ ] Обновить документацию Phase 2/timeout policy так, чтобы было явно записано: timeout = caller stop + cleanup, а не только fast return.
Проверка: в docs есть одно место с новой формулировкой, и оно не противоречит существующим тестам.

---

## P2-R2. Storyblocks timeout/retry policy должна стать run-scoped и единой

### Анализ проблемы

Сейчас `StoryblocksCandidateSearchBackend` получает `download_retries` и `download_timeout_seconds` в момент bootstrap. UI и runtime могут менять concurrency settings на уровне run, но уже созданный backend продолжает жить со старыми значениями. Одновременно pipeline поверх этого backend использует свои run-level timeout/retry. В итоге фактическая policy раздваивается:

- внешний pipeline budget;
- внутренний Storyblocks download manager budget.

Это приводит к непредсказуемому количеству попыток и к расхождению между тем, что пользователь выставил в advanced settings, и тем, как реально работает Storyblocks download path.

### Целевое состояние

- Для run существует один источник истины по timeout/retry policy.
- Storyblocks backend получает policy на вызов или на run context, а не только на bootstrap.
- Изменение advanced settings влияет на следующий run без перезапуска приложения и без пересоздания контейнера вручную.

### Чеклист

1. [ ] Зафиксировать все места, где timeout/retry читаются из `settings.concurrency` и `settings.browser` для Storyblocks path.
Проверка: grep по `download_retries`, `download_timeout_seconds`, `retry_budget`, `downloads_timeout_seconds` показывает полный список мест.

2. [ ] Выбрать единый owner для Storyblocks operation policy: либо `MediaSelectionConfig`, либо отдельный `ProviderOperationPolicy`, но не оба одновременно.
Проверка: в design note выбран один canonical source of truth.

3. [ ] Вынести Storyblocks search/download policy в отдельную dataclass/DTO, пригодную для передачи на каждый вызов.
Проверка: новый тип используется и в pipeline, и в Storyblocks backend, без прямого чтения global settings внутри runtime path.

4. [ ] Изменить интерфейс Storyblocks backend так, чтобы timeout/retry policy передавалась per-call или per-run, а bootstrap-значения стали только дефолтами.
Проверка: bootstrap больше не является единственным местом, где задаётся актуальная Storyblocks policy.

5. [ ] Убрать дублирование budget'ов между pipeline retry loop и внутренним `StoryblocksDownloadManager`, либо формально развести их роли.
Проверка: код явно описывает один общий retry budget или два разных budget'а с разными именами и комментариями.

6. [ ] Обновить вызов Storyblocks download/search так, чтобы runtime использовал policy текущего run, а не cached значения backend instance.
Проверка: тест может изменить advanced settings, создать новый run и увидеть новые timeout/retry в реальном Storyblocks/mock path.

7. [ ] Добавить unit test: изменение advanced settings между двумя run'ами меняет effective Storyblocks timeout без перезапуска приложения.
Проверка: первый run использует старое значение, второй run использует новое значение, это видно по mock backend/spy.

8. [ ] Добавить unit test: retry budget в Storyblocks download не превышает ожидаемое число суммарных попыток.
Проверка: счётчик вызовов backend/download manager совпадает с задокументированным budget.

9. [ ] Добавить payload/event с effective Storyblocks policy на run start.
Проверка: в run metadata или event payload есть зафиксированные `search_timeout_seconds`, `download_timeout_seconds`, `retry_budget`.

10. [ ] Удалить или явно пометить как legacy bootstrap-only параметры Storyblocks backend constructor.
Проверка: signature backend constructor больше не создаёт ложное впечатление, что именно там живёт актуальная runtime policy.

---

## P2-R3. Pause/cancel при `paragraph_workers > 1` должны быть корректными и детерминированными

### Анализ проблемы

Параллельная ветка orchestrator сейчас делает ранний `return` прямо из цикла обработки `done`. Из-за этого:

- может быть записан только один завершившийся future из пачки `done`;
- другие уже завершившиеся paragraph tasks остаются отражёнными в manifest, но не в checkpoint/run state;
- выход из `with BoundedExecutor(...)` вызывает blocking shutdown, поэтому pause/cancel ждут весь пул, хотя логика уже решила завершиться.

Проблема усугубляется тем, что free-image mode официально допускает `paragraph_workers > 1`, а существующие тесты pause/cancel under load используют только `max_workers = 1`.

### Целевое состояние

- После pause/cancel run state, checkpoint и manifest согласованы.
- Orchestrator сначала полностью обрабатывает уже готовые `done` futures текущей итерации, потом принимает решение о transition.
- Новые paragraph tasks после pause/cancel не submit'ятся.
- Завершение parallel path не полагается на неявный `with`-shutdown там, где нужен контролируемый stop/drain policy.

### Чеклист

1. [ ] Зафиксировать желаемую семантику `pause_after_current` при `paragraph_workers > 1`: "после первого завершившегося", "после текущей волны done", или "после всех in-flight".
Проверка: выбран ровно один вариант и он описан в кодовом комментарии/документации.

2. [ ] Зафиксировать отдельную семантику для `cancel`: что делать с уже завершившимися futures, что делать с in-flight futures, что писать в checkpoint.
Проверка: есть одна таблица/комментарий с expected outcome для `done`, `pending`, `not yet submitted`.

3. [ ] Убрать ранний `return` из цикла `for future in done` и сначала полностью обработать весь набор `done`.
Проверка: в orchestrator больше нет `return` внутри цикла обработки `done`.

4. [ ] После обработки полного набора `done` ввести отдельный decision point, который выбирает `pause`, `cancel`, `continue`.
Проверка: decision point существует в одном месте, а не размазан по нескольким веткам.

5. [ ] Перестать использовать `with BoundedExecutor(...)` в том месте, где нужен неявный `shutdown(wait=True)`.
Проверка: orchestrator управляет `shutdown(wait=..., cancel_futures=...)` явно и осознанно.

6. [ ] Ввести явную stop-policy для executor на pause/cancel: stop new submissions immediately, затем drain или cancel в соответствии с выбранной семантикой.
Проверка: код отдельно делает "stop submit", отдельно делает "drain/cancel".

7. [ ] Обеспечить инвариант: если paragraph успел попасть в manifest как processed, он обязательно отражён и в `run.completed_paragraphs` или `run.failed_paragraphs`.
Проверка: после каждой итерации есть assertion/helper-тест на согласованность run state и manifest summary.

8. [ ] Добавить helper проверки консистентности run/manfiest/checkpoint для тестов reliability.
Проверка: тесты вызывают один reusable assert-helper, а не дублируют сравнение вручную.

9. [ ] Добавить regression test: `pause_after_current` при `paragraph_workers = 2+` и двух одновременно завершившихся futures не теряет результаты второго future.
Проверка: после pause manifest и checkpoint содержат одинаковое число завершённых paragraph entries.

10. [ ] Добавить regression test: `cancel` при `paragraph_workers = 2+` не ждёт бесконечно весь пул и не оставляет "невидимые" completed paragraphs.
Проверка: тест ограничивает wall-clock и сверяет run state с manifest.

11. [ ] Добавить regression test: `resume` после parallel pause продолжает только действительно непроцессенные paragraphs.
Проверка: resumed run не переобрабатывает paragraph, уже отражённый и в manifest, и в checkpoint.

12. [ ] Добавить event payload для pause/cancel решения: сколько futures было `done`, `pending`, `cancelled`.
Проверка: event journal содержит диагностическую запись с этими числами.

13. [ ] После исправления включить отдельный stress-test для free-image mode с `paragraph_workers > 1`, pause, cancel и resume в одном сценарии.
Проверка: тест стабильно проходит многократно и не зависит от lucky timing.

---

## P2-R4. Storyblocks parallelism guard должен использовать resolved concurrency mode

### Анализ проблемы

Сервисный guard сейчас смотрит только на `video_enabled` и `storyblocks_images_enabled`. Но это не то же самое, что фактическое использование Storyblocks provider'ов. Реальный источник истины уже существует: `ProviderRegistry.resolve_concurrency_mode(...)`. Из-за расхождения service и UI могут принимать разные решения для одного и того же набора провайдеров.

### Целевое состояние

- Guard в service и UI опираются на одну и ту же функцию/правило.
- Если resolved mode = `free_images_parallel`, paragraph parallelism разрешён без ручного отключения Storyblocks флагов.
- Если resolved mode использует Storyblocks, guard блокирует небезопасный режим независимо от того, из какого entrypoint пришёл вызов.

### Чеклист

1. [ ] Вынести логику "требует ли режим paragraph_workers=1" в один helper, основанный на `resolve_concurrency_mode(...)`.
Проверка: service и UI больше не имеют двух разных условий на Storyblocks parallelism.

2. [ ] Переписать `ParagraphMediaRunService._validate_storyblocks_concurrency()` так, чтобы он использовал resolved mode, а не raw flags.
Проверка: код guard читает `mode_resolution.mode` или `mode_resolution.uses_storyblocks`.

3. [ ] Переписать UI validation на тот же helper или на тот же lower-level API.
Проверка: в UI нет самостоятельной логики определения Storyblocks presence по provider ids.

4. [ ] Добавить unit test: only-free-image providers + `paragraph_workers > 1` + default flags не должны падать в service path.
Проверка: тест проходит без необходимости вручную ставить `video_enabled=False` и `storyblocks_images_enabled=False`.

5. [ ] Добавить unit test: mixed mode и pure Storyblocks mode по-прежнему блокируются.
Проверка: guard возвращает тот же error code, что и раньше, для небезопасных режимов.

6. [ ] Добавить unit test на согласованность UI и service: оба entrypoint для одинакового provider set принимают одинаковое решение.
Проверка: один общий тестовый сценарий покрывает оба слоя и сравнивает outcome.

7. [ ] Записать effective concurrency mode в error details guard'а.
Проверка: payload ошибки содержит `concurrency_mode`, что упрощает диагностику.

---

## P2-R5. Финальный regression-pass и quality gate

### Анализ задачи

Даже если каждая проблема исправлена локально, Phase 2 останется хрупкой без общего набора тестов и критериев приёмки. Здесь нужен короткий, но жёсткий gate, который закрывает именно найденные классы дефектов.

### Целевое состояние

- Есть явный набор regression-тестов для timeout cleanup, Storyblocks policy, parallel pause/cancel и resolved guard.
- Проверки запускаются до отметки phase completion.
- Документация и чеклист обновлены по фактическому поведению.

### Чеклист

1. [ ] Собрать новый список тестов, покрывающих P2-R1..P2-R4, и пометить их как обязательные для Phase 2 remediation.
Проверка: список тестовых имён/классов существует в одном месте.

2. [ ] Прогнать `ruff check .`.
Проверка: команда завершается без ошибок.

3. [ ] Прогнать целевые `unittest` suites для `test_media_pipeline`, `test_phase9_reliability`, `test_ui_controller`.
Проверка: все связанные suites проходят.

4. [ ] Добавить при необходимости новый `tests/test_phase2_remediation.py`, если сценарии слишком разнородны для существующих файлов.
Проверка: новые regression tests лежат в одном очевидном месте и не дублируют старые почти дословно.

5. [ ] Сверить event payloads и run metadata с ожидаемыми полями после исправлений.
Проверка: smoke-test или unit test читает payload и подтверждает наличие новых диагностических полей.

6. [ ] Обновить `docs/optimization-checklists/phase-2-concurrency-checklist.md` или соседний remediation note статусом фактического закрытия.
Проверка: рядом с каждым исправленным дефектом есть ссылка на тест или commit/PR note.

7. [ ] Зафиксировать остаточные риски, если какие-то stop-semantics для внешних библиотек остаются кооперативными, а не hard-cancel.
Проверка: есть короткий residual-risk раздел, а не молчаливое допущение.

---

## Definition of Done

Phase 2 remediation можно считать завершённым только если одновременно выполнены все условия ниже:

1. timeout path больше не создаёт detached background work без cleanup strategy;
2. Storyblocks timeout/retry policy считывается из актуального run-level source of truth;
3. `pause_after_current` и `cancel` корректны и консистентны при `paragraph_workers > 1`;
4. service и UI используют один и тот же resolved concurrency rule;
5. regression tests на все четыре дефекта зелёные;
6. документация обновлена под фактическое поведение системы.
