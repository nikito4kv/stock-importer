# P6-06. ТЗ на максимальное упрощение поиска картинок

Исходный пункт плана: [phase-6-simplification-checklist.md](./phase-6-simplification-checklist.md)

## Цель

Свести image-search к простой и предсказуемой схеме:

- у image providers используется 1-2 понятных запроса;
- нет provider-specific rewrite для разных image sources;
- нет отдельной "умной" оценки качества картинок;
- нет сложного ранжирования image candidates;
- после license filter и дедупликации берутся первые подходящие результаты.

На этом этапе важно не "улучшить качество", а убрать лишнюю архитектурную сложность, которая замедляет пайплайн, усложняет тестирование и делает поведение менее прозрачным.

Рабочее правило после P6-06:

- порядок выбора изображения определяется порядком провайдеров;
- внутри провайдера порядок определяется порядком запросов;
- внутри одного запроса порядок определяется исходным порядком результатов провайдера;
- дополнительная image-specific магия поверх этого не нужна.

Важно: в рамках P6-06 не надо переписывать video-путь. Упрощение относится именно к поиску и отбору изображений.

## Границы задачи

Входит в P6-06:

- упрощение image query generation в intent/query layer;
- удаление provider-specific rewrite для image providers;
- удаление quality-prefilter и quality-score для image candidates;
- упрощение image ranking и раннего выхода в media pipeline;
- чистка тестов и кэшей, которые существуют только ради старой image-quality логики.

Не входит в P6-06:

- изменение набора провайдеров и provider registry как продуктового решения;
- изменение video query generation и video ranking;
- удаление license-policy checks;
- удаление дедупликации кандидатов;
- крупная переделка формата `QueryBundle`, `ParagraphIntent` или UI-редактирования intent;
- переписывание legacy image clients, если цель достигается упрощением orchestration-слоя.

Практическое решение по scope:

- `QueryBundle.provider_queries` оставить, чтобы не разносить изменения в UI, payload и persistence;
- `free_image` оставить как общий bucket для бесплатных image providers;
- video relevance path не трогать, если он не мешает image simplification;
- search-result cache оставить, если он не завязан на quality scoring;
- убирать только то, что реально обслуживает "умный" image-selection.

## Где сейчас сидит лишняя логика

- Санитизация и построение image queries: [pipeline/intents.py](../../pipeline/intents.py)
- Provider-specific image query rewrite: [providers/images/querying.py](../../providers/images/querying.py)
- Quality filter, metadata cache key и scoring: [providers/images/filtering.py](../../providers/images/filtering.py)
- Search orchestration и cache wiring для image providers: [providers/images/service.py](../../providers/images/service.py)
- Free-image backend adapter и перенос `rank_hint` в `AssetCandidate`: [pipeline/media.py](../../pipeline/media.py)
- Выбор provider-specific query для backend: [pipeline/media.py](../../pipeline/media.py)
- Подготовка provider results и ranking candidates: [pipeline/media.py](../../pipeline/media.py)
- UI manual-edit path, где image queries уже синхронизируются одинаково между image providers: [ui/controller.py](../../ui/controller.py)

Основные тесты, которые обязательно затронет задача:

- [tests/test_paragraph_intents.py](../../tests/test_paragraph_intents.py)
- [tests/test_image_provider_architecture.py](../../tests/test_image_provider_architecture.py)
- [tests/test_phase3_cache_network.py](../../tests/test_phase3_cache_network.py)
- [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py)

## Definition of Done

- Для image providers используется максимум 1-2 простых запроса на абзац.
- `storyblocks_image`, `free_image` и `generic_web_image` больше не получают разные автоматически сгенерированные image-query только из-за типа провайдера.
- В `providers/images/querying.py` больше нет ветвлений по `provider_group`, которые добавляют `cinematic`, `photo`, `photograph`, `realistic` и похожие suffix/rewrite.
- В `providers/images/filtering.py` больше нет quality-prefilter и quality-score, которые ранжируют картинки по эвристикам.
- В image pipeline выбор кандидатов не зависит от отдельной relevance-очереди и quality threshold.
- После license filter и дедупликации изображения выбираются в простом детерминированном порядке.
- Основной пользовательский сценарий run execution продолжает работать: supporting/fallback images подбираются и скачиваются без regressions.

## Пошаговый план

### Шаг 1. Зафиксировать новый контракт простого image-search в тестах

Что сделать:

