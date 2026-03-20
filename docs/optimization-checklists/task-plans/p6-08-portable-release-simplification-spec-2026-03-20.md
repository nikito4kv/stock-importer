# P6-08. ТЗ на сокращение portable/release слоя с сохранением переносимого bundle

Исходный пункт плана: [phase-6-simplification-checklist.md](./phase-6-simplification-checklist.md)

## Цель

Оставить в проекте только один реальный deploy path: переносимый portable bundle,
который можно собрать, перенести на другую Windows-машину, распаковать, установить
зависимости и запустить без локального dev-окружения. Убрать всё, что создаёт
иллюзию "полноценного release-слоя для широкой дистрибуции", но не нужно для
фактического сценария "собрать -> перенести -> установить -> проверить -> запустить".

## Проблема текущего состояния

Сейчас portable path уже существует, но вокруг него есть лишняя release-обвязка:

- [release_tools/portable.py](../../release_tools/portable.py) копирует в bundle
  весь `docs/` и дополнительные root-файлы, которые не участвуют в установке или
  запуске на целевой машине.
- В bundle попадают `implementation_plan.md` и `.env.example`, хотя текущий
  desktop runtime не использует их как обязательную часть install/start flow.
- [tests/test_phase10_release.py](../../tests/test_phase10_release.py) считает
  частью shipped artifact внутренний
  [docs/phase-10/release-checklist.md](../../docs/phase-10/release-checklist.md),
  хотя это maintainer-документ, а не документ для целевой машины.
- Из-за широкого копирования `docs/` bundle содержит внутренние материалы,
  которые не помогают установке, но раздувают контракт сборки и тесты.

Важно: упрощение release-слоя не означает, что можно вырезать всё "подозрительное"
по имени. Например, `legacy_core/` всё ещё нужен runtime-коду, а `browser/` нужен
для Storyblocks/browser automation path. Удалять их из bundle в P6-08 нельзя.

## Границы задачи

Входит в P6-08:

- упрощение контракта portable bundle в
  [release_tools/portable.py](../../release_tools/portable.py);
- сокращение списка файлов, которые реально копируются в bundle;
- сохранение только install/run entrypoints, нужных для переносимого сценария;
- выравнивание [portable_manifest.json](../../release_tools/portable.py) под новый
  минимальный контракт;
- обновление release-тестов под новый portable contract;
- обновление phase-10 документации под один упрощённый deploy path.

Не входит в P6-08:

- переход на installer/MSI/Setup EXE;
- CI/CD-пайплайн публикации релизов;
- переименование пакета `release_tools` только ради терминологии;
- переписывание runtime-архитектуры, чтобы убрать `legacy_core`;
- ручное редактирование или коммит generated артефактов в `dist/portable/`;
- чистка исторических audit/analysis документов, если они просто лежат в repo и
  не входят в portable bundle.

Практическое решение по scope:

- оставить один builder: `python -m release_tools.portable`;
- оставить один формат доставки: директория bundle плюс optional `.zip`, собранный
  из той же директории;
- оставить только те документы, которые помогают пользователю на целевой машине;
- внутренние release/checklist документы держать в repo при необходимости, но не
  тащить их в bundle автоматически.

## Целевой контракт portable bundle

После P6-08 portable build должен означать строго следующее:

1. Сборка выполняется одной командой:

```bash
python -m release_tools.portable --output-dir dist/portable --version <version>
```

2. На выходе получается директория `vid-img-downloader-portable-<version>/` и,
   если не указан `--no-zip`, архив `vid-img-downloader-portable-<version>.zip`.

3. Поддерживаемый install/start flow на целевой машине ровно один:

- распаковать bundle;
- запустить `setup_portable.ps1` или `setup_portable.bat`;
- запустить `launch_smoke.bat` для быстрой проверки;
- запустить `launch_gui.bat` для работы с приложением.

4. Целевой состав bundle после P6-08:

```text
vid-img-downloader-portable-<version>/
  app/
  browser/
  config/
  domain/
  legacy_core/
  pipeline/
  providers/
  services/
  storage/
  ui/
  docs/
    phase-10/
      onboarding.md
  requirements.txt
  launch_gui.bat
  launch_smoke.bat
  setup_portable.bat
  setup_portable.ps1
  PORTABLE-README.txt
  portable_manifest.json
  workspace/
```

