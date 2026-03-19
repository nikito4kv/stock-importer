from __future__ import annotations

import json
from pathlib import Path

from .events import AppEvent


class JsonLineEventLogger:
    def __init__(self, log_path: Path):
        self._path = log_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: AppEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(f"{line}\n")
