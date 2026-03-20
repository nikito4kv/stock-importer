from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin

from domain.enums import AssetKind, SessionHealth
from domain.models import AssetCandidate

from .session import AuthorizationSnapshot


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def slugify_storyblocks_query(query: str) -> str:
    normalized = _normalize_text(query).casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized).strip("-")
    return slug or quote(_normalize_text(query))


@dataclass(frozen=True, slots=True)
class StoryblocksSearchFilter:
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class StoryblocksSelectorCatalog:
    search_input: tuple[str, ...] = (
        'input[aria-label="Input Search:"]',
        'input[placeholder*="Search"]',
        '[data-testid="search-input"]',
    )
    submit_search: tuple[str, ...] = (
        'button[aria-label="Submit Search"]',
        'button:has-text("Search")',
        '[data-testid="search-submit"]',
    )
    gallery_item_patterns: tuple[str, ...] = (
        r"justified-gallery-item-[\w-]+",
        r"gallery-item-[\w-]+",
    )
    detail_download_button: tuple[str, ...] = (
        '.memberDownloadCta button:has-text("Download")',
        '.memberDownloadCta-cta button:has-text("Download")',
        '.memberDownloadCta .PrimaryButton:has-text("Download")',
        'button:has-text("Download")',
        'a:has-text("Download")',
        '[data-testid="detail-download"]',
    )


@dataclass(slots=True)
class StoryblocksDomContractResult:
    valid: bool
    missing: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StoryblocksPageSnapshot:
    html: str
    current_url: str
    warning: str = ""


class StoryblocksDomContractChecker:
    def __init__(self, selectors: StoryblocksSelectorCatalog | None = None):
        self._selectors = selectors or StoryblocksSelectorCatalog()

    def validate_markup(self, html: str) -> StoryblocksDomContractResult:
        normalized = html or ""
        missing: list[str] = []

        if "Input Search:" not in normalized and "search-input" not in normalized:
            missing.append("search_input")
        if "Submit Search" not in normalized and "search-submit" not in normalized:
            missing.append("submit_search")
        has_gallery_item = any(
            re.search(pattern, normalized)
            for pattern in self._selectors.gallery_item_patterns
        ) or (
            "stock-item-group-wrapper" in normalized
            and "data-stock-id=" in normalized
            and "/images/stock/" in normalized
        )
        if not has_gallery_item:
            missing.append("gallery_item")
        if "Download" not in normalized and "detail-download" not in normalized:
            missing.append("detail_download_button")

        return StoryblocksDomContractResult(valid=not missing, missing=missing)