5. В bundle после P6-08 не должны попадать:

- `implementation_plan.md`;
- `.env.example`;
- весь `docs/` целиком;
- `docs/phase-10/release-checklist.md`;
- `.venv`, `__pycache__`, `dist`, `build`, `output`, `recordings`, `workspace`
  из локальной dev-машины;
- любые локальные секреты, браузерные профили, временные файлы и generated caches.

## Где сейчас сидит лишняя release-логика

- Состав bundle, launchers и manifest:
  [release_tools/portable.py](../../release_tools/portable.py)
- Короткий user-facing install doc:
  [docs/phase-10/onboarding.md](../../docs/phase-10/onboarding.md)
- Внутренний release checklist, который сейчас ошибочно выглядит как shipped-doc:
  [docs/phase-10/release-checklist.md](../../docs/phase-10/release-checklist.md)
- Release smoke/manifest/archive assertions:
  [tests/test_phase10_release.py](../../tests/test_phase10_release.py)
- Проверка синхронности bundled runtime sources:
  [tests/test_release_tools_portable.py](../../tests/test_release_tools_portable.py)

## Файлы, которые нельзя вырезать в этом пункте

Эти файлы важны не как release-обвязка, а как реальные runtime-зависимости:

- App bootstrap тянет browser stack:
  [app/bootstrap.py](../../app/bootstrap.py)
- Runtime pipeline всё ещё зависит от `legacy_core`:
  [pipeline/ingestion.py](../../pipeline/ingestion.py),
  [pipeline/intents.py](../../pipeline/intents.py),
  [pipeline/media.py](../../pipeline/media.py)
- Free-image adapters используют legacy providers/utilities:
  [providers/images/clients.py](../../providers/images/clients.py),
  [providers/images/filtering.py](../../providers/images/filtering.py),
  [providers/images/querying.py](../../providers/images/querying.py)
- Секреты в desktop path идут не через `.env`, а через workspace secret store:
  [services/settings_manager.py](../../services/settings_manager.py),
  [services/secrets.py](../../services/secrets.py)

Практический вывод:

- `browser/` и `legacy_core/` сохраняются в bundle;
- `.env.example` не считается обязательной частью portable install path, пока
  desktop runtime реально живёт через `SettingsManager` и `SecretStore`.

## Definition of Done

- В проекте есть один канонический export path: `python -m release_tools.portable`.
- Portable bundle по-прежнему переносится на другую машину и запускается после
  распаковки и `setup_portable.*`.
- В bundle остаются только runtime-директории, `requirements.txt`, launchers,
  `PORTABLE-README.txt`, `portable_manifest.json`, `workspace/` и
  `docs/phase-10/onboarding.md`.
- В bundle больше не попадают `implementation_plan.md`, `.env.example`,
  `docs/phase-10/release-checklist.md` и остальной `docs/`.
- `portable_manifest.json` описывает только реально shipped items и не врёт про
  removed content.
- Тесты вокруг portable path проверяют именно перенос/установку/архивирование, а
  не широкую release-дистрибуцию.
- Документация описывает один упрощённый deploy path и не отсылает к несуществующим
  installer/release сценариям.

## Пошаговый план

### Шаг 1. Зафиксировать минимальный portable contract как единственный поддерживаемый deploy path

Что сделать:

- В явном виде принять, что P6-08 оставляет только один builder entrypoint:
  `python -m release_tools.portable`.
- Зафиксировать, что директория bundle и `.zip` являются двумя формами одного и
  того же артефакта, а не двумя разными release-сценариями.
- Выписать allowlist runtime-директорий, которые обязаны остаться в bundle:
  `app`, `browser`, `config`, `domain`, `legacy_core`, `pipeline`, `providers`,
  `services`, `storage`, `ui`.
- Выписать allowlist root-артефактов bundle: `requirements.txt`, launchers,
  `PORTABLE-README.txt`, `portable_manifest.json`, `workspace/`.
- Выписать единственный user-facing doc, который должен ехать в bundle:
  `docs/phase-10/onboarding.md`.
- Зафиксировать denylist для этой задачи: `implementation_plan.md`,
  `.env.example`, `docs/phase-10/release-checklist.md`, прочие `docs/*`,
  локальные cache/venv/secrets.

Файлы:

