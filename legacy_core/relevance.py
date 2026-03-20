from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def clean_model_text(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*\n", "", cleaned)
        cleaned = re.sub(r"\n```\s*$", "", cleaned).strip()
    return cleaned


def parse_relevance_response(raw_text: str) -> tuple[bool, float, str]:
    cleaned = clean_model_text(raw_text)
    if not cleaned:
        raise ValueError("Empty relevance response")

    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("Relevance response is not a JSON object")

    is_match = bool(parsed.get("match"))
    score_raw = parsed.get("score")
    reason = re.sub(r"\s+", " ", str(parsed.get("reason") or "")).strip()

    if isinstance(score_raw, (int, float, str)):
        try:
            score = float(score_raw)
        except ValueError:
            score = 0.0
    else:
        score = 0.0

    score = max(0.0, min(1.0, score))
    if not reason:
        reason = "No reason provided"
    return is_match, score, reason[:240]


class SimpleRateLimiter:
    def __init__(self, max_rps: float):
        self._max_rps = max(0.1, float(max_rps))
        self._interval = 1.0 / self._max_rps
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                delay = self._next_allowed - now
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed = now + self._interval


class ImageRelevanceCache:
    def __init__(self, cache_path: str | Path):
        self._path = Path(cache_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS relevance_cache (
                keyword_norm TEXT NOT NULL,
                image_sha256 TEXT NOT NULL,
                model_name TEXT NOT NULL,
                is_match INTEGER NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(keyword_norm, image_sha256, model_name)
            )
            """
        )
        self._conn.commit()

    def get(
        self,
        keyword_norm: str,
        image_sha256: str,
        model_name: str,
    ) -> tuple[bool, float, str] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT is_match, score, reason
                FROM relevance_cache
                WHERE keyword_norm = ? AND image_sha256 = ? AND model_name = ?
                """,
                (keyword_norm, image_sha256, model_name),
            ).fetchone()
        if row is None:
            return None
        return bool(row[0]), float(row[1]), str(row[2])

    def set(
        self,
        keyword_norm: str,
        image_sha256: str,
        model_name: str,
        is_match: bool,
        score: float,
        reason: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO relevance_cache
                (keyword_norm, image_sha256, model_name, is_match, score, reason, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(keyword_norm, image_sha256, model_name)
                DO UPDATE SET
                    is_match = excluded.is_match,
                    score = excluded.score,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    keyword_norm,
                    image_sha256,
                    model_name,
                    1 if is_match else 0,
                    float(score),
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class VideoRelevanceCache:
    def __init__(self, cache_path: str | Path):
        self._path = Path(cache_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS video_relevance_cache (
                keyword_norm TEXT NOT NULL,
                media_sha256 TEXT NOT NULL,
                model_name TEXT NOT NULL,
                frame_samples INTEGER NOT NULL,
                sampler_version TEXT NOT NULL,
                is_match INTEGER NOT NULL,
                score REAL NOT NULL,
                reason TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(
                    keyword_norm,
                    media_sha256,
                    model_name,
                    frame_samples,
                    sampler_version
                )
            )
            """
        )
        self._conn.commit()

    def get(
        self,
        keyword_norm: str,
        media_sha256: str,
        model_name: str,
        frame_samples: int,
        sampler_version: str,
    ) -> tuple[bool, float, str] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT is_match, score, reason
                FROM video_relevance_cache
                WHERE keyword_norm = ?
                  AND media_sha256 = ?
                  AND model_name = ?
                  AND frame_samples = ?
                  AND sampler_version = ?
                """,
                (
                    keyword_norm,
                    media_sha256,
                    model_name,
                    int(frame_samples),
                    sampler_version,
                ),
            ).fetchone()
        if row is None:
            return None
        return bool(row[0]), float(row[1]), str(row[2])

    def set(
        self,
        keyword_norm: str,
        media_sha256: str,
        model_name: str,
        frame_samples: int,
        sampler_version: str,
        is_match: bool,
        score: float,
        reason: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO video_relevance_cache
                (
                    keyword_norm,
                    media_sha256,
                    model_name,
                    frame_samples,
                    sampler_version,
                    is_match,
                    score,
                    reason,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    keyword_norm,
                    media_sha256,
                    model_name,
                    frame_samples,
                    sampler_version
                )
                DO UPDATE SET
                    is_match = excluded.is_match,
                    score = excluded.score,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (
                    keyword_norm,
                    media_sha256,
                    model_name,
                    int(frame_samples),
                    sampler_version,
                    1 if is_match else 0,
                    float(score),
                    reason,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
