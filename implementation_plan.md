# План развития проекта

Этот документ фиксирует обновленный фазовый план развития проекта с учетом уточненной продуктовой идеи:

- проект - это desktop-first приложение для автоматизированного подбора медиа под сценарий;
- основной смысловой объект системы - нумерованный абзац сценария, а не абстрактный тайм-сегмент;
- текущая логика выделения нумерованных абзацев из `keyword_extractor.py` сохраняется и переносится в общую подсистему ingestion;
- Storyblocks выступает основным источником видео и опционально изображений через сохраненную авторизованную браузерную сессию;
- бесплатные внешние источники и web-image поиск используются прежде всего как слой для изображений, fallback или отдельный режим проекта;
- текущая legacy free-video ветка перестает быть целевым product-path, но полезные технические куски из нее должны быть переиспользованы;
- автоматическая логика таймингов `1 видео на 15-20 секунд` не заменяет paragraph-first модель, а рассматривается как дополнительный слой slot planning для длинных абзацев или post-MVP режима;
- план рассчитан на допустимую автоматизацию пользовательской авторизованной сессии без задач по обходу защит: только ручной логин, dedicated persistent browser profile, реальный установленный browser channel, щадящие задержки, пауза и возобновление, прозрачная обработка challenge и blocked state; временные Playwright-контексты, `codegen`, `--save-storage` и `browser.new_context()` не считаются целевым auth-path для Storyblocks.

## Общие критерии готовности для каждой фазы

- [ ] Все декомпозированные задачи фазы реализованы.
- [ ] Для нового и измененного кода есть автоматические тесты.
- [ ] Пройден ручной smoke или e2e-сценарий по пользовательскому пути фазы.
- [ ] Обновлены конфигурация, документация, миграции данных и UX-тексты.
- [ ] Не осталось legacy-зависимостей, мешающих следующей фазе.

## Фаза 0. Фиксация product scope и целевой модели

- [x] Зафиксировать MVP как paragraph-first продукт, где каждый нумерованный абзац является отдельной смысловой единицей подбора.
- [x] Зафиксировать базовые режимы работы MVP:
  - [x] только Storyblocks video;
  - [x] Storyblocks video + Storyblocks images;
  - [x] Storyblocks video + free images;
  - [x] Storyblocks images + free images;
  - [x] только free images.
- [x] Явно зафиксировать, что free-video из текущего `video_fetcher.py` не является целевым пользовательским режимом.
- [x] Зафиксировать, что для MVP главным источником видео является Storyblocks через сохраненную браузерную сессию.
- [x] Зафиксировать входные форматы сценария:
  - [x] MVP: нумерованный `.docx`;
  - [x] next step: нумерованный `.txt` и `.md`;
  - [x] правила валидации numbering и сообщения об ошибках.
- [x] Зафиксировать модель результата по абзацу:
  - [x] primary video;
  - [x] optional Storyblocks image;
  - [x] optional free-image fallback;
  - [x] сохраненные поисковые запросы и причины выбора.
- [x] Зафиксировать решение по Storyblocks images в MVP.
- [x] Зафиксировать решение по Storyblocks audio:
  - [x] вынести Storyblocks audio в post-MVP;
  - [x] отдельный MVP-трек задач по audio не нужен.
- [x] Зафиксировать решение по авто-слотингу `1 видео на 15-20 секунд`:
  - [x] считать это дополнительным режимом поверх paragraph-first модели;
  - [x] не заменять абзац как базовую сущность.
- [x] Описать пользовательские сценарии:
  - [x] первый запуск;
  - [x] выбор сценария;
  - [x] валидация нумерации абзацев;
  - [x] просмотр и корректировка абзацев;
  - [x] вход в Storyblocks;
  - [x] выбор режима работы;
  - [x] выбор провайдеров изображений;
  - [x] запуск run;
  - [x] пауза и возобновление;
  - [x] повторный запуск только по выбранным абзацам;
  - [x] смена аккаунта;
  - [x] работа с пресетами.
- [x] Описать продуктовые метрики успеха:
  - [x] percent абзацев с найденным primary video;
  - [x] fill-rate по изображениям на абзац;
  - [x] среднее время обработки сценария;
  - [x] частота ручного вмешательства;
  - [x] процент успешного reuse сохраненной Storyblocks-сессии;
  - [x] процент успешного rerun only failed paragraphs;
  - [x] качество выбранных запросов по отношению к текущей keyword-модели.
