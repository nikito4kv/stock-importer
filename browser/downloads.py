from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from services.errors import DownloadError


def _now() -> datetime:
    return datetime.now(timezone.utc)


PARTIAL_DOWNLOAD_SUFFIXES = {".crdownload", ".part", ".tmp"}


@dataclass(frozen=True, slots=True)
class StoryblocksDownloadRequest:
    asset_id: str
    detail_url: str
    destination_dir: Path
    filename: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StoryblocksDownloadRecord:
    request: StoryblocksDownloadRequest
    status: str
    attempts: int = 0
    local_path: Path | None = None
    error: str | None = None
    completed_at: datetime | None = None


class StoryblocksDownloadDriver(Protocol):
    def download(self, request: StoryblocksDownloadRequest) -> bytes | Path: ...


class PlaywrightDownloadDriver:
    def __init__(
        self,
        page: Any,
        download_button_selectors: tuple[str, ...],
        *,
        timeout_ms: int | None = None,
    ):
        self._page = page
        self._selectors = download_button_selectors
        self._timeout_ms = max(1, int(timeout_ms)) if timeout_ms is not None else None

    def download(self, request: StoryblocksDownloadRequest) -> bytes | Path:
        if not hasattr(self._page, "expect_download") or not hasattr(
            self._page, "goto"
        ):
            raise DownloadError(
                code="playwright_download_unavailable",
                message="Playwright page does not support expect_download().",
            )

        self._page.goto(request.detail_url)
        try:
            if self._timeout_ms is not None:
                download_context = self._page.expect_download(timeout=self._timeout_ms)
            else:
                download_context = self._page.expect_download()
        except TypeError:
            download_context = self._page.expect_download()
        with download_context as download_info:
            self._click_download()
        download = download_info.value
        request.destination_dir.mkdir(parents=True, exist_ok=True)
        destination = request.destination_dir / request.filename
        save_as = getattr(download, "save_as", None)
        if callable(save_as):
            save_as(str(destination))
            if destination.exists():
                return destination
        try:
            temp_path = download.path()
        except Exception:
            temp_path = None
        if temp_path is None:
            raise DownloadError(
                code="download_temp_path_missing",
                message=f"Playwright did not expose a retrievable temporary path for asset '{request.asset_id}'.",
            )
        return Path(temp_path)

    def _click_download(self) -> None:
        for selector in self._selectors:
            try:
                locator = self._page.locator(selector)
                count = locator.count() if hasattr(locator, "count") else 0
            except Exception:
                count = 0
                locator = None
            if count:
                target = locator.first if hasattr(locator, "first") else locator
                target.click()
                return
        raise DownloadError(
            code="download_button_missing",
            message="Unable to find a Storyblocks download button on the detail page.",
        )


class StoryblocksDownloadManager:
    def __init__(self, driver: StoryblocksDownloadDriver, *, max_retries: int = 2):
        self._driver = driver
        self._max_retries = max(0, int(max_retries))
        self._queue: list[StoryblocksDownloadRequest] = []
        self._downloaded_by_asset: dict[str, Path] = {}

    def enqueue(self, request: StoryblocksDownloadRequest) -> None:
        self._queue.append(request)

    def pending_count(self) -> int:
        return len(self._queue)

    def run_queue(self) -> list[StoryblocksDownloadRecord]:
        results: list[StoryblocksDownloadRecord] = []
        while self._queue:
            request = self._queue.pop(0)
            results.append(self.download_one(request))
        return results

    def download_one(
        self, request: StoryblocksDownloadRequest
    ) -> StoryblocksDownloadRecord:
        destination = request.destination_dir / request.filename
        request.destination_dir.mkdir(parents=True, exist_ok=True)

        if request.asset_id in self._downloaded_by_asset:
            return StoryblocksDownloadRecord(
                request=request,
                status="deduplicated",
                local_path=self._downloaded_by_asset[request.asset_id],
                completed_at=_now(),
            )
        if self._is_complete_file(destination):
            self._downloaded_by_asset[request.asset_id] = destination
            return StoryblocksDownloadRecord(
                request=request,
                status="completed",
                attempts=0,
                local_path=destination,
                completed_at=_now(),
            )

        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 2):
            try:
                payload = self._driver.download(request)
                local_path = self._persist_payload(payload, destination)
                if not self._is_complete_file(local_path):
                    raise DownloadError(
                        code="download_incomplete",
                        message=f"Download for asset '{request.asset_id}' did not complete cleanly.",
                    )
                self._downloaded_by_asset[request.asset_id] = local_path
                return StoryblocksDownloadRecord(
                    request=request,
                    status="completed",
                    attempts=attempt,
                    local_path=local_path,
                    completed_at=_now(),
                )
            except Exception as exc:
                last_error = exc

        return StoryblocksDownloadRecord(
            request=request,
            status="failed",
            attempts=self._max_retries + 1,
            local_path=None,
            error=str(last_error),
        )

    def _persist_payload(self, payload: bytes | Path, destination: Path) -> Path:
        if isinstance(payload, Path):
            if payload.resolve() != destination.resolve():
                shutil.copy2(payload, destination)
            else:
                destination = payload
            return destination

        destination.write_bytes(payload)
        return destination

    def _is_complete_file(self, path: Path) -> bool:
        return (
            path.exists()
            and path.is_file()
            and path.suffix.lower() not in PARTIAL_DOWNLOAD_SUFFIXES
            and path.stat().st_size > 0
        )
