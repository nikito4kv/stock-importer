from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Lock

from .clients import SearchCandidate


def _serialize_candidate(candidate: SearchCandidate) -> dict[str, object]:
    return {
        "source": candidate.source,
        "url": candidate.url,
        "referrer_url": candidate.referrer_url,
        "query_used": candidate.query_used,
        "license_name": candidate.license_name,
        "license_url": candidate.license_url,
        "author": candidate.author,
        "commercial_allowed": candidate.commercial_allowed,
        "attribution_required": candidate.attribution_required,
        "rank_hint": candidate.rank_hint,
    }


def _deserialize_candidate(data: dict[str, object]) -> SearchCandidate:
    raw_rank_hint = data.get("rank_hint", 0.0)
    try:
        rank_hint = float(raw_rank_hint)
    except (TypeError, ValueError):
        rank_hint = 0.0
    return SearchCandidate(
        source=str(data.get("source", "")),
        url=str(data.get("url", "")),
        referrer_url=str(data.get("referrer_url", "")).strip() or None,
        query_used=str(data.get("query_used", "")),
        license_name=str(data.get("license_name", "")),
        license_url=str(data.get("license_url", "")).strip() or None,
        author=str(data.get("author", "")).strip() or None,
        commercial_allowed=bool(data.get("commercial_allowed", False)),
        attribution_required=bool(data.get("attribution_required", False)),
        rank_hint=rank_hint,
    )


class SearchResultCache:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS search_cache (provider_id TEXT, query TEXT, limit_value INTEGER, payload TEXT, PRIMARY KEY(provider_id, query, limit_value))"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False)

    def get(self, provider_id: str, query: str, limit_value: int) -> list[SearchCandidate] | None:
        with self._lock:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT payload FROM search_cache WHERE provider_id = ? AND query = ? AND limit_value = ?",
                    (provider_id, query, int(limit_value)),
                ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, list):
            return None
        return [_deserialize_candidate(item) for item in payload if isinstance(item, dict)]

    def set(self, provider_id: str, query: str, limit_value: int, candidates: list[SearchCandidate]) -> None:
        payload = json.dumps([_serialize_candidate(item) for item in candidates], ensure_ascii=False)
        with self._lock:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO search_cache(provider_id, query, limit_value, payload) VALUES (?, ?, ?, ?)",
                    (provider_id, query, int(limit_value), payload),
                )
                connection.commit()

    def close(self) -> None:
        return None


class MetadataCache:
    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS metadata_cache (cache_key TEXT PRIMARY KEY, payload TEXT)"
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False)

    def get(self, cache_key: str) -> dict[str, object] | None:
        with self._lock:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT payload FROM metadata_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            return None
        return payload

    def set(self, cache_key: str, payload: dict[str, object]) -> None:
        with self._lock:
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata_cache(cache_key, payload) VALUES (?, ?)",
                    (cache_key, json.dumps(payload, ensure_ascii=False)),
                )
                connection.commit()

    def close(self) -> None:
        return None


__all__ = ["MetadataCache", "SearchResultCache"]
