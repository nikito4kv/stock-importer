from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILENAME = ".env"


def get_env_path(
    anchor_file: str | Path | None = None,
    env_filename: str = ENV_FILENAME,
) -> Path:
    anchor = Path(anchor_file) if anchor_file is not None else Path.cwd() / "anchor.py"
    return anchor.resolve().parent / env_filename


def load_dotenv(
    dotenv_path: str | Path | None = None,
    *,
    anchor_file: str | Path | None = None,
    env_filename: str = ENV_FILENAME,
) -> Path:
    path = (
        Path(dotenv_path)
        if dotenv_path is not None
        else get_env_path(anchor_file=anchor_file, env_filename=env_filename)
    )
    if not path.exists() or not path.is_file():
        return path

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="utf-8-sig")

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if line.lower().startswith("export "):
            line = line[7:].lstrip()

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if (
            value
            and value[0] in ('"', "'")
            and len(value) >= 2
            and value[-1] == value[0]
        ):
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()

        os.environ[key] = value

    return path