- Перед кодом переписать ожидания в тестах так, чтобы они описывали новую модель поведения, а не старую "умную".
- В [tests/test_image_provider_architecture.py](../../tests/test_image_provider_architecture.py) убрать ожидания, что planner добавляет provider-specific варианты вроде `photo` только для одних провайдеров и отдельные special-case варианты для других.
- В [tests/test_phase3_cache_network.py](../../tests/test_phase3_cache_network.py) убрать ожидания, связанные с `METADATA_CACHE_KEY_VERSION`, `build_metadata_cache_key(...)`, `metadata.sqlite`, `low_quality_prefilter`, если после упрощения эти механизмы становятся ненужными.
- В [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py) заменить image-specific проверки relevance degradation/timeout на проверки детерминированного порядка выбора image candidates.
- В [tests/test_paragraph_intents.py](../../tests/test_paragraph_intents.py) скорректировать ожидания так, чтобы image queries проверялись как простой и ограниченный набор запросов, а не как результат сложного ранжирования.

Файлы:

- [tests/test_paragraph_intents.py](../../tests/test_paragraph_intents.py)
- [tests/test_image_provider_architecture.py](../../tests/test_image_provider_architecture.py)
- [tests/test_phase3_cache_network.py](../../tests/test_phase3_cache_network.py)
- [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py)

Что должно получиться:

- Тесты начинают описывать простой контракт image-search.
- После этого любые остатки "умной" image-логики становятся явно видны как несоответствие новым ожиданиям.

Как проверить:

- Запустить целевые тесты и убедиться, что падения объясняют именно старую сложную логику, а не случайные побочные эффекты.
- Проверить, что в формулировках тестов больше нет зависимости от `low_quality_prefilter`, quality metadata cache и provider-specific image rewrites.

Практический совет:

- Не начинать с production-кода. Если сначала менять runtime, а потом подгонять тесты, легко сохранить старые неявные ожидания и получить ложное ощущение, что упрощение завершено.

### Шаг 2. Выделить отдельную простую ветку для image query sanitization

Что сделать:

- В [pipeline/intents.py](../../pipeline/intents.py) не переиспользовать без изменений текущую тяжёлую `_sanitize_queries(...)` для картинок.
- Ввести отдельный простой helper для image queries, который:
  - принимает уже имеющиеся `intent.image_queries`;
  - добирает fallback из `subject`, `setting`, `action`;
  - убирает пустые и слишком абстрактные варианты;
  - сохраняет текущий лимит на количество слов в query;
  - возвращает максимум 2 итоговых image-query;
  - не использует `_rank_query_candidate(...)` и `_sort_query_candidates(...)`.
- Логику для `intent.primary_video_queries` оставить как есть, чтобы не смешивать P6-06 с изменением video behavior.

Файлы:

- [pipeline/intents.py](../../pipeline/intents.py)

Что должно получиться:

- Для картинок появляется отдельный простой и читаемый путь генерации запросов.
- Сложное query ranking остается только там, где оно реально нужно, а image-path перестает зависеть от него.

Как проверить:

- В коде `intent.image_queries` больше не проходят через ту же эвристику сортировки, что и video queries.
- Для одного paragraph image queries формируются из понятного приоритета: ручные image queries, затем `subject`, затем `setting`, затем `action`.
- Итоговый список image queries всегда короткий и предсказуемый.

Практический совет:

- Не переписывать `_sanitize_queries(...)` глобально. Если упростить его для всех, можно случайно изменить video behavior и резко увеличить blast radius задачи.

### Шаг 3. Свести все image-provider query builders к одному и тому же правилу

Что сделать:

- В [pipeline/intents.py](../../pipeline/intents.py) упростить `_build_storyblocks_image_queries(...)`, `_build_free_image_queries(...)`, `_build_generic_web_image_queries(...)`.
- Сделать один общий helper, который возвращает одинаковый набор image queries для всех image providers.
- Убрать `_append_photo_hint(...)` из автоматической image-генерации.
- Оставить `provider_queries` как структуру данных, но перестать наполнять разные image providers разными версиями одного и того же смысла.
- Сохранить текущую совместимость с manual-edit path в [ui/controller.py](../../ui/controller.py), где пользовательские image queries уже копируются одинаково в `storyblocks_image`, `free_image` и `generic_web_image`.

Файлы:

- [pipeline/intents.py](../../pipeline/intents.py)
- [ui/controller.py](../../ui/controller.py)

Что должно получиться:

- Все image providers получают одинаковый короткий список query.
- В коде больше нет расхождения между "автоматически сгенерировали image queries" и "пользователь руками исправил image queries".

Как проверить:

- После `build_query_bundle(...)` проверить, что `provider_queries["storyblocks_image"]`, `provider_queries["free_image"]` и, если включён, `provider_queries["generic_web_image"]` содержат одинаковые значения.
- Проверить, что список не разрастается из-за добавления `photo`, `realistic`, `photograph` и других suffixes.

