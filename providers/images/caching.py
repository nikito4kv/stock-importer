from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from threading import Lock
from typing import Callable

from .clients import SearchCandidate

logger = logging.getLogger(__name__)

SQLITE_JOURNAL_MODE = "WAL"
SQLITE_SYNCHRONOUS_MODE = "NORMAL"
SQLITE_BUSY_TIMEOUT_MS = 5_000
SQLITE_FOREIGN_KEYS_ENABLED = True
SEARCH_RESULT_CACHE_TTL_SECONDS = 6 * 60 * 60
METADATA_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
CACHE_CLEANUP_EVERY_OPERATIONS = 32


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


class SQLiteCacheBase:
    def __init__(
        self,
        path: str | Path,
        *,
        table_name: str,
        create_table_sql: str,
        ttl_seconds: int,
        time_fn: Callable[[], float] | None = None,
        cleanup_every_operations: int = CACHE_CLEANUP_EVERY_OPERATIONS,
    ):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._table_name = table_name
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._time_fn = time_fn or time.time
        self._cleanup_every_operations = max(1, int(cleanup_every_operations))
        self._lock = Lock()
        self._closed = False
        self._operations_since_cleanup = 0
        self._pragma_state: dict[str, object] = {}
        self._conn = self._connect()
        self._apply_pragmas()
        self._conn.execute(create_table_sql)
        self._ensure_created_at_column()
        self._conn.commit()

    @property
    def closed(self) -> bool:
        return bool(self._closed)

    @property
    def pragma_state(self) -> dict[str, object]:
        return dict(self._pragma_state)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._conn.commit()
            finally:
                self._conn.close()
                self._closed = True

    def purge_expired(self) -> int:
        with self._lock:
            self._require_open_locked()
            return self._purge_expired_locked(force=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def _apply_pragmas(self) -> None:
        journal_row = self._conn.execute(
            f"PRAGMA journal_mode={SQLITE_JOURNAL_MODE}"
        ).fetchone()
        self._conn.execute(f"PRAGMA synchronous={SQLITE_SYNCHRONOUS_MODE}")
        synchronous_row = self._conn.execute("PRAGMA synchronous").fetchone()
        self._conn.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        busy_timeout_row = self._conn.execute("PRAGMA busy_timeout").fetchone()
        self._conn.execute(
            f"PRAGMA foreign_keys={1 if SQLITE_FOREIGN_KEYS_ENABLED else 0}"
        )
        foreign_keys_row = self._conn.execute("PRAGMA foreign_keys").fetchone()
        self._pragma_state = {
            "journal_mode": str(journal_row[0]).casefold() if journal_row else "",
            "synchronous": synchronous_row[0] if synchronous_row else "",
            "busy_timeout_ms": int(busy_timeout_row[0]) if busy_timeout_row else 0,
            "foreign_keys": bool(foreign_keys_row[0]) if foreign_keys_row else False,
        }
        logger.debug(
            "SQLite cache %s pragmas applied: %s",
            self._path,
            self._pragma_state,
        )

    def _ensure_created_at_column(self) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({self._table_name})").fetchall()
        columns = {str(row["name"]).casefold() for row in rows}
        if "created_at" not in columns:
            self._conn.execute(
                f"ALTER TABLE {self._table_name} ADD COLUMN created_at REAL"
            )

    def _delete_row_locked(self, where_sql: str, params: tuple[object, ...]) -> None:
        self._conn.execute(
            f"DELETE FROM {self._table_name} WHERE {where_sql}",
            params,
        )
        self._conn.commit()

    def _is_expired(self, created_at: object) -> bool:
        if created_at in (None, ""):
            return True
        try:
            created_ts = float(created_at)
        except (TypeError, ValueError):
            return True
        return created_ts < self._expiry_cutoff()

    def _expiry_cutoff(self) -> float:
        return float(self._time_fn()) - float(self._ttl_seconds)

    def _mark_write_locked(self) -> None:
        self._operations_since_cleanup += 1
        if self._operations_since_cleanup >= self._cleanup_every_operations:
            self._purge_expired_locked(force=True)

    def _purge_expired_locked(self, *, force: bool) -> int:
        if not force and self._operations_since_cleanup < self._cleanup_every_operations:
            return 0
        cursor = self._conn.execute(
            f"DELETE FROM {self._table_name} WHERE created_at IS NULL OR created_at < ?",
            (self._expiry_cutoff(),),
        )
        self._conn.commit()
        self._operations_since_cleanup = 0
        return max(0, int(cursor.rowcount))

    def _require_open_locked(self) -> None:
        if self._closed:
            raise RuntimeError(f"{self.__class__.__name__} is closed")

    def _timestamp(self) -> float:
        return float(self._time_fn())


class SearchResultCache(SQLiteCacheBase):
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = SEARCH_RESULT_CACHE_TTL_SECONDS,
        time_fn: Callable[[], float] | None = None,
        cleanup_every_operations: int = CACHE_CLEANUP_EVERY_OPERATIONS,
    ):
        super().__init__(
            path,
            table_name="search_cache",
            create_table_sql=(
                "CREATE TABLE IF NOT EXISTS search_cache ("
                "provider_id TEXT NOT NULL, "
                "query TEXT NOT NULL, "
                "limit_value INTEGER NOT NULL, "
                "payload TEXT NOT NULL, "
                "created_at REAL, "
                "PRIMARY KEY(provider_id, query, limit_value)"
                ")"
            ),
            ttl_seconds=ttl_seconds,
            time_fn=time_fn,
            cleanup_every_operations=cleanup_every_operations,
        )

    def get(
        self, provider_id: str, query: str, limit_value: int
    ) -> list[SearchCandidate] | None:
        with self._lock:
            self._require_open_locked()
            row = self._conn.execute(
                """
                SELECT payload, created_at
                FROM search_cache
                WHERE provider_id = ? AND query = ? AND limit_value = ?
                """,
                (provider_id, query, int(limit_value)),
            ).fetchone()
            if row is None:
                return None
            if self._is_expired(row["created_at"]):
                self._delete_row_locked(
                    "provider_id = ? AND query = ? AND limit_value = ?",
                    (provider_id, query, int(limit_value)),
                )
                return None

        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, list):
            return None
        return [_deserialize_candidate(item) for item in payload if isinstance(item, dict)]

    def set(
        self,
        provider_id: str,
        query: str,
        limit_value: int,
        candidates: list[SearchCandidate],
    ) -> None:
        payload = json.dumps(
            [_serialize_candidate(item) for item in candidates],
            ensure_ascii=False,
        )
        with self._lock:
            self._require_open_locked()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO search_cache(
                    provider_id,
                    query,
                    limit_value,
                    payload,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (provider_id, query, int(limit_value), payload, self._timestamp()),
            )
            self._conn.commit()
            self._mark_write_locked()


