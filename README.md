# vid-img-downloader

Desktop tool for paragraph-first media sourcing from Storyblocks and free image providers.

## Quick Start

- Supported local runtime: `CPython 3.13`
- Create or recreate `.venv` with a real `python.exe` from a CPython 3.13 install:
  `C:\path\to\python.exe -m venv .venv`
- Install dependencies into that environment:
  `.venv\Scripts\python.exe -m pip install -r requirements.txt`
- Install Ruff in the same environment:
  `.venv\Scripts\python.exe -m pip install ruff`
- Run tests:
  `.venv\Scripts\python.exe -m unittest discover -s tests`
- Run startup smoke:
  `.venv\Scripts\python.exe -m app --smoke --no-gui`
- Launch the app:
  `.venv\Scripts\python.exe -m app`

## Canonical Architecture

- `app/` - composition root and application entry points
- `ui/` - desktop adapters only; UI should not reach into repositories or pipeline internals
- `pipeline/` - ingestion, intent generation, media selection, run execution
- `providers/` - provider registry and free-image integrations
- `browser/` - Storyblocks browser/session automation
- `storage/` - workspace layout, repositories, JSON persistence
- `services/` - errors, events, settings, secrets, Gemini adapter
- `domain/` - source-of-truth models and enums

## Supported Runtime Paths

- Script import is heuristic-only and does not call Gemini on the open/start path
- Gemini intent enrichment is an explicit follow-up action from the UI/application layer
- Storyblocks auth diagnostics surface reason codes and reset flow through the session panel
- Media runs use the configured orchestrator; default paragraph concurrency stays conservative

## Main Commands

- `.venv\Scripts\python.exe -m unittest tests.test_image_provider_architecture`
- `.venv\Scripts\python.exe -m unittest tests.test_media_pipeline`
- `.venv\Scripts\python.exe -m unittest discover -s tests`
- `.venv\Scripts\python.exe -m ruff check .`
- `.venv\Scripts\python.exe -m app --smoke --no-gui`
- `.venv\Scripts\python.exe -m app --workspace tmp_manual_smoke_workspace`

## Change Rules

- Add new behavior through `DesktopApplication` or controller-level use cases, not ad hoc UI wiring
- Keep expensive AI calls off import/start paths
- Treat persisted payload changes as schema changes; update tests and migration notes
- Prefer new-core modules over `legacy_core/` unless a compatibility adapter is required

See `CONTRIBUTING.md` and `docs/ai/` for subsystem rules.
