from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from domain.enums import ProviderCapability, SessionHealth
from domain.models import AssetCandidate, ParagraphUnit, ProviderResult
from providers.base import ProviderDescriptor
from services.errors import DownloadError, ProviderError, SessionError

from .downloads import (
    PlaywrightDownloadDriver,
    StoryblocksDownloadManager,
    StoryblocksDownloadRequest,
)
from .session import BrowserSessionManager
from .storyblocks import (
    StoryblocksDomContractChecker,
    capture_storyblocks_page_snapshot,
)

STORYBLOCKS_FATAL_ERROR_CODES = {
    "storyblocks_profile_missing",
    "storyblocks_session_not_ready",
    "storyblocks_login_required",
    "storyblocks_challenge_detected",
    "storyblocks_session_expired",
    "storyblocks_blocked",
    "storyblocks_page_unavailable",
    "storyblocks_detail_url_missing",
}


def is_storyblocks_fatal_error_code(code: str) -> bool:
    return str(code or "").strip().casefold() in STORYBLOCKS_FATAL_ERROR_CODES


@dataclass(frozen=True, slots=True)
class StoryblocksOperationPolicy:
    search_timeout_seconds: float = 20.0
    download_retries: int = 2
    download_timeout_seconds: float = 120.0


@dataclass(slots=True)
class StoryblocksCandidateSearchBackend:
    provider_id: str
    capability: ProviderCapability
    descriptor: ProviderDescriptor
    session_manager: BrowserSessionManager
    search_adapter: Any
    dom_checker: StoryblocksDomContractChecker
    search_filters: list[Any] = field(default_factory=list)
    operation_policy: StoryblocksOperationPolicy = field(
        default_factory=StoryblocksOperationPolicy
    )

    def _raise_for_session_state(
        self,
        state,
        *,
        paragraph: ParagraphUnit,
        query: str,
        rescue_url: str,
    ) -> None:
        if state.health == SessionHealth.LOGIN_REQUIRED:
            self.session_manager.require_manual_login(
                paragraph_no=paragraph.paragraph_no,
                query=query,
                rescue_url=rescue_url,
            )
            raise SessionError(
                code="storyblocks_login_required",
                message="Storyblocks login is required before searching providers.",
                details={"paragraph_no": paragraph.paragraph_no, "query": query},
            )
        if state.health == SessionHealth.CHALLENGE:
            self.session_manager.register_challenge(
                paragraph_no=paragraph.paragraph_no,
                query=query,
                rescue_url=rescue_url,
            )
            raise SessionError(
                code="storyblocks_challenge_detected",
                message="Storyblocks challenge detected. Finish the manual verification in the persistent browser.",
                details={"paragraph_no": paragraph.paragraph_no, "query": query},
            )
        if state.health == SessionHealth.EXPIRED:
            self.session_manager.require_manual_login(
                paragraph_no=paragraph.paragraph_no,
                query=query,
                rescue_url=rescue_url,
            )
            raise SessionError(
                code="storyblocks_session_expired",
                message="The saved Storyblocks session expired. Log in again and retry the paragraph.",
                details={"paragraph_no": paragraph.paragraph_no, "query": query},
            )
        if state.health == SessionHealth.BLOCKED:
            raise SessionError(
                code="storyblocks_blocked",
                message="Storyblocks reported a blocked or denied state. Pause automation and inspect the browser.",
                details={"paragraph_no": paragraph.paragraph_no, "query": query},
            )
        if state.health != SessionHealth.READY:
            raise SessionError(
                code="storyblocks_session_not_ready",
                message="Storyblocks session is not ready for automated provider search.",
                details={
                    "paragraph_no": paragraph.paragraph_no,
                    "query": query,
                    "health": state.health.value,
                },
            )

    def search(
        self,
        paragraph: ParagraphUnit,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> ProviderResult:
        rescue_url = self.search_adapter.build_direct_search_url(query)
        try:
            if self.session_manager.has_manual_ready_override():
                state = self.session_manager.current_state()
            else:
                state = self.session_manager.check_authorization()
        except KeyError as exc:
            raise SessionError(
                code="storyblocks_profile_missing",
                message="Create or select a Storyblocks browser profile before running a Storyblocks mode.",
                details={"paragraph_no": paragraph.paragraph_no, "query": query},
            ) from exc
        if not state.manual_ready_override:
            self._raise_for_session_state(
                state,
                paragraph=paragraph,
                query=query,
                rescue_url=rescue_url,
            )

        session = self.session_manager.open_browser()
        page = session.handle.page
        if not hasattr(page, "content"):
            raise ProviderError(
                code="storyblocks_page_unavailable",
                message="Persistent browser page does not expose HTML content for Storyblocks parsing.",
            )

        policy = self.operation_policy
        effective_timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(policy.search_timeout_seconds)
        )
        self._apply_page_timeouts(page, timeout_seconds=effective_timeout_seconds)
        self.search_adapter.open_search(page, query, filters=list(self.search_filters))
        snapshot = capture_storyblocks_page_snapshot(
            page,
            wait_timeout_ms=self._snapshot_wait_timeout_ms(effective_timeout_seconds),
        )
        html = snapshot.html
        state = self.session_manager.check_authorization(
            html=html,
            current_url=snapshot.current_url,
        )
        self._raise_for_session_state(
            state,
            paragraph=paragraph,
            query=query,
            rescue_url=rescue_url,
        )
        contract = self.dom_checker.validate_markup(html)
        candidates = self.search_adapter.parse_result_cards(html, query)[
            : max(1, limit)
        ]
        for candidate in candidates:
            candidate.provider_name = self.provider_id
        diagnostics = {
            "search_url": snapshot.current_url or getattr(page, "url", rescue_url),
            "dom_contract_valid": contract.valid,
            "dom_missing": list(contract.missing),
        }
        errors = (
            []
            if contract.valid
            else [f"Storyblocks DOM contract drifted: {', '.join(contract.missing)}"]
        )
        if snapshot.warning:
            diagnostics["page_snapshot_warning"] = snapshot.warning
            if not candidates:
                errors.append(snapshot.warning)
        return ProviderResult(
            provider_name=self.provider_id,
            capability=self.capability,
            query=query,
            candidates=candidates,
            errors=errors,
            diagnostics=diagnostics,
        )

    def _apply_page_timeouts(self, page: Any, *, timeout_seconds: float) -> None:
        if timeout_seconds <= 0:
            return
        timeout_ms = max(1, int(timeout_seconds * 1000.0))
        setter = getattr(page, "set_default_timeout", None)
        if callable(setter):
            setter(timeout_ms)
        navigation_setter = getattr(page, "set_default_navigation_timeout", None)
        if callable(navigation_setter):
            navigation_setter(timeout_ms)

    def _snapshot_wait_timeout_ms(self, timeout_seconds: float) -> int:
        if timeout_seconds <= 0:
            return 2000
        return max(250, min(2000, int(timeout_seconds * 1000.0)))

    def download_asset(
        self,
        asset: AssetCandidate,
        *,
        destination_dir: Path,
        filename: str,
        operation_policy: StoryblocksOperationPolicy | None = None,
    ) -> AssetCandidate:
        detail_url = str(
            asset.source_url or asset.metadata.get("detail_url", "")
        ).strip()
        if not detail_url:
            raise DownloadError(
                code="storyblocks_detail_url_missing",
                message=f"Storyblocks asset '{asset.asset_id}' has no detail URL for download.",
                details={
                    "asset_id": asset.asset_id,
                    "provider_id": self.provider_id,
                },
                fatal=True,
            )
        session = self.session_manager.open_browser()
        policy = operation_policy or self.operation_policy
        driver = PlaywrightDownloadDriver(
            session.handle.page,
            download_button_selectors=self.search_adapter.selectors.detail_download_button,
            timeout_ms=max(1, int(policy.download_timeout_seconds * 1000.0)),
        )
        manager = StoryblocksDownloadManager(
            driver, max_retries=policy.download_retries
        )
        record = manager.download_one(
            StoryblocksDownloadRequest(
                asset_id=asset.asset_id,
                detail_url=detail_url,
                destination_dir=destination_dir,
                filename=filename,
                metadata={"provider_id": self.provider_id},
            )
        )
        if (
            record.status not in {"completed", "deduplicated"}
            or record.local_path is None
        ):
            raise DownloadError(
                code="storyblocks_download_failed",
                message=record.error
                or f"Storyblocks download failed for asset '{asset.asset_id}'.",
                details={
                    "asset_id": asset.asset_id,
                    "provider_id": self.provider_id,
                },
            )
        downloaded = AssetCandidate.from_dict(asset.to_dict())
        downloaded.local_path = record.local_path
        downloaded.metadata["download_status"] = record.status
        downloaded.metadata["download_attempts"] = record.attempts
        downloaded.metadata["detail_url"] = detail_url
        return downloaded
