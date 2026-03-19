# Contributing

## Workflow

- Keep changes small and seam-oriented
- Add or update regression tests for every new use case or bug fix
- Run `python -m unittest discover -s tests` before shipping
- Keep `legacy_core/` behind adapters; do not add new direct dependencies from new modules

## Layer Rules

- UI -> controller/application use cases only
- Application/runtime -> pipeline, browser, storage services
- Providers/browser integrations stay behind pipeline/provider contracts
- Persisted model changes must keep backward compatibility or include migration coverage

## Runtime Rules

- Do not attach Gemini to import/start paths
- Keep Storyblocks diagnostics explicit; avoid silent auth fallbacks
- Do not hide Qt defects behind broad fallback exceptions
- Do not treat worker counts as product media-count settings

## Test Priorities

- import/open path latency and stability
- run lifecycle and manifest recovery
- Storyblocks auth/session diagnostics
- media selection and payload validation
