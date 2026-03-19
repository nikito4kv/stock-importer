from __future__ import annotations

import argparse
import json

from .runtime import DesktopApplication


def main() -> None:
    parser = argparse.ArgumentParser(description="Start desktop application core")
    parser.add_argument("--workspace", help="Workspace root", default=None)
    parser.add_argument("--smoke", action="store_true", help="Run startup smoke output")
    parser.add_argument("--no-gui", action="store_true", help="Do not launch the GUI after startup")
    args = parser.parse_args()

    application = DesktopApplication.create(args.workspace)
    snapshot = application.start()
    if args.smoke:
        print(
            json.dumps(
                {
                    "workspace_root": str(snapshot.workspace_root),
                    "providers": snapshot.providers,
                    "browser_profiles": snapshot.browser_profiles,
                },
                ensure_ascii=False,
            )
        )
        return

    if args.no_gui:
        return

    from ui import DesktopGuiController, launch_desktop_app

    launch_desktop_app(DesktopGuiController(application))


if __name__ == "__main__":
    main()