Практический совет:

- Не убирать `provider_queries` целиком. В этом пункте задача не в том, чтобы упростить модель данных, а в том, чтобы убрать разную семантику для image providers при сохранении совместимости.

### Шаг 4. Превратить image query planner в тонкую нормализацию без provider-specific rewrite

Что сделать:

- В [providers/images/querying.py](../../providers/images/querying.py) сохранить публичный интерфейс `rewrite_for_provider(...)`, чтобы не ломать все вызовы, но радикально упростить внутреннюю реализацию.
- Убрать:
  - `_suffixes_for(...)`;
  - использование `shared_build_query_variants(...)`;
  - ветвления по `descriptor.provider_group`;
  - добавление `cinematic`, `photo`, `photograph`, `realistic`, `reference`.
- Оставить только нормализацию входного query и возврат одного элемента в `ProviderQueryPlan.queries`.

Файлы:

- [providers/images/querying.py](../../providers/images/querying.py)
- [tests/test_image_provider_architecture.py](../../tests/test_image_provider_architecture.py)

Что должно получиться:

- Planner становится тонким adapter-слоем, а не местом, где сидит продуктовая логика выбора image query.
- Runtime перестает менять смысл пользовательского запроса только из-за того, какой image provider выбран.

Как проверить:

- Поиск по коду не находит ветвлений `if descriptor.provider_group == ...` в image planner.
- Один и тот же входной query для `storyblocks_image`, `pexels`, `openverse`, `bing` после planner-а остаётся одинаковым по смыслу и отличается только нормализацией пробелов.

Практический совет:

- Лучше сохранить имя метода и dataclass `ProviderQueryPlan`, чем одновременно упрощать и API. Это уменьшает риск каскадных правок в `service.py`, тестах и возможных внешних импортерах.

### Шаг 5. Убрать quality-prefilter и quality-score из image filtering

Что сделать:

- В [providers/images/filtering.py](../../providers/images/filtering.py) удалить всё, что оценивает "качество" изображения по эвристикам:
  - `LOW_QUALITY_TOKENS`;
  - `candidate_hint_score(...)`;
  - provider-specific bonuses/penalties;
  - штрафы за `unknown` license name, отсутствие referrer и attribution;
  - `assess_candidate_quality(...)` в текущем виде;
  - `cached_quality_assessment(...)`, если он больше не нужен.
- Оставить только лицензионную проверку через `is_license_allowed(...)`.
- Вернуть accepted candidates в исходном порядке, не меняя их order по score.
- Если нужен `rank_hint` для совместимости downstream-кода, оставить его как исходное значение провайдера или индекс выдачи, но не вычислять новый quality score.

Файлы:

- [providers/images/filtering.py](../../providers/images/filtering.py)
- [providers/images/service.py](../../providers/images/service.py)
- [pipeline/media.py](../../pipeline/media.py)

Что должно получиться:

- В image filtering остаётся только понятная бизнес-логика: подходит ли лицензия.
- Изображения не отбрасываются и не переставляются местами из-за эвристических штрафов и бонусов.

Как проверить:

- В accepted/rejected flow больше нет причины `low_quality_prefilter`.
- При одинаковом наборе кандидатов после filtering сохраняется исходный порядок результатов провайдера.
- Generic web results больше не penalize-ятся только потому, что это `generic_web_image`.

Практический совет:

- Не смешивать license filter с quality filter. Лицензионные ограничения - это валидное бизнес-правило, а quality scoring - цель на удаление в P6-06.

### Шаг 6. Упростить image provider search service и удалить лишний metadata cache

Что сделать:

- В [providers/images/service.py](../../providers/images/service.py) убрать wiring, которое нужно только ради quality metadata cache.
- Пересмотреть `provider_limit = max(8, min(80, max_candidates_per_keyword or 8))` и использовать фактический `max_candidates_per_keyword` без скрытого раздувания лимита для последующего quality filtering.
- Если после шага 5 `MetadataCache` больше нигде не нужен, удалить:
  - создание `metadata.sqlite`;
  - поле `_metadata_cache_instance`;
  - property `_metadata_cache`;
  - передачу `metadata_cache=...` в filtering;
  - re-export связанных сущностей.
- Оставить `SearchResultCache`, потому что кэш сырых результатов поиска не противоречит целям упрощения.

Файлы:

- [providers/images/service.py](../../providers/images/service.py)
- [providers/images/caching.py](../../providers/images/caching.py)
- [providers/images/__init__.py](../../providers/images/__init__.py)
- [tests/test_phase3_cache_network.py](../../tests/test_phase3_cache_network.py)