- [x] Зафиксировать правовые и эксплуатационные ограничения:
  - [x] только пользовательская авторизованная сессия;
  - [x] только разрешенные загрузки;
  - [x] без реализации обхода защит;
  - [x] прозрачное информирование пользователя о проблемах с сессией.
- [x] Выбрать desktop stack:
  - [x] GUI framework: PySide6;
  - [x] browser automation stack: Playwright for Python с persistent Chromium context;
  - [x] сохранить Python-core и выбрать desktop GUI, совместимый с локальным Python pipeline.
- [x] Подготовить короткий PRD/spec, продуктовую терминологию и матрицу режимов.
- [x] Артефакты фазы 0 подготовлены:
  - [x] `docs/phase-0/prd.md`;
  - [x] `docs/phase-0/terminology.md`;
  - [x] `docs/phase-0/mode-matrix.md`.

### Проверка фазы 0

- [x] Утвержден короткий PRD/spec (`docs/phase-0/prd.md`).
- [x] Утверждена матрица режимов (`docs/phase-0/mode-matrix.md`).
- [x] Приняты решения по Storyblocks images, Storyblocks audio и авто-слотингу 15-20 секунд.
- [x] Зафиксировано, что абзац - главная доменная сущность MVP.

## Фаза 1. Удаление legacy-path и извлечение reusable core

- [x] Вывести из целевой архитектуры текущий `video_fetcher.py` как legacy free-video product-path.
- [x] До удаления извлечь из `video_fetcher.py` reusable-технические части:
  - [x] скачивание с безопасной обработкой redirects;
  - [x] file hashing и dedupe;
  - [x] ffmpeg/ffprobe validation;
  - [x] temp-file handling;
  - [x] manifest-поля и диагностическую телеметрию;
  - [x] общие retry/backoff паттерны.
- [x] Извлечь из `image_fetcher.py` reusable-части:
  - [x] provider adapters;
  - [x] network safety;
  - [x] license policy;
  - [x] caching;
  - [x] normalization;
  - [x] multi-stage filtering.
- [x] Извлечь из `keyword_extractor.py` reusable-логику ingestion:
  - [x] `read_script_paragraphs`;
  - [x] валидацию numbering;
  - [x] сохранение `header_text` и `original_index`;
  - [x] нормализацию paragraph payload.
- [x] Подготовить план удаления и миграции legacy-артефактов:
  - [x] `output/cache/video_relevance_cache.sqlite`;
  - [x] `output/tmp/videos/`;
  - [x] `output/videos/`;
  - [x] старые video-manifest артефакты;
  - [x] video CLI-параметры;
  - [x] зависимости, нужные только legacy free-video ветке.
- [x] Убрать из терминологии проекта модель отдельных CLI-скриптов как целевого продукта.
- [x] Подготовить стратегию миграции старых артефактов:
  - [x] что архивируется;
  - [x] что удаляется;
  - [x] что больше не поддерживается.
- [x] Убрать неожиданные runtime side effects вроде автосоздания `.env` как части будущего product-path.
- [x] Артефакты фазы 1 подготовлены:
  - [x] `legacy_core/` reusable modules;
  - [x] `docs/phase-1/legacy-migration.md`;
  - [x] `tests/` на extracted core.

### Проверка фазы 1

- [x] В целевой схеме не осталось зависимости от legacy free-video пути.
- [x] Все reusable части из старых скриптов выделены до удаления старого product-path.
- [x] Логика разбора нумерованных абзацев сохранена и оформлена как отдельный reusable module.
- [x] Все legacy-артефакты перечислены и помечены к удалению или архивированию.

## Фаза 2. Рефакторинг основы проекта в desktop-приложение

- [x] Превратить проект из набора монолитных скриптов в пакетное приложение.
- [x] Ввести модули:
  - [x] `app`;
  - [x] `domain`;
  - [x] `pipeline`;
  - [x] `providers`;
  - [x] `browser`;
  - [x] `storage`;
  - [x] `ui`;
  - [x] `config`;
  - [x] `services`.
