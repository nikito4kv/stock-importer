# Repository Guidelines

## Project Structure & Module Organization

This repository is a Python 3.13 desktop application for paragraph-first media sourcing. Core layers are organized by responsibility:

- `app/`: bootstrap, runtime wiring, and `python -m app` entrypoints
- `ui/`: Qt/Tk desktop adapters and controller-facing presentation code
- `pipeline/`: ingestion, intent generation, orchestration, and media flow
- `providers/` and `browser/`: external media/provider integrations and Storyblocks automation
- `storage/`, `services/`, `domain/`: persistence, shared services, and source-of-truth models
- `tests/`: `unittest` suites plus HTML fixtures under `tests/fixtures/`
- `docs/`: product, audit, and technical notes

Prefer new code in the layered modules above; treat `legacy_core/` as compatibility code behind adapters.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt`: install runtime dependencies
- `python -m pip install ruff`: install the linter used in CI
- `ruff check .`: run lint and import-order checks
- `python -m unittest discover -s tests`: run the full test suite
- `python -m app --smoke --no-gui`: startup smoke test without launching the GUI
- `python -m app`: launch the desktop app locally

## Coding Style & Naming Conventions

Use 4-space indentation, type hints, and Pythonic naming: `snake_case` for functions/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants. Keep lines within Ruff’s 88-character limit. Follow existing boundary rules: UI talks to controller/application use cases, not directly to pipeline or storage internals. Keep expensive AI calls off import/start paths.

## Testing Guidelines

Tests use the standard library `unittest` framework. Add or update regression tests for every bug fix and new use case. Name files `tests/test_*.py` and test methods `test_*`. Keep fixtures under `tests/fixtures/` when a flow depends on stable sample inputs. Run both `ruff check .` and `python -m unittest discover -s tests` before opening a PR.

## Commit & Pull Request Guidelines

Visible Git history is currently minimal (`init`), so follow a simple convention: short, imperative commit subjects such as `Add Storyblocks session timeout coverage`. Keep commits focused and seam-oriented. PRs should include a concise description, testing performed, and any notes about schema, settings, or UI behavior changes. Include screenshots only when UI behavior changes materially.

## Configuration & Safety Notes

Use `.env.example` as the starting point for local configuration. Do not commit secrets, generated workspaces, or provider credentials. If you change persisted payloads or workspace layout, treat that as a schema change and add migration or backward-compatibility coverage.