Что должно получиться:

- Image search orchestration становится короче и прозрачнее.
- В runtime остаётся только кэш search results, а не отдельная подсистема quality metadata.
- Количество скрытых side effects и специальных invalidation path заметно уменьшается.

Как проверить:

- В runtime больше не создаётся и не используется `metadata.sqlite`, если он не нужен для других задач.
- Поиск по репозиторию не находит активных runtime-вызовов `MetadataCache` в image search path.
- Кэширование повторных запросов через `SearchResultCache` продолжает работать.

Практический совет:

- Если есть сомнение, удалён ли `MetadataCache` полностью, сначала убрать его из runtime path и только потом чистить exports и тесты. Это безопаснее, чем начать с массового удаления.

### Шаг 7. Упростить image ranking в media pipeline до "первые подходящие"

Что сделать:

- В [pipeline/media.py](../../pipeline/media.py) отделить image path от video path там, где сейчас используется общее ranking/relevance поведение.
- Для image results убрать зависимость от:
  - `relevance_workers`;
  - `bounded_relevance_queue`;
  - `relevance_timeout_seconds`;
  - `paragraph.relevance.degraded` как части штатного image path;
  - `early_stop_quality_threshold` при наборе image slots.
- Реализовать простое правило:
  - после `_prepare_provider_result(...)` кандидаты для image сохраняют порядок;
  - при выборе supporting/fallback assets pipeline берёт первые ещё не отбракованные кандидаты;
  - ранний выход по картинкам зависит только от того, что нужное число image slots уже заполнено.
- Video path при этом оставить нетронутым, если он использует `quality_threshold` и relevance отдельным образом.

Файлы:

- [pipeline/media.py](../../pipeline/media.py)
- [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py)
- [config/settings.py](../../config/settings.py)

Что должно получиться:

- Для картинок исчезает тяжёлая инфраструктура, которая почти не даёт пользы, но добавляет таймауты, очередь и дополнительные точки отказа.
- Выбор изображений становится детерминированным и дешёвым.

Как проверить:

- В image-only сценарии порядок выбранных `fallback_assets` и `supporting_assets` соответствует порядку провайдеров и порядку найденных кандидатов.
- Image path больше не зависит от `_asset_rank(...)` как отдельной дорогой фазы.
- Конфигурационные поля relevance/quality threshold либо больше не участвуют в image path, либо явно помечены как video-only.

Практический совет:

- Не пытаться сразу удалить весь shared relevance-код. Надёжнее сначала сделать явную image-specific ветку и только потом, если код становится мёртвым, удалять общие helper-ы.

### Шаг 8. Проверить совместимость, детерминизм и отсутствие скрытой старой логики

Что сделать:

- Пройтись по коду поиском по репозиторию и убедиться, что в image path не осталось старых концепций:
  - `low_quality_prefilter`;
  - `METADATA_CACHE_KEY_VERSION`;
  - `build_metadata_cache_key`;
  - `cached_quality_assessment`;
  - provider-specific suffixes для image queries;
  - image early-stop по quality threshold.
- Прогнать таргетированные и полные тесты.
- Сделать smoke-проверку, что run execution по-прежнему проходит без GUI.

Файлы:

- [providers/images/querying.py](../../providers/images/querying.py)
- [providers/images/filtering.py](../../providers/images/filtering.py)
- [providers/images/service.py](../../providers/images/service.py)
- [pipeline/intents.py](../../pipeline/intents.py)
- [pipeline/media.py](../../pipeline/media.py)
- [tests/test_paragraph_intents.py](../../tests/test_paragraph_intents.py)
- [tests/test_image_provider_architecture.py](../../tests/test_image_provider_architecture.py)
- [tests/test_phase3_cache_network.py](../../tests/test_phase3_cache_network.py)
- [tests/test_media_pipeline.py](../../tests/test_media_pipeline.py)

Что должно получиться:

- Упрощение не осталось "наполовину".
- В кодовой базе нет скрытых хвостов старой image-quality архитектуры.

Как проверить:

- Запустить:
  - `ruff check .`
  - `python -m unittest tests.test_paragraph_intents tests.test_image_provider_architecture tests.test_phase3_cache_network tests.test_media_pipeline`
  - `python -m unittest discover -s tests`
  - `python -m app --smoke --no-gui`
- Отдельно проверить по коду, что image query generation, filtering и selection читаются как простой линейный поток без provider-specific и quality-specific эвристик.

Практический совет:

- Считать задачу незавершённой, если после всех правок в image path всё ещё есть "временный" metadata-quality код, который никто больше не использует, но который продолжает создавать ложную сложность.