- [docs/optimization-checklists/phase-6-simplification-checklist.md](./phase-6-simplification-checklist.md)
- [release_tools/portable.py](../../release_tools/portable.py)
- [docs/phase-10/onboarding.md](../../docs/phase-10/onboarding.md)
- [docs/phase-10/release-checklist.md](../../docs/phase-10/release-checklist.md)

Что должно получиться:

- У команды есть точный target contract, по которому можно оценивать любые
  изменения в portable bundle.
- Дальнейшие правки не превращаются в хаотичное "удалим всё, что выглядит
  release-подобным".

Как проверить:

- Сравнить текущие `INCLUDED_DIRS` и `INCLUDED_FILES` в
  [release_tools/portable.py](../../release_tools/portable.py) с целевым списком.
- Любой файл, который попадает в bundle и не входит в allowlist, считать
  отклонением от P6-08.

Практический совет:

- Не начинайте с `dist/portable/`. Это generated output, а не источник правды.
  Сначала нужно изменить контракт в исходниках, потом уже пересобрать bundle.

### Шаг 2. Сузить builder до явного allowlist и перестать копировать лишние документы

Что сделать:

- В [release_tools/portable.py](../../release_tools/portable.py) разделить
  текущую логику включения на явные категории:
  runtime directories, root files, document files.
- Убрать из состава bundle копирование всего `docs/`.
- Добавить точечное копирование только `docs/phase-10/onboarding.md`.
- Убрать `implementation_plan.md` из bundled root files.
- Убрать `.env.example` из bundled root files.
- Оставить `requirements.txt` как обязательный файл для `setup_portable.*`.
- Сохранить `browser/` и `legacy_core/` в bundle, несмотря на их "опасные"
  названия, потому что они реально нужны runtime-коду.
- Не менять naming output (`vid-img-downloader-portable-<version>`) и не убирать
  `--no-zip`; это не лишняя release-сложность, а полезная функциональность для
  проверки и переноса.

Файлы:

- [release_tools/portable.py](../../release_tools/portable.py)
- [app/bootstrap.py](../../app/bootstrap.py)
- [pipeline/ingestion.py](../../pipeline/ingestion.py)
- [pipeline/intents.py](../../pipeline/intents.py)
- [pipeline/media.py](../../pipeline/media.py)
- [providers/images/clients.py](../../providers/images/clients.py)
- [providers/images/filtering.py](../../providers/images/filtering.py)
- [providers/images/querying.py](../../providers/images/querying.py)
- [services/settings_manager.py](../../services/settings_manager.py)
- [services/secrets.py](../../services/secrets.py)

Что должно получиться:

- Сборщик bundle больше не тащит в архив внутренние документы и лишние root-файлы.
- Контракт bundle становится читаемым: по исходнику сразу видно, что реально
  должно оказаться на целевой машине.
- При этом bundle остаётся полным с точки зрения runtime-зависимостей.

Как проверить:

- Собрать bundle в тестовый каталог:

```bash
python -m release_tools.portable --output-dir dist/portable --version p6-08-smoke
```

- Проверить, что в корне bundle есть `requirements.txt`, launchers, manifest,
  `PORTABLE-README.txt` и `workspace/`.
- Проверить, что в bundle есть `browser/` и `legacy_core/`.
- Проверить, что в bundle нет `implementation_plan.md`, `.env.example` и
  `docs/phase-10/release-checklist.md`.
- Проверить, что `docs/` внутри bundle больше не содержит лишние документы.

Практический совет:

- Не ориентируйтесь на название директории. `legacy_core/` сейчас не "мусор",
  а транзитивная runtime-зависимость. Удалять её из bundle, пока есть прямые
  импорты в `pipeline/` и `providers/`, нельзя.

### Шаг 3. Оставить только минимальные install/run entrypoints и выровнять manifest

Что сделать:

- Сохранить генерацию `setup_portable.ps1` и `setup_portable.bat` как install path
  для целевой машины.
- Сохранить `launch_smoke.bat` как обязательный smoke-check после распаковки.
- Сохранить `launch_gui.bat` как основной runtime entrypoint.
- Обновить текст `PORTABLE-README.txt`, который генерируется в
  [release_tools/portable.py](../../release_tools/portable.py), чтобы он ссылался
  на конкретный файл `docs/phase-10/onboarding.md`, а не на "полный docs tree".