- [x] Разделить бизнес-логику, браузерную автоматизацию, UI, persistence и provider-инфраструктуру.
- [x] Ввести типизированные модели:
  - [x] `Project`;
  - [x] `Run`;
  - [x] `ScriptDocument`;
  - [x] `ParagraphUnit`;
  - [x] `ParagraphIntent`;
  - [x] `QueryBundle`;
  - [x] `ProviderResult`;
  - [x] `AssetCandidate`;
  - [x] `AssetSelection`;
  - [x] `BrowserProfile`;
  - [x] `RunCheckpoint`.
- [x] Ввести единый orchestrator, управляющий стадиями выполнения, прогрессом, отменой, pause/resume и rerun выбранных абзацев.
- [x] Ввести единый storage-слой для:
  - [x] состояния run;
  - [x] настроек;
  - [x] пресетов;
  - [x] кэшей;
  - [x] workspace проекта;
  - [x] browser-profile registry.
- [x] Заменить ad-hoc `.env` и CLI-модель на централизованный settings manager.
- [x] Ввести безопасное хранение секретов и пользовательских ключей.
- [x] Ввести нормальную taxonomy ошибок для UI и логов:
  - [x] config errors;
  - [x] session errors;
  - [x] provider errors;
  - [x] download errors;
  - [x] relevance errors;
  - [x] persistence errors.
- [x] Ввести структуру логов и событий, пригодную для GUI.
- [x] Заложить bounded concurrency, очереди и backpressure как базовую архитектурную норму.

### Проверка фазы 2

- [x] Приложение стартует как единая система.
- [x] Состояние run и настройки сохраняются и восстанавливаются.
- [x] Основные модули тестируются независимо.
- [x] Нет циклических зависимостей между UI, orchestration и provider-слоями.
- [x] Ошибки разделены по категориям и пригодны для отображения пользователю.

## Фаза 3. Переработка анализа сценария: от `3 keywords` к `paragraph intent`

- [x] Сохранить paragraph-first модель как ядро системы.
- [x] Перенести текущую логику чтения нумерованных абзацев из `keyword_extractor.py` в shared ingestion service.
- [x] Поддержать сохранение и отображение:
  - [x] `header_text`;
  - [x] `paragraph_no`;
  - [x] `original_index`;
  - [x] исходного текста абзаца;
  - [x] статуса валидности numbering.
- [x] Ввести валидацию структуры сценария:
  - [x] пропущенные номера;
  - [x] ненумерованные блоки;
  - [x] пустые абзацы;
  - [x] предупреждения до запуска run.
- [x] Уйти от схемы `ровно 3 keywords на абзац` к richer paragraph intent model.
- [x] Ввести structured paragraph intent:
  - [x] `primary_video_queries`;
  - [x] `image_queries`;
  - [x] `subject`;
  - [x] `action`;
  - [x] `setting`;
  - [x] `mood`;
  - [x] `style`;
  - [x] `negative_terms`;
  - [x] `source_language`;
  - [x] `translated_queries`;
  - [x] `estimated_duration_seconds` как будущий timing hint, не заменяющий абзац.
- [x] Сделать отдельные стратегии query generation для:
  - [x] Storyblocks video;
  - [x] Storyblocks images;
  - [x] free images;
  - [x] generic web images при явном включении.
- [x] Снизить долю абстрактных и плохо визуализируемых запросов вроде `fear of the unknown` как primary search key.
- [x] Добавить режимы extraction strictness:
  - [x] простой;
  - [x] сбалансированный;
  - [x] строгий.
- [x] Добавить возможность ручной правки paragraph intent до запуска поиска.
- [x] Сделать новый базовый контракт данных вместо старого `paragraph_keywords.json`, например `paragraph_intents.json`.

### Проверка фазы 3

- [x] На тестовом сценарии нумерованные абзацы формируются стабильно.
- [x] Intent-запросы визуально лучше старых keywords.
- [x] Процент явно абстрактных поисковых фраз снижен.
- [x] Есть golden tests минимум на нескольких сценариях.
- [x] `estimated_duration_seconds` сохраняется как вспомогательное поле, не ломая paragraph-first модель.

## Фаза 4. Storyblocks session automation core

- [x] Выбрать и зафиксировать browser automation stack:
  - [x] Playwright for Python через `launch_persistent_context(...)`;
  - [x] dedicated `user_data_dir` на профиль;
  - [x] реальный установленный browser channel (`chrome` как default, `msedge` как fallback);
  - [x] явно зафиксировать, что временный Chromium/guest context, `codegen` и `storage_state` не являются поддерживаемым login baseline для Storyblocks.