class StoryblocksSessionProbe:
    def inspect_document(
        self, html: str, current_url: str = ""
    ) -> AuthorizationSnapshot:
        content = (html or "").casefold()
        current = (current_url or "").casefold()
        account_match = re.search(
            r'data-account-email=["\']([^"\']+)["\']', html or "", flags=re.IGNORECASE
        )
        account = account_match.group(1) if account_match else None

        strong_ready_markers = self._strong_ready_markers(content, current, account)
        weak_ready_markers = self._weak_ready_markers(content, current)
        challenge_markers = self._challenge_markers(content, current)
        blocked_markers = self._blocked_markers(content, current)
        expired_markers = self._expired_markers(content, current)
        login_markers = self._login_markers(content, current)
        non_storyblocks_markers = self._non_storyblocks_markers(current)

        if non_storyblocks_markers:
            return AuthorizationSnapshot(
                SessionHealth.UNKNOWN,
                account=account,
                current_url=current_url,
                message=f"Attached to a non-Storyblocks browser page ({non_storyblocks_markers[0]})",
            )

        if blocked_markers:
            return AuthorizationSnapshot(
                SessionHealth.BLOCKED,
                account=account,
                current_url=current_url,
                message=f"Blocked or denied ({blocked_markers[0]})",
            )
        if expired_markers:
            return AuthorizationSnapshot(
                SessionHealth.EXPIRED,
                account=account,
                current_url=current_url,
                message=f"Session expired ({expired_markers[0]})",
            )
        if login_markers and not strong_ready_markers:
            return AuthorizationSnapshot(
                SessionHealth.LOGIN_REQUIRED,
                account=account,
                current_url=current_url,
                message=f"Login required ({login_markers[0]})",
            )
        if challenge_markers and not strong_ready_markers:
            return AuthorizationSnapshot(
                SessionHealth.CHALLENGE,
                account=account,
                current_url=current_url,
                message=f"Challenge detected ({challenge_markers[0]})",
            )
        if strong_ready_markers:
            return AuthorizationSnapshot(
                SessionHealth.READY,
                account=account,
                current_url=current_url,
                message=f"Session ready ({strong_ready_markers[0]})",
            )
        if weak_ready_markers and not challenge_markers:
            return AuthorizationSnapshot(
                SessionHealth.READY,
                account=account,
                current_url=current_url,
                message=f"Session ready ({weak_ready_markers[0]})",
            )
        return AuthorizationSnapshot(
            SessionHealth.UNKNOWN,
            account=account,
            current_url=current_url,
            message="Session state unknown",
        )

    def _strong_ready_markers(
        self, content: str, current: str, account: str | None
    ) -> list[str]:
        markers: list[str] = []
        if account:
            markers.append("data-account-email")
        for token in (
            "/logout",
            ">logout<",
            "logout",
            "my account",
            "account-menu",
            "justified-gallery-item-",
            "download credits",
            "member library",
        ):
            if token in content or token in current:
                markers.append(token)
        return list(dict.fromkeys(markers))

    def _weak_ready_markers(self, content: str, current: str) -> list[str]:
        markers: list[str] = []
        for token in ("all-video/search", "/images/search/"):
            if token in content or token in current:
                markers.append(token)
        if "download" in content and ("/video/" in current or "/images/" in current):
            markers.append("download")
        if "storyblocks.com" in current and any(
            token in content
            for token in ("justified-gallery-item-", "search-input", "submit search")
        ):
            markers.append("storyblocks-app-shell")
        return markers

    def _non_storyblocks_markers(self, current: str) -> list[str]:
        markers: list[str] = []
        for token in ("chrome://", "edge://", "about:blank"):
            if current.startswith(token):
                markers.append(token)
        return markers

    def _challenge_markers(self, content: str, current: str) -> list[str]:
        markers: list[str] = []
        for token in (
            "verify you are human",
            "captcha",
            "g-recaptcha",
            "recaptcha",
            "hcaptcha",
            "cf-challenge",
            "challenge-platform",
            "complete the captcha",
        ):
            if token in content or token in current:
                markers.append(token)
        return markers

    def _blocked_markers(self, content: str, current: str) -> list[str]:
        markers: list[str] = []
        for token in (
            "access denied",
            "temporarily blocked",
            "request blocked",
            "blocked",
        ):
            if token in content or token in current:
                markers.append(token)
        return markers

    def _expired_markers(self, content: str, current: str) -> list[str]:
        markers: list[str] = []
        for token in (
            "session expired",
            "sign in again",
            "expired session",
            "your session has expired",
        ):
            if token in content or token in current:
                markers.append(token)
        return markers

    def _login_markers(self, content: str, current: str) -> list[str]:
        markers: list[str] = []
        for token in ("login to continue", "sign in to continue", "/login"):
            if token in content or token in current:
                markers.append(token)
        generic_text_present = any(token in content for token in ("log in", "sign in"))
        if generic_text_present and not any(
            token in content
            for token in (
                "download",
                "my account",
                "justified-gallery-item-",
                "data-account-email",
            )
        ):
            markers.append("generic-sign-in")
        return markers

    def inspect_page(self, page: Any) -> AuthorizationSnapshot:
        snapshot = capture_storyblocks_page_snapshot(page)
        result = self.inspect_document(snapshot.html, current_url=snapshot.current_url)
        if snapshot.warning and result.health == SessionHealth.UNKNOWN:
            result.message = snapshot.warning
        return result


