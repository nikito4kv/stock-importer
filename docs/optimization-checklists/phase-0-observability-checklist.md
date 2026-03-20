# Фаза 0: Метрики И Наблюдаемость

Цель фазы: получить честный baseline производительности до оптимизаций.

## P0-01 [x] Создать единый perf-контекст для run

Действия:
1. Добавить структуру `PerformanceContext` (run_id, project_id, timestamps, counters).
2. Добавить фабрику/хелпер инициализации контекста на старте run.
3. Прокинуть контекст в pipeline/orchestrator path без изменения бизнес-логики.

Артефакты:
- `pipeline/` (новый модуль perf context)
- `app/runtime.py` и/или `pipeline/media.py`

Проверка:
1. Для каждого запуска есть единый perf context ID.
2. Unit tests проходят без изменения функционального результата run.

## P0-02 [x] Инструментировать ingest/import тайминги

Действия:
1. Замерить длительность DOCX ingest, intent bootstrap и сохранения проекта.
2. Добавить метрики в event payload и perf-log.
3. Убедиться, что тайминги пишутся и при ошибках.

Артефакты:
- `app/runtime.py`
- `services/events.py` (если требуется расширение payload)

Проверка:
1. В логах есть `ingestion_ms`, `intent_bootstrap_ms`, `time_to_import_project_ms`.
2. Значения > 0 на реальном импорте.

## P0-03 [x] Инструментировать intent extraction этапы

Действия:
1. Добавить тайминги на paragraph-level: prompt build, model call, parse/normalize.
2. Добавить агрегаты p50/p95 по документу.
3. Зафиксировать счетчики ошибок по типам исключений.

Артефакты:
- `pipeline/intents.py`

Проверка:
1. Для каждого абзаца есть `intent_total_ms`.
2. Для документа есть агрегаты `intent_p50_ms`, `intent_p95_ms`, `intent_errors_total`.

## P0-04 [x] Инструментировать media pipeline hot-path

Действия:
1. Добавить тайминги на стадии: provider_search, download, persist, finalize.
2. Добавить счетчики кандидатов: найдено, отфильтровано, скачано, отклонено.
3. Добавить счетчик ранних остановок и причин no-match.

Артефакты:
- `pipeline/media.py`
- `pipeline/orchestrator.py`

Проверка:
1. Логи run содержат breakdown по стадиям.
2. Можно построить профиль run без ручного разбора кода.

## P0-05 [x] Добавить отдельный perf-log (jsonl)

Действия:
1. Добавить `workspace/logs/perf.jsonl`.
2. Писать туда только метрики/тайминги (без шумных UI событий).
3. Добавить ротацию по размеру или по запуску.

Артефакты:
- `services/logging.py` или новый perf logger
- `app/bootstrap.py` (подключение)

Проверка:
1. При run создается/обновляется `perf.jsonl`.
2. Каждая запись валидный JSON и содержит timestamp/run_id.

## P0-06 [x] Подготовить benchmark-сценарии

Действия:
1. Создать минимальный набор сценариев: small/medium/large.
2. Зафиксировать входные файлы, режимы провайдеров и параметры конкурентности.
3. Описать метод запуска benchmark в markdown.

Артефакты:
- `docs/benchmarks/README.md`
- `docs/benchmarks/scenarios.md`

Проверка:
1. Любой разработчик может воспроизвести benchmark по инструкции.
2. Результаты воспроизводимы в пределах допустимого разброса.

## P0-07 [x] Создать baseline-репорт

Действия:
1. Прогнать benchmark-сценарии и собрать p50/p95.
2. Сохранить baseline таблицу по этапам и размерам сценариев.
3. Зафиксировать ограничения среды (CPU, RAM, OS, Python version).
4. Зафиксировать benchmark-конфигурацию (paragraph_workers, queue_size, synthetic delays / profile).

Артефакты:
- `docs/benchmarks/baseline-2026-03-XX.md`
- `docs/benchmarks/baseline-production-template.md`

Проверка:
1. Для каждой фазы оптимизации есть с чем сравнивать.
2. В документе есть данные, дата и конфигурация окружения.