- [x] Реализовать manager браузерных профилей:
  - [x] создать профиль;
  - [x] создать предсказуемую структуру каталогов профиля (`user_data`, `downloads`, `diagnostics`);
  - [x] выбрать профиль;
  - [x] переименовать профиль;
  - [x] удалить профиль;
  - [x] переключить активный профиль.
- [x] Реализовать session manager:
  - [x] открыть браузер через persistent context;
  - [x] проверить доступность выбранного browser channel;
  - [x] определить, что профиль занят другим процессом браузера;
  - [x] проверить авторизацию;
  - [x] определить невалидную сессию;
  - [x] предложить ручной логин;
  - [x] сохранить сессию между перезапусками.
- [x] Реализовать flow ручного вмешательства:
  - [x] login required;
  - [x] challenge detected;
  - [x] ручное подтверждение пользователем;
  - [x] ожидание пользователя в уже открытом persistent browser profile;
  - [x] продолжение run после вмешательства.
- [x] Реализовать Storyblocks video search adapter:
  - [x] открытие поиска через прямой URL `/all-video/search/{query-slug}` как базовый путь;
  - [x] homepage search использовать только как secondary/rescue path;
  - [x] ввод запроса;
  - [x] применение фильтров;
  - [x] навигация по результатам;
  - [x] чтение карточек;
  - [x] нормализация метаданных и извлечение `asset_id` из `data-testid` или detail URL.
- [x] Реализовать Storyblocks image search adapter по той же схеме:
  - [x] открытие поиска через прямой URL `/images/search/{query-slug}` как базовый путь;
  - [x] не опираться на homepage media tabs как на основной automation path.
- [x] Реализовать selector layer и DOM-contract regression checks для browser automation:
  - [x] `Input Search:`;
  - [x] `Submit Search`;
  - [x] `justified-gallery-item-*`;
  - [x] detail-page `Download` button;
  - [x] fallback selectors на случай DOM drift.
- [x] Реализовать Storyblocks download manager:
  - [x] очередь загрузок;
  - [x] статусы;
  - [x] retry policy;
  - [x] корректная запись файлов;
  - [x] дедупликация;
  - [x] определение завершения загрузки;
  - [x] базовый happy-path через `page.expect_download()` с detail page.
- [x] Реализовать щадящий `slow mode`:
  - [x] увеличенные задержки;
  - [x] ограничения на частоту действий;
  - [x] backoff при нестабильности.
- [x] Реализовать обнаружение blocked или expired state и понятное сообщение в UI.
- [x] Реализовать восстановление после перезапуска приложения.
- [x] Реализовать ручной rescue-flow: открыть текущий абзац и запрос в браузере для ручной помощи без потери run state.
- [x] Зафиксировать baseline наблюдения и ограничения по Storyblocks automation в `docs/phase-4/storyblocks-automation-baseline.md`.

### Проверка фазы 4

- [ ] Реальный ручной e2e сценарий с аккаунтом проходит успешно.
- [ ] Логин сохраняется между перезапусками.
- [ ] Смена аккаунта работает.
- [x] Логин в временном Chromium/guest context явно не считается поддерживаемым сценарием и не трактуется как продуктовый regression.
- [ ] Run можно поставить на паузу при ручном логине и продолжить без потери состояния.
- [x] Browser automation не разваливается на базовом наборе regression fixtures.
- [ ] Happy-path поиска и скачивания подтвержден отдельно для Storyblocks video и Storyblocks images.

## Фаза 5. Подсистема Storyblocks и бесплатных изображений

- [x] Вытащить reusable-логику из `image_fetcher.py` в независимые provider modules.
- [x] Ввести provider registry с опциональным включением и отключением провайдеров.
- [x] Разделить image providers по группам:
  - [x] Storyblocks images;
  - [x] free stock APIs;
  - [x] open-license repositories;
  - [x] generic web image search.
