# Phase 10 Release Checklist

- Build the portable bundle from a clean working tree snapshot.
- Verify `launch_gui.bat`, `launch_smoke.bat`, and `setup_portable.ps1` are
  present in the bundle root.
- Verify `portable_manifest.json` includes the expected version and
  entrypoints.
- Verify `docs/phase-10/onboarding.md` is packaged in both the bundle
  directory and the archive.
- Run the smoke launcher on the packaged bundle.
- Confirm `.venv`, `__pycache__`, and local secrets are not included.
- Archive the bundle and confirm the archive contents match the manifest.
