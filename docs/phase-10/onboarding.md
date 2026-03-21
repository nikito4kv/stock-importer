# Portable Onboarding

This bundle supports one install path on the target Windows machine.

1. Copy the portable directory to the target machine or extract the portable `.zip`.
2. Make sure `python` is available and Chrome or Edge is installed.
3. Run `setup_portable.ps1`. If PowerShell is restricted, run `setup_portable.bat`.
4. Run `launch_smoke.bat` to verify the bundle after setup.
5. Run `launch_gui.bat` for normal work.
6. Keep the generated `workspace/` folder next to the bundle. The app stores local settings, secrets, logs, and runtime data there.
7. Configure provider credentials inside the app. The portable flow does not require `.env.example`.