- [x] Пересмотреть дефолтный набор провайдеров, их приоритеты и лицензионную политику.
- [x] Отдельно решить, нужен ли Bing в default-path или только как opt-in режим.
- [x] Добавить search-result cache и metadata cache.
- [x] Добавить query normalization и query rewrite под разные типы image providers.
- [x] Добавить многоступенчатую фильтрацию:
  - [x] metadata prefilter;
  - [x] source-quality prefilter;
  - [x] cheap ranking;
  - [x] отбрасывание screenshot, illustration, logo, meme, UI-like результатов там, где это не запрошено;
  - [x] Gemini relevance только для top-K.
- [x] Реализовать понятную политику лицензий и отображение ограничений в UI и manifest.
- [x] Поддержать режим `только бесплатные изображения` без Storyblocks.
- [x] Поддержать mixed-mode, где бесплатные изображения выступают fallback или отдельным типом результата.

### Проверка фазы 5

- [ ] Каждый провайдер проходит отдельные integration tests.
- [x] Выбор провайдеров пользователем работает.
- [x] Качество image results выше текущего noisy literal поведения.
- [x] Лицензии и источники корректно отражаются в manifest.
- [x] Generic web image search включается осознанно и прозрачно по рискам качества и лицензий.

## Фаза 6. Единый paragraph-based движок подбора медиа

- [x] Ввести единый candidate pipeline для:
  - [x] Storyblocks video;
  - [x] Storyblocks images;
  - [x] free images.
- [x] Реализовать selection policy по абзацу:
  - [x] 1 primary video на абзац, если video mode включен;
  - [x] optional supporting images;
  - [x] fallback policy по режиму проекта;
  - [x] user-lock для вручную подтвержденных ассетов.
- [x] Не заменяя paragraph-first модель, заложить в доменную схему возможность нескольких media slots внутри одного абзаца для будущего режима авто-слотинга.
- [x] Реализовать dedupe на нескольких уровнях:
  - [x] raw file hash;
  - [x] source id;
  - [x] perceptual hash там, где это уместно;
  - [x] semantic similarity там, где это оправдано.
- [x] Реализовать unified manifest по абзацам:
  - [x] проект;
  - [x] абзацы;
  - [x] paragraph intent;
  - [x] provider queries;
  - [x] выбранные ассеты;
  - [x] fallback-варианты;
  - [x] статусы;
  - [x] причины отклонений;
  - [x] источник;
  - [x] лицензия;
  - [x] путь к файлу;
  - [x] user decision status.
- [x] Реализовать checkpointing:
  - [x] остановка после текущего абзаца;
  - [x] безопасная пауза;
  - [x] resume с места остановки;
  - [x] retry failed only;
  - [x] rerun selected paragraphs only.
- [x] Реализовать per-paragraph diagnostics.
- [x] Реализовать configurable provider priority и sourcing strategy.
- [x] Ограничить fan-out и стоимость pipeline:
  - [x] top-K до Gemini;
  - [x] bounded downloads;
  - [x] bounded relevance queue;
  - [x] ранняя остановка, когда для абзаца уже найден приемлемый набор.

### Проверка фазы 6

- [x] Один run можно остановить и продолжить без повреждения состояния.
- [x] Итоговый manifest детерминирован и пригоден для следующего этапа использования.
- [x] Mixed-mode дает предсказуемый результат.
- [x] Поведение системы описывается на уровне абзаца, а не только на уровне сырых запросов.

## Фаза 7. UI для обычного и продвинутого пользователя

- [x] Сделать desktop-first GUI с двумя уровнями:
  - [x] быстрый запуск;
  - [x] расширенные настройки.
- [x] Реализовать основной пользовательский flow:
  - [x] открыть сценарий;
  - [x] провалидировать нумерованные абзацы;
  - [x] просмотреть список абзацев;
  - [x] выбрать режим работы;
  - [x] выбрать провайдеры;
  - [x] выбрать папку результата;
  - [x] выбрать или создать preset;
  - [x] управлять Storyblocks-сессией;
  - [x] стартовать run.
- [x] Сделать paragraph workbench:
  - [x] просмотр intent и queries по абзацу;
  - [x] ручная правка queries;
  - [x] preview найденных ассетов;
  - [x] lock selected asset;
  - [x] reject asset;
  - [x] rerun current paragraph;
  - [x] rerun selected paragraphs.
- [x] Сделать понятные пользовательские настройки:
  - [x] использовать ли видео;
  - [x] использовать ли Storyblocks images;
  - [x] использовать ли free images;
  - [x] насколько строго отбирать результаты;
  - [x] насколько медленно работать в браузере;
  - [x] какие источники использовать.