class MetadataCache(SQLiteCacheBase):
    def __init__(
        self,
        path: str | Path,
        *,
        ttl_seconds: int = METADATA_CACHE_TTL_SECONDS,
        time_fn: Callable[[], float] | None = None,
        cleanup_every_operations: int = CACHE_CLEANUP_EVERY_OPERATIONS,
    ):
        super().__init__(
            path,
            table_name="metadata_cache",
            create_table_sql=(
                "CREATE TABLE IF NOT EXISTS metadata_cache ("
                "cache_key TEXT PRIMARY KEY, "
                "payload TEXT NOT NULL, "
                "created_at REAL"
                ")"
            ),
            ttl_seconds=ttl_seconds,
            time_fn=time_fn,
            cleanup_every_operations=cleanup_every_operations,
        )

    def get(self, cache_key: str) -> dict[str, object] | None:
        with self._lock:
            self._require_open_locked()
            row = self._conn.execute(
                """
                SELECT payload, created_at
                FROM metadata_cache
                WHERE cache_key = ?
                """,
                (cache_key,),
            ).fetchone()
            if row is None:
                return None
            if self._is_expired(row["created_at"]):
                self._delete_row_locked("cache_key = ?", (cache_key,))
                return None

        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            return None
        return payload

    def set(self, cache_key: str, payload: dict[str, object]) -> None:
        with self._lock:
            self._require_open_locked()
            self._conn.execute(
                """
                INSERT OR REPLACE INTO metadata_cache(cache_key, payload, created_at)
                VALUES (?, ?, ?)
                """,
                (cache_key, json.dumps(payload, ensure_ascii=False), self._timestamp()),
            )
            self._conn.commit()
            self._mark_write_locked()


__all__ = [
    "CACHE_CLEANUP_EVERY_OPERATIONS",
    "METADATA_CACHE_TTL_SECONDS",
    "MetadataCache",
    "SEARCH_RESULT_CACHE_TTL_SECONDS",
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLITE_JOURNAL_MODE",
    "SQLITE_SYNCHRONOUS_MODE",
    "SearchResultCache",
]
