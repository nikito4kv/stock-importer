# Benchmark Runbook

Цель: воспроизводимо снять baseline по latency и сравнивать изменения по фазам.

## Что меряем

- `intent_p50_ms`, `intent_p95_ms`, `intent_errors_total`
- `provider_search_ms`, `download_ms`, `persist_ms`, `finalize_ms`
- `paragraph_total_ms`
- `candidates_found_total`, `candidates_filtered_total`, `candidates_downloaded_total`
- `no_match_total`, `early_stops_total`

## Где брать данные

- `workspace/logs/perf.jsonl` — только perf-события в JSONL
- `workspace/logs/app.log` — полный event stream
- Входные benchmark-файлы: `docs/benchmarks/fixtures/`
  (`small-3.docx`, `medium-15.docx`, `large-40.docx`)

## Как запускать benchmark

1. Подготовить окружение:
   - `python3 -m venv .venv`
   - `.venv/bin/pip install -r requirements.txt`
2. Запустить автоматизированный benchmark:
   - `.venv/bin/python -m app.benchmark_phase0 --scenarios small medium large --repeats 3 --paragraph-workers 1 --queue-size 4 --synthetic-search-delay-ms 12 --synthetic-download-delay-ms 8 --workspace-root /tmp/stock-importer-phase0-bench --write-baseline docs/benchmarks/baseline-$(date +%F).md`
3. Проверить, что отчёт обновлён и содержит все сценарии:
   - `docs/benchmarks/baseline-YYYY-MM-DD.md`
   - для production baseline использовать шаблон `docs/benchmarks/baseline-production-template.md`
4. При необходимости сохранить stdout в отдельный файл:
   - `.venv/bin/python -m app.benchmark_phase0 --scenarios medium --repeats 5 --paragraph-workers 1 --queue-size 4 > /tmp/medium-benchmark.md`

## Правила воспроизводимости

- Не менять provider flags между повторами одного сценария.
- Не менять `paragraph_workers`/`queue_size` внутри одной серии.
- Не менять synthetic delays (`--synthetic-search-delay-ms`, `--synthetic-download-delay-ms`) внутри одной серии.
- Не смешивать результаты разных ОС/CPU в один baseline.
- Использовать один и тот же `--workspace-root` в пределах одного benchmark-прогона.