- Пересобрать `portable_manifest.json`, чтобы он описывал новый минимальный
  состав bundle. Если текущие поля `included_directories` и `included_files`
  становятся двусмысленными, разделить их на более точные поля, например:
  `included_runtime_directories`, `included_root_files`, `included_document_files`.
- Убедиться, что manifest больше не декларирует `docs` целиком, если теперь
  копируется только `docs/phase-10/onboarding.md`.

Файлы:

- [release_tools/portable.py](../../release_tools/portable.py)
- [docs/phase-10/onboarding.md](../../docs/phase-10/onboarding.md)

Что должно получиться:

- Bundle можно распаковать и использовать без лишних release-артефактов.
- Человек на целевой машине видит ровно те entrypoints, которые реально нужны:
  setup, smoke, GUI.
- Manifest становится полезным техническим контрактом, а не размытой сводкой.

Как проверить:

- После сборки открыть `portable_manifest.json` и проверить, что он содержит
  только актуальные shipped items.
- Проверить, что `PORTABLE-README.txt` ведёт к актуальному onboarding flow.
- На распакованном bundle выполнить:

```powershell
.\setup_portable.ps1
.\launch_smoke.bat
```

- Убедиться, что smoke-путь стартует без ошибок и создаёт/использует локальный
  `workspace/`.

Практический совет:

- Не вырезайте `launch_smoke.bat` как "лишний release script". Это самый дешёвый
  и надёжный install-check после распаковки bundle на новой машине.

### Шаг 4. Сузить release-тесты до portable contract и убрать проверки широкого release-слоя

Что сделать:

- В [tests/test_phase10_release.py](../../tests/test_phase10_release.py) оставить
  только те проверки, которые защищают переносимый bundle:
  наличие runtime-файлов, launchers, manifest, onboarding-doc, архива и отсутствие
  dev-artifacts.
- Удалить или переписать assertions, которые требуют наличия в archive/bundle
  внутреннего `docs/phase-10/release-checklist.md`.
- Добавить явные negative assertions на отсутствие:
  `implementation_plan.md`, `.env.example`, лишнего `docs/*`.
- В [tests/test_release_tools_portable.py](../../tests/test_release_tools_portable.py)
  сохранить идею "bundled sources совпадают с текущими runtime source files", потому
  что это защищает от тихого рассинхрона builder-а и кода.
- При необходимости добавить проверку, что manifest отражает новый состав bundle,
  а zip содержит те же ключевые файлы, что и директория bundle.

Файлы:

- [tests/test_phase10_release.py](../../tests/test_phase10_release.py)
- [tests/test_release_tools_portable.py](../../tests/test_release_tools_portable.py)
- [release_tools/portable.py](../../release_tools/portable.py)

Что должно получиться:

- Тесты валятся только на реальных regressions в portable transfer/install path.
- Тесты больше не тянут проект обратно к широкому release-контракту, который
  уже не нужен.

Как проверить:

- Выполнить:

```bash
python -m unittest tests.test_phase10_release tests.test_release_tools_portable
```

- Убедиться, что тесты подтверждают:
  присутствие обязательных runtime items;
  присутствие onboarding doc;
  отсутствие удалённых release-артефактов;
  корректность архива и manifest.

Практический совет:

- Builder и tests меняйте в одном и том же change set. Если сначала сузить
  builder, а тесты оставить старыми, получится ложный "красный" билд без
  полезного сигнала.

### Шаг 5. Переписать документацию под один реальный сценарий "перенести и установить"

Что сделать:

- В [docs/phase-10/onboarding.md](../../docs/phase-10/onboarding.md) описать один
  канонический путь:
  собрать bundle -> перенести директорию или zip -> распаковать -> запустить
  `setup_portable.*` -> выполнить `launch_smoke.bat` -> запустить `launch_gui.bat`.
- Явно убрать из onboarding любые намёки на альтернативный installer/release path,
  если такой path больше не поддерживается.
- В [docs/phase-10/release-checklist.md](../../docs/phase-10/release-checklist.md)
  оставить только internal maintainer checklist вокруг portable export, если он
  ещё нужен команде. Если документ остаётся, он должен говорить именно о portable
  bundle, а не о широком "релизе для всех".
- Проверить, что ни один документ не рекомендует использовать `.env.example` как
  обязательную часть desktop install flow, если фактическая работа идёт через
  workspace settings и secret store.

