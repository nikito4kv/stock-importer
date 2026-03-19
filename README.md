# vid-img-downloader

Desktop tool for paragraph-first media sourcing from Storyblocks and free image providers.

## Quick Start

- Create a virtual environment and install dependencies: `python -m pip install -r requirements.txt`
- Run tests: `python -m unittest discover -s tests`
- Run startup smoke: `python -m app --smoke --no-gui`
- Launch the app: `python -m app`

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

- `python -m unittest discover -s tests`
- `python -m app --smoke --no-gui`
- `python -m app --workspace tmp_manual_smoke_workspace`

## Change Rules

- Add new behavior through `DesktopApplication` or controller-level use cases, not ad hoc UI wiring
- Keep expensive AI calls off import/start paths
- Treat persisted payload changes as schema changes; update tests and migration notes
- Prefer new-core modules over `legacy_core/` unless a compatibility adapter is required

See `CONTRIBUTING.md` and `docs/ai/` for subsystem rules.
