# Portable Export Checklist

Internal maintainer checklist for the only supported deploy path: the portable bundle.

- Build with `python -m release_tools.portable --output-dir dist/portable --version <version>`.
- Verify the bundle contains runtime directories, `requirements.txt`, launchers, `PORTABLE-README.txt`, `portable_manifest.json`, `workspace/`, and `docs/phase-10/onboarding.md`.
- Verify the bundle does not contain `.env.example`, `implementation_plan.md`, or `docs/phase-10/release-checklist.md`.
- Extract the bundle into a clean directory, run `setup_portable.ps1` or `setup_portable.bat`, then run `launch_smoke.bat`.
- Optionally run `launch_gui.bat` and confirm runtime state stays under the local `workspace/` directory.