@dataclass(slots=True)
class _ParsedCard:
    data_testid: str | None = None
    stock_id: str | None = None
    detail_url: str | None = None
    thumbnail_url: str | None = None
    title: str = ""


def _looks_like_storyblocks_image_card(tag: str, attr_map: dict[str, str]) -> bool:
    stock_id = attr_map.get("data-stock-id", "").strip()
    if not stock_id or tag not in {"section", "article", "div"}:
        return False
    class_tokens = set(attr_map.get("class", "").split())
    return "stock-item" in class_tokens or "stock-item-v2" in class_tokens


_VOID_HTML_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _StoryblocksGalleryParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.cards: list[_ParsedCard] = []
        self._current: _ParsedCard | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        testid = attr_map.get("data-testid", "")
        if testid.startswith("justified-gallery-item-") or testid.startswith(
            "gallery-item-"
        ):
            self._current = _ParsedCard(data_testid=testid)
            self._depth = 1
            return
        if _looks_like_storyblocks_image_card(tag, attr_map):
            self._current = _ParsedCard(
                stock_id=attr_map.get("data-stock-id", "").strip()
            )
            self._depth = 1
            return

        if self._current is None:
            return

        if tag not in _VOID_HTML_TAGS:
            self._depth += 1
        href = attr_map.get("href", "")
        if href and self._current.detail_url is None:
            self._current.detail_url = href
        src = attr_map.get("src", "") or attr_map.get("data-src", "")
        if src and self._current.thumbnail_url is None:
            self._current.thumbnail_url = src
        for key in ("aria-label", "title", "alt", "data-title"):
            label = _normalize_text(attr_map.get(key, ""))
            if label:
                self._current.title = label
                break

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        text = _normalize_text(data)
        if text and not self._current.title:
            self._current.title = text

    def handle_endtag(self, tag: str) -> None:
        if self._current is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            self.cards.append(self._current)
            self._current = None
            self._depth = 0


def _extract_asset_id(
    testid: str | None, detail_url: str | None, stock_id: str | None = None
) -> str | None:
    if testid:
        match = re.search(r"(?:justified-gallery-item-|gallery-item-)([\w-]+)$", testid)
        if match:
            return match.group(1)
    if stock_id:
        return stock_id
    if detail_url:
        match = re.search(r"/(?:video|images?)/(?:stock/)?([\w-]+)", detail_url)
        if match:
            return match.group(1)
        stripped = detail_url.rstrip("/").rsplit("/", 1)[-1]
        if stripped:
            return stripped
    return None


def _make_candidate(
    *,
    kind: AssetKind,
    base_url: str,
    query: str,
    card: _ParsedCard,
) -> AssetCandidate | None:
    asset_id = _extract_asset_id(card.data_testid, card.detail_url, card.stock_id)
    if not asset_id:
        return None
    detail_url = urljoin(base_url, card.detail_url or "") if card.detail_url else None
    thumbnail_url = (
        urljoin(base_url, card.thumbnail_url or "") if card.thumbnail_url else None
    )
    return AssetCandidate(
        asset_id=asset_id,
        provider_name="storyblocks",
        kind=kind,
        source_url=detail_url,
        license_name="storyblocks-license",
        metadata={
            "title": card.title or asset_id.replace("-", " "),
            "detail_url": detail_url,
            "thumbnail_url": thumbnail_url,
            "search_query": query,
        },
    )


