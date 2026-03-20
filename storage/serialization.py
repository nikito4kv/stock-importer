from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {}
    last_error: Exception | None = None
    for _attempt in range(4):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            break
        except PermissionError as exc:
            last_error = exc
            time.sleep(0.05)
    else:
        assert last_error is not None
        raise last_error
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        os.replace(temp_path, path)
    except Exception:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
        raise