- [x] Вынести технические параметры в advanced-pane:
  - [x] concurrency;
  - [x] таймауты;
  - [x] Gemini thresholds;
  - [x] размер кэша;
  - [x] browser profile path;
  - [x] retry budget;
  - [x] top-K для relevance stage.
- [x] Сделать отдельную панель управления Storyblocks-сессией:
  - [x] открыть браузер;
  - [x] войти;
  - [x] выйти;
  - [x] сменить аккаунт;
  - [x] проверить состояние сессии;
  - [x] очистить профиль.
- [x] Сделать сохранение, загрузку, экспорт и импорт пресетов.
- [x] Сделать управление Gemini key:
  - [x] задать;
  - [x] заменить;
  - [x] проверить валидность;
  - [x] хранить безопасно.
- [x] Сделать selector провайдеров по режимам проекта.
- [x] Сделать output preview и сводку run до старта.
- [x] Добавить run history и повторный запуск по прошлому проекту.

### Проверка фазы 7

- [x] Обычный пользователь проходит сценарий без CLI.
- [x] Пользователь понимает, что система работает по абзацам и может управлять каждым абзацем отдельно.
- [x] Уверенный пользователь находит и понимает advanced-настройки.
- [x] Пресеты и Gemini key реально сохраняются и применяются.

## Фаза 8. Прогресс, контроль run и операционное управление

- [ ] Ввести многоуровневый прогресс:
  - [ ] проект;
  - [ ] абзацы;
  - [ ] текущий провайдер;
  - [ ] текущий ассет;
  - [ ] текущая стадия.
- [ ] Показать live-состояния:
  - [ ] ingestion и валидация абзацев;
  - [ ] extraction intent;
  - [ ] поиск;
  - [ ] ожидание ручного логина;
  - [ ] скачивание;
  - [ ] проверка relevance;
  - [ ] сохранение;
  - [ ] ожидание ручного решения пользователя.
- [ ] Добавить действия управления:
  - [ ] пауза;
  - [ ] продолжить;
  - [ ] остановить после текущего абзаца;
  - [ ] прервать;
  - [ ] повторить неудачные;
  - [ ] повторить выбранные абзацы.
- [ ] Сделать прозрачную модель остановки и возобновления.
- [ ] Сделать журнал ошибок и предупреждений в UI с привязкой к абзацу, провайдеру и запросу.
- [ ] Сделать indicator session health.
- [ ] Сделать уведомления о состояниях:
  - [ ] challenge detected;
  - [ ] expired session;
  - [ ] Gemini quota issue;
  - [ ] no results;
  - [ ] download failed;
  - [ ] resumed from checkpoint.

### Проверка фазы 8

- [ ] Run можно безопасно поставить на паузу и возобновить.
- [ ] Пользователь в каждый момент понимает, что происходит с конкретным абзацем.
- [ ] Ручное вмешательство не ломает pipeline.
- [ ] Ошибки и предупреждения достаточно детальны для повторного запуска только нужных абзацев.

## Фаза 9. Надежность, тестирование, QA и hardening

- [x] Ввести unit tests на:
  - [x] domain-модели;
  - [x] parser нумерованных абзацев;
  - [x] query generation;
  - [x] paragraph intent builder;
  - [x] settings manager;
  - [x] manifest builder;
  - [x] selection policy.
- [x] Ввести integration tests на:
  - [x] provider adapters;
  - [x] storage;
  - [x] cache;
  - [x] session state machine;
  - [x] resume logic;
  - [x] paragraph rerun logic.
- [x] Ввести browser automation tests на mock/stub страницах.
- [x] Ввести selector-contract regression tests на стабах или сохраненных HTML fixtures.
- [x] Подготовить отдельный ручной smoke suite на реальной сессии.
- [x] Ввести failure tests:
  - [x] истекшая сессия;
  - [x] login required;
  - [x] ручной challenge;
  - [x] browser profile locked другим процессом;
  - [x] отсутствующий или неподдерживаемый browser channel;
  - [x] пустой search result;
  - [x] сетевой timeout;
  - [x] частичный download;
  - [x] перезапуск приложения во время run;
  - [x] сценарий с ошибочной нумерацией;
  - [x] потеря locked asset state.
