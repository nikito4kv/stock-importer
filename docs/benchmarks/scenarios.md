# Benchmark Scenarios

Все сценарии запускаются на чистом workspace. Конфигурация фиксируется и не меняется внутри серии.

## Общие параметры

- `strictness`: `balanced`
- `paragraph_workers`: `1` (для baseline фазы 0)
- `queue_size`: `4`
- `synthetic_search_delay_ms`: `12`
- `synthetic_download_delay_ms`: `8`
- `max_candidates_per_provider`: `8`
- `top_k_to_relevance`: `24`
- `storyblocks_images_enabled`: `true`
- `free_images_enabled`: `false`
- `video_enabled`: `true`
- повторов на сценарий: `3`
- рекомендованный запуск: `.venv/bin/python -m app.benchmark_phase0 --scenarios small medium large --repeats 3 --paragraph-workers 1 --queue-size 4 --synthetic-search-delay-ms 12 --synthetic-download-delay-ms 8`

## Small

- Входной файл: `docs/benchmarks/fixtures/small-3.docx`
- Размер документа: `3` paragraph entries
- Цель: быстрый smoke baseline, проверка корректности метрик

## Medium

- Входной файл: `docs/benchmarks/fixtures/medium-15.docx`
- Размер документа: `15` paragraph entries
- Цель: стабильные p50/p95 на типичном сценарии

## Large

- Входной файл: `docs/benchmarks/fixtures/large-40.docx`
- Размер документа: `40` paragraph entries
- Цель: проверка хвоста распределения и влияния persist/finalize стадий

## Формат фиксации результатов

Для каждого сценария фиксировать:

- `paragraph_total_ms`: p50 / p95
- `provider_search_ms`: p50 / p95
- `download_ms`: p50 / p95
- `persist_ms`: p50 / p95
- `finalize_ms`: p50 / p95
- `intent_p50_ms` / `intent_p95_ms` / `intent_errors_total`
- ограничения среды: CPU / RAM / OS / Python