Файлы:

- [docs/phase-10/onboarding.md](../../docs/phase-10/onboarding.md)
- [docs/phase-10/release-checklist.md](../../docs/phase-10/release-checklist.md)
- [services/settings_manager.py](../../services/settings_manager.py)
- [services/secrets.py](../../services/secrets.py)

Что должно получиться:

- Пользовательская документация совпадает с реальным bundle contract.
- Внутренний maintainer-doc больше не притворяется частью shipped bundle.
- У команды не остаётся второго "воображаемого" release path в текстах.

Как проверить:

- Прочитать onboarding сверху вниз и проверить, что он воспроизводим без доступа
  к repo checkout.
- Прочитать release-checklist и убедиться, что он не требует артефактов, которые
  больше не входят в bundle.
- Поискать по phase-10 docs слова `installer`, `.env.example`, `release candidate`,
  `full docs`, если они больше не отражают реальный path.

Практический совет:

- Разделяйте docs для пользователя и docs для мейнтейнера. Как только внутренний
  checklist попадает в bundle, он снова начинает определять публичный release
  contract, и задача P6-08 откатывается назад.

### Шаг 6. Провести end-to-end проверку не на repo, а на собранном bundle

Что сделать:

- Собрать portable bundle с фиксированной версией.
- Распаковать его в чистую временную директорию, не используя исходный repo как
  runtime source.
- Выполнить install flow через `setup_portable.ps1` или `setup_portable.bat`.
- Выполнить `launch_smoke.bat`.
- Проверить, что `workspace/` создаётся и writable.
- Проверить, что `portable_manifest.json` соответствует реальному содержимому
  директории и архива.
- При возможности дополнительно выполнить `launch_gui.bat` и убедиться, что
  desktop app стартует на собранном bundle.

Файлы:

- [release_tools/portable.py](../../release_tools/portable.py)
- [tests/test_phase10_release.py](../../tests/test_phase10_release.py)
- [tests/test_release_tools_portable.py](../../tests/test_release_tools_portable.py)
- [docs/phase-10/onboarding.md](../../docs/phase-10/onboarding.md)

Что должно получиться:

- Bundle по-прежнему можно перенести на другую машину.
- Установка/распаковка остаётся рабочей.
- Лишний release-слой действительно исчезает не только из кода, но и из
  фактического сценария использования.

Как проверить:

- Выполнить:

```bash
python -m release_tools.portable --output-dir dist/portable --version p6-08-e2e
python -m unittest tests.test_phase10_release tests.test_release_tools_portable
```

- Затем на распакованном bundle выполнить:

```powershell
.\setup_portable.ps1
.\launch_smoke.bat
```

- Проверить:
  есть ли `portable_manifest.json`;
  есть ли `docs/phase-10/onboarding.md`;
  отсутствуют ли `implementation_plan.md`, `.env.example`,
  `docs/phase-10/release-checklist.md`;
  появился ли локальный `workspace/`.

Практический совет:

- Проверка из исходного repo недостаточна. Если тестировать только checkout,
  можно не заметить, что builder перестал копировать важный файл или, наоборот,
  тащит в bundle лишнее.

## Критерии приёмки для code review

- В diff нет ручных правок внутри `dist/portable/`; меняются только исходники и
  тесты/документы.
- В [release_tools/portable.py](../../release_tools/portable.py) видно явный и
  узкий allowlist bundled content.
- В тестах нет зависимостей от внутренних release-документов как от shipped files.
- В docs нет второго альтернативного release path.
- После изменений bundle остаётся переносимым и проверяемым через smoke-launcher.

## Основные риски и как их избежать

- Риск: случайно вырезать `legacy_core/` или `browser/` вместе с "лишним release".
  Как избежать: проверять реальные импорты в `app/`, `pipeline/`, `providers/`
  до удаления из bundle.
- Риск: убрать `.env.example`, но оставить документацию, будто она обязательна.
  Как избежать: синхронно обновить onboarding и release-checklist.
- Риск: перестать копировать весь `docs/`, но забыть поправить manifest и tests.
  Как избежать: builder, manifest и tests менять одним пакетом.
- Риск: тесты проходят в repo, но portable bundle реально неполный.
  Как избежать: обязательно делать проверку на распакованном артефакте.
