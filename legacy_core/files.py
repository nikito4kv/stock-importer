from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .common import slugify


def sha256_bytes(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def resolve_output_json_path(path_value: str | Path) -> Path:
    out = Path(path_value)
    if str(out.parent) in {"", "."}:
        out = Path("output") / out
    if out.suffix.lower() != ".json":
        out = out.with_suffix(".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def build_run_dir(
    root: Path,
    run_prefix: str,
    *,
    default_prefix: str = "run",
    max_prefix_len: int = 24,
) -> tuple[str, Path]:
    prefix = slugify(run_prefix or default_prefix, max_len=max_prefix_len, default=default_prefix)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base = f"{prefix}_{stamp}"

    for attempt in range(100):
        run_id = base if attempt == 0 else f"{base}_{attempt:02d}"
        run_dir = root / run_id
        if not run_dir.exists():
            run_dir.mkdir(parents=True, exist_ok=False)
            return run_id, run_dir

    raise RuntimeError("Unable to create unique run directory")


def write_hashed_temp_file(
    temp_dir: Path,
    raw_bytes: bytes,
    extension: str,
) -> tuple[str, Path]:
    sha256 = sha256_bytes(raw_bytes)
    path = temp_dir / f"{sha256}{extension}"
    if not path.exists():
        path.write_bytes(raw_bytes)
    return sha256, path