class StoryblocksSearchAdapterBase:
    search_path_prefix = "/"
    asset_kind = AssetKind.VIDEO

    def __init__(
        self,
        base_url: str = "https://www.storyblocks.com",
        selectors: StoryblocksSelectorCatalog | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.selectors = selectors or StoryblocksSelectorCatalog()

    def build_direct_search_url(self, query: str) -> str:
        return f"{self.base_url}{self.search_path_prefix}/{slugify_storyblocks_query(query)}"

    def build_homepage_rescue_url(self, query: str) -> str:
        return f"{self.base_url}/?search={quote(_normalize_text(query))}"

    def open_search(
        self,
        page: Any,
        query: str,
        *,
        filters: list[StoryblocksSearchFilter] | None = None,
        use_homepage: bool = False,
    ) -> str:
        url = (
            self.build_homepage_rescue_url(query)
            if use_homepage
            else self.build_direct_search_url(query)
        )
        if hasattr(page, "goto"):
            page.goto(url)
        self.apply_filters(page, filters or [])
        return url

    def apply_filters(self, page: Any, filters: list[StoryblocksSearchFilter]) -> None:
        for item in filters:
            if hasattr(page, "apply_storyblocks_filter"):
                page.apply_storyblocks_filter(item.name, item.value)
            elif hasattr(page, "applied_filters") and isinstance(
                page.applied_filters, list
            ):
                page.applied_filters.append((item.name, item.value))

    def parse_result_cards(self, html: str, query: str) -> list[AssetCandidate]:
        parser = _StoryblocksGalleryParser()
        parser.feed(html or "")
        candidates: list[AssetCandidate] = []
        for card in parser.cards:
            candidate = _make_candidate(
                kind=self.asset_kind, base_url=self.base_url, query=query, card=card
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates


class StoryblocksVideoSearchAdapter(StoryblocksSearchAdapterBase):
    search_path_prefix = "/all-video/search"
    asset_kind = AssetKind.VIDEO


class StoryblocksImageSearchAdapter(StoryblocksSearchAdapterBase):
    search_path_prefix = "/images/search"
    asset_kind = AssetKind.IMAGE


def first_available_selector(page: Any, candidates: tuple[str, ...]) -> str | None:
    if not hasattr(page, "locator"):
        return None
    for selector in candidates:
        locator = page.locator(selector)
        try:
            count = locator.count() if hasattr(locator, "count") else 0
        except Exception:
            count = 0
        if count:
            return selector
    return None


def capture_storyblocks_page_snapshot(
    page: Any,
    *,
    attempts: int = 4,
    wait_timeout_ms: int = 2000,
    retry_delay_seconds: float = 0.35,
) -> StoryblocksPageSnapshot:
    current_url = getattr(page, "url", "")
    if not hasattr(page, "content"):
        return StoryblocksPageSnapshot(html="", current_url=current_url)

    last_warning = ""
    for _attempt in range(max(1, attempts)):
        current_url = getattr(page, "url", current_url)
        if hasattr(page, "wait_for_load_state"):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=wait_timeout_ms)
            except Exception:
                pass
        try:
            return StoryblocksPageSnapshot(
                html=page.content(),
                current_url=getattr(page, "url", current_url),
                warning=last_warning,
            )
        except Exception as exc:
            if not _is_transient_storyblocks_navigation_error(exc):
                raise
            last_warning = "Storyblocks page is still navigating. Wait for the page to settle and check the session again."
            if hasattr(page, "wait_for_timeout"):
                try:
                    page.wait_for_timeout(int(retry_delay_seconds * 1000))
                    continue
                except Exception:
                    pass
            time.sleep(max(0.0, retry_delay_seconds))
    return StoryblocksPageSnapshot(
        html="", current_url=getattr(page, "url", current_url), warning=last_warning
    )


def _is_transient_storyblocks_navigation_error(exc: Exception) -> bool:
    message = str(exc).casefold()
    return any(
        token in message
        for token in (
            "page.content",
            "page is navigating",
            "changing the content",
            "execution context was destroyed",
            "navigation",
        )
    )
