# Production Baseline Template

Используется как шаблон для второго baseline с реальными provider calls.

## Контекст запуска

- Дата: `YYYY-MM-DD`
- ОС: `...`
- CPU cores: `...`
- RAM: `...`
- Python: `...`
- Повторы: `...`
- paragraph_workers: `...`
- queue_size: `...`
- Providers: `...`

## Результаты (p50/p95, ms)

| Scenario | Paragraphs | paragraph_total p50 | paragraph_total p95 | provider_search p50 | provider_search p95 | download p50 | download p95 | persist p50 | persist p95 | finalize p50 | finalize p95 | intent_total p50 | intent_total p95 | intent_errors_total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| small | 3 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| medium | 15 |  |  |  |  |  |  |  |  |  |  |  |  |  |
| large | 40 |  |  |  |  |  |  |  |  |  |  |  |  |  |

## Примечания

- Указать ограничения окружения и runtime флаги.
- Указать known incidents (throttling, auth expiry, network retries).
