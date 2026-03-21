# Browser Layer

- Runtime keeps exactly one managed Storyblocks profile and reuses it for login, automation, and imported Chromium sessions.
- Legacy workspaces may still contain multiple profile JSON files, but `BrowserProfileRegistry.get_or_create_singleton()` selects one deterministic profile and higher layers do not expose profile switching.
- `session.py` is the Storyblocks session source of truth
- `storyblocks_backend.py` adapts browser automation into provider search/download contracts
- Keep auth failures explicit with reason codes and diagnostics