- [ ] Ввести regression suite на качество extraction и selection.
- [ ] Ввести performance и soak tests на длинных сценариях.
- [x] Ввести crash recovery tests.
- [x] Проверить, что удаление legacy free-video ветки не ломает free-image режим и mixed-mode.

### Проверка фазы 9

- [x] Automated test suite стабильна.
- [ ] Ручной acceptance checklist пройден.
- [ ] Есть release candidate сборка, пригодная для пилотного использования.
- [ ] Сценарии с десятками и сотнями абзацев проходят без деградации UX и потери состояния.

## Фаза 10. Упаковка, документация и выпуск

- [x] Подготовить installer или portable build.
- [x] Подготовить onboarding:
  - [x] первый запуск;
  - [x] настройка Gemini;
  - [x] вход в Storyblocks;
  - [x] проверка наличия поддерживаемого Chrome/Edge channel;
  - [x] создание браузерного профиля;
  - [x] создание первого пресета;
  - [x] загрузка первого сценария;
  - [x] понимание paragraph-first flow.
- [x] Подготовить troubleshooting guide:
  - [x] невалидная сессия;
  - [x] профиль занят другим браузерным процессом;
  - [x] отсутствует поддерживаемый browser channel;
  - [x] ручной логин;
  - [x] пустые результаты;
  - [x] плохая нумерация абзацев;
  - [x] quota;
  - [x] зависшая загрузка;
  - [x] resume;
  - [x] rerun only failed paragraphs.
- [x] Подготовить migration guide от текущего репозитория к новому приложению.
- [x] Подготовить release checklist.
- [ ] Провести pilot rollout на нескольких сценариях и профилях.

### Проверка фазы 10

- [ ] Сборка устанавливается с нуля.
- [x] Документация покрывает ключевые пользовательские сценарии.
- [ ] Пилотный пользователь проходит путь без помощи разработчика.
- [x] Пользователь понимает, как работать с результатами по абзацам и повторно запускать только нужные части сценария.

## Явное удаление и замена текущих частей

- [ ] Удалить как product-path текущий `video_fetcher.py` после извлечения reusable infra.
- [ ] Удалить старую free-video схему выходных данных и связанный кэш.
- [ ] Удалить обязательность legacy-видео-зависимостей из базового сценария установки.
- [ ] Заменить `keyword_extractor.py` не на segment planner, а на shared paragraph parser + paragraph intent planner.
- [ ] Сохранить и переработать `read_script_paragraphs` как основу будущего ingestion слоя.
- [ ] Разобрать `image_fetcher.py` на reusable free-image subsystem вместо второго монолитного CLI.

## Рекомендованный порядок реализации

- [ ] Сначала выполнить фазы 0-2.
- [ ] Затем выполнить фазу 3.
- [ ] Затем выполнить фазу 4 как основу Storyblocks-пути.
- [ ] Затем выполнить фазы 5-6 как ядро paragraph-based media engine.
- [ ] Затем выполнить фазы 7-8 как пользовательский слой.
- [ ] Затем выполнить фазы 9-10 как стабилизацию и выпуск.
- [ ] Для MVP рекомендовано:
  - [ ] не включать Storyblocks audio в первый релиз без отдельного подтверждения;
  - [ ] не делать авто-слотинг 15-20 секунд обязательной логикой первого релиза;
  - [ ] сначала довести до качества paragraph-first подбор.

## Post-MVP. Тайминги, аудио и расширение экосистемы

- [ ] Добавить режим paragraph slot expansion для длинных абзацев:
  - [ ] использовать `estimated_duration_seconds`;
  - [ ] разбивать один абзац на несколько media slots;
  - [ ] поддержать правило `1 primary video на 15-20 секунд` как дополнительный режим.
- [ ] Добавить Storyblocks audio:
  - [ ] определить, как audio вписывается в paragraph model и timing slots;
  - [ ] добавить отдельный Storyblocks audio adapter;
  - [ ] добавить audio selection policy;
  - [ ] добавить UI-настройки для аудио;
  - [ ] добавить manifest-поля для audio.
- [ ] Добавить экспорт результатов в форматы, удобные для следующего этапа монтажа.
- [ ] Добавить дополнительные провайдеры изображений и режимы smart fallback.
