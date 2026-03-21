# Phase 6 remediation status

Date: 2026-03-22

## Completed cleanup blocks

- `P6-05`: Storyblocks runtime now exposes a singleton managed profile contract end-to-end.
- `P6-02`: provider catalog no longer carries `provider_group`/`priority`, and `mixed_image_fallback` is isolated to legacy settings load.
- `P6-06`: image path no longer depends on shared relevance knobs; video ranking is isolated in `VideoSelectionPolicy`.
- `P6-07`: new manifests no longer serialize `user_locked`, and legacy locked payloads load only through deserialization compatibility.
- Docs/task-plan links were repaired, `p6-04-.md` was renamed to `p6-04.md`, and stale Tk references were removed from active README-level docs.

## Compatibility notes

- Legacy Storyblocks profile JSON payloads still load because unknown fields are ignored on read.
- Legacy settings payloads with `mixed_image_fallback` still normalize into a supported project mode before runtime uses them.
- Legacy manifests with `locked` / `needs_review` statuses and `user_locked` payloads still load for history inspection, but new runtime payloads do not write those fields.
- Root-level `image_fetcher.py` and `video_fetcher.py` still retain their own legacy relevance knobs; they remain out-of-scope desktop compatibility tools.

## Verification snapshot

- `ruff check .` -> OK
- `python -m unittest discover -s tests` -> OK (`162` tests)
- `python -m app --smoke --no-gui` -> OK
- Timed smoke pass -> `1.777s`
- `python -m release_tools.portable --output-dir dist/portable-final --version phase6-final-check` -> OK
- Markdown link scan under `docs/` -> OK

## Expected residual grep hits

- Phase-6 audit/remediation docs still mention removed vocabulary as historical evidence.
- Older optimization/benchmark planning docs still mention deprecated knobs as historical context.
- Production Python code no longer contains live Tk paths, multi-profile Storyblocks APIs, runtime `provider_group`/`priority` semantics, or new `user_locked` writes.
