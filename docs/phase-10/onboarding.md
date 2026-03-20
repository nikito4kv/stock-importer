# Phase 10 Onboarding

## Goal

Phase 10 packages the desktop application into a portable bundle that can be
copied to another Windows machine without carrying a local development
environment.

## Portable Bundle Layout

- `app/`, `pipeline/`, `providers/`, `services/`, `storage/`, `ui/`
- `docs/`
- `launch_gui.bat`
- `launch_smoke.bat`
- `setup_portable.ps1`
- `portable_manifest.json`
- `workspace/`

## Smoke Verification

1. Run `launch_smoke.bat`.
2. Confirm the process starts without a local `.venv`.
3. Confirm `workspace/` is created and writable.
4. Review `portable_manifest.json` for the packaged version and entrypoints.

## Notes

- Do not ship local secrets, caches, or browser profiles from development.
- Rebuild the bundle after any schema, runtime dependency, or launcher change.
