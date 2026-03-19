from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from app.bootstrap import bootstrap_application
from browser import (
    AuthorizationSnapshot,
    BrowserActionPacer,
    BrowserChannelResolver,
    ChromiumProfileImportService,
    BrowserSessionManager,
    NativeBrowserSession,
    PlaywrightDownloadDriver,
    PersistentBrowserHandle,
    SlowModePolicy,
    StoryblocksDomContractChecker,
    StoryblocksDownloadManager,
    StoryblocksDownloadRequest,
    StoryblocksCandidateSearchBackend,
    StoryblocksImageSearchAdapter,
    StoryblocksSearchFilter,
    StoryblocksSelectorCatalog,
    StoryblocksSessionProbe,
    StoryblocksVideoSearchAdapter,
)
from domain.enums import SessionHealth
from domain.models import ParagraphUnit, QueryBundle
from services.errors import SessionError


class FakePage:
    def __init__(self, html: str = "", url: str = "about:blank"):
        self._html = html
        self.url = url
        self.applied_filters: list[tuple[str, str]] = []
        self.default_timeout = None
        self.navigation_timeout = None

    def goto(self, url: str) -> None:
        self.url = url

    def content(self) -> str:
        return self._html

    def set_default_timeout(self, timeout: int) -> None:
        self.default_timeout = timeout

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self.navigation_timeout = timeout

    def wait_for_load_state(self, _state: str, timeout: int | None = None) -> None:
        self.navigation_timeout = timeout

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.default_timeout = timeout_ms

    def apply_storyblocks_filter(self, name: str, value: str) -> None:
        self.applied_filters.append((name, value))


class _FakeDownloadArtifact:
    def __init__(self, payload: bytes = b"video-bytes"):
        self._payload = payload

    def save_as(self, target_path: str) -> None:
        Path(target_path).write_bytes(self._payload)

    def path(self):
        raise RuntimeError("download path unavailable over remote connection")


class _DownloadInfoContext:
    def __init__(self, artifact: _FakeDownloadArtifact):
        self.value = artifact

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class DownloadCapableFakePage(FakePage):
    def __init__(
        self, html: str = "", url: str = "about:blank", payload: bytes = b"video-bytes"
    ):
        super().__init__(html=html, url=url)
        self.payload = payload

    def expect_download(self):
        return _DownloadInfoContext(_FakeDownloadArtifact(self.payload))

    def locator(self, _selector: str):
        class _Locator:
            def count(self_nonlocal):
                return 1

            @property
            def first(self_nonlocal):
                return self_nonlocal

            def click(self_nonlocal):
                return None

        return _Locator()


class TimeoutAwareDownloadPage(DownloadCapableFakePage):
    def __init__(self, html: str = "", url: str = "about:blank"):
        super().__init__(html=html, url=url)
        self.download_timeout = None

    def expect_download(self, timeout=None):
        self.download_timeout = timeout
        return super().expect_download()


class SelectorAwareDownloadPage(DownloadCapableFakePage):
    def __init__(
        self,
        available_selectors: set[str],
        html: str = "",
        url: str = "about:blank",
    ):
        super().__init__(html=html, url=url)
        self.available_selectors = set(available_selectors)
        self.clicked_selectors: list[str] = []

    def locator(self, selector: str):
        page = self
        is_available = selector in self.available_selectors

        class _Locator:
            def count(self_nonlocal):
                return 1 if is_available else 0

            @property
            def first(self_nonlocal):
                return self_nonlocal

            def click(self_nonlocal):
                if not is_available:
                    raise AssertionError(
                        f"Unexpected click for unavailable selector: {selector}"
                    )
                page.clicked_selectors.append(selector)
                return None

        return _Locator()


class FakeContextFactory:
    def __init__(self, page: FakePage):
        self.page = page
        self.plans = []
        self.attachments: list[tuple[object, str]] = []
        self.closed_handles = 0

    def launch(self, plan):
        self.plans.append(plan)
        return PersistentBrowserHandle(
            context=object(), page=self.page, close_callback=self._close
        )

    def connect_over_cdp(self, plan, endpoint_url: str):
        self.attachments.append((plan, endpoint_url))
        return PersistentBrowserHandle(
            context=object(), page=self.page, close_callback=self._close
        )

    def _close(self) -> None:
        self.closed_handles += 1


class _FakeBrowserContext:
    def __init__(self, pages: list[FakePage]):
        self.pages = pages

    def new_page(self):
        page = FakePage()
        self.pages.append(page)
        return page


class MultiPageContextFactory:
    def __init__(self, pages: list[FakePage]):
        self.pages = pages
        self.attachments: list[tuple[object, str]] = []
        self.closed_handles = 0

    def launch(self, plan):
        return PersistentBrowserHandle(
            context=_FakeBrowserContext(self.pages),
            page=self.pages[0],
            close_callback=self._close,
        )

    def connect_over_cdp(self, plan, endpoint_url: str):
        self.attachments.append((plan, endpoint_url))
        return PersistentBrowserHandle(
            context=_FakeBrowserContext(self.pages),
            page=self.pages[0],
            close_callback=self._close,
        )

    def _close(self) -> None:
        self.closed_handles += 1


class ThreadBoundFakePage(FakePage):
    def __init__(self, html: str = "", url: str = "about:blank"):
        super().__init__(html=html, url=url)
        self.owner_thread_id = threading.get_ident()

    def _assert_owner(self) -> None:
        if threading.get_ident() != self.owner_thread_id:
            raise RuntimeError("Thread-bound fake page used from a different thread")

    def goto(self, url: str) -> None:
        self._assert_owner()
        super().goto(url)

    def content(self) -> str:
        self._assert_owner()
        return super().content()

    def set_default_timeout(self, timeout: int) -> None:
        self._assert_owner()
        super().set_default_timeout(timeout)

    def set_default_navigation_timeout(self, timeout: int) -> None:
        self._assert_owner()
        super().set_default_navigation_timeout(timeout)

    def wait_for_load_state(self, _state: str, timeout: int | None = None) -> None:
        self._assert_owner()
        super().wait_for_load_state(_state, timeout=timeout)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self._assert_owner()
        super().wait_for_timeout(timeout_ms)

    def apply_storyblocks_filter(self, name: str, value: str) -> None:
        self._assert_owner()
        super().apply_storyblocks_filter(name, value)


class ThreadBoundContextFactory:
    def __init__(self, html: str, url: str):
        self.html = html
        self.url = url
        self.plans = []
        self.closed_handles = 0

    def launch(self, plan):
        self.plans.append(plan)
        page = ThreadBoundFakePage(html=self.html, url=self.url)
        return PersistentBrowserHandle(
            context=object(), page=page, close_callback=self._close
        )

    def connect_over_cdp(self, plan, endpoint_url: str):
        self.plans.append((plan, endpoint_url))
        page = ThreadBoundFakePage(html=self.html, url=self.url)
        return PersistentBrowserHandle(
            context=object(), page=page, close_callback=self._close
        )

    def _close(self) -> None:
        self.closed_handles += 1


class FlakyDownloadDriver:
    def __init__(self):
        self.calls = 0

    def download(self, request: StoryblocksDownloadRequest):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary failure")
        return b"video-bytes"


class FakeNativeProcess:
    def __init__(self):
        self.running = True

    def poll(self):
        return None if self.running else 0

    def terminate(self) -> None:
        self.running = False


class FakeNativeBrowserLauncher:
    def __init__(self):
        self.plans = []
        self.sessions: list[NativeBrowserSession] = []

    def launch(self, plan):
        self.plans.append(plan)
        session = NativeBrowserSession(plan=plan, process=FakeNativeProcess())
        self.sessions.append(session)
        return session


class FlakyNavigatingPage(FakePage):
    def __init__(self, html: str, url: str = "https://www.storyblocks.com/login"):
        super().__init__(html=html, url=url)
        self.content_calls = 0

    def content(self) -> str:
        self.content_calls += 1
        if self.content_calls == 1:
            raise RuntimeError(
                "Page.content: Unable to retrieve content because the page is navigating and changing the content."
            )
        return super().content()


class StoryblocksBrowserCoreTests(unittest.TestCase):
    def test_browser_profile_registry_creates_predictable_structure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Storyblocks",
                container.workspace.paths.browser_profiles_dir,
            )

            paths = container.profile_registry.paths_for(profile.profile_id)
            self.assertTrue(paths.root.exists())
            self.assertTrue(paths.user_data_dir.exists())
            self.assertTrue(paths.downloads_dir.exists())
            self.assertTrue(paths.diagnostics_dir.exists())

    def test_browser_channel_resolver_uses_fallback_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            edge_path = Path(temp_dir) / "msedge.exe"
            edge_path.write_bytes(b"x")

            resolver = BrowserChannelResolver(
                explicit_candidates={"chrome": [], "msedge": [edge_path]},
                which_func=lambda _: None,
            )
            availability = resolver.resolve(["chrome", "msedge"])

            self.assertEqual(availability.channel, "msedge")
            self.assertEqual(availability.executable_path, edge_path)

    def test_session_manager_opens_persistent_context_and_checks_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            page = FakePage(
                html='<div data-account-email="editor@example.com"></div><button>Download</button><a href="/logout">Logout</a>',
                url="https://www.storyblocks.com/dashboard",
            )
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=FakeContextFactory(page),
                session_probe=StoryblocksSessionProbe(),
            )

            session = manager.open_browser()
            state = manager.check_authorization()

            self.assertEqual(session.plan.browser_channel, "chrome")
            self.assertTrue(state.persistent_context_ready)
            self.assertEqual(state.health, SessionHealth.READY)
            self.assertEqual(state.storyblocks_account, "editor@example.com")

    def test_check_authorization_without_persisting_handle_closes_ui_browser_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            context_factory = FakeContextFactory(
                FakePage(
                    html='<div data-account-email="editor@example.com"></div><button>Download</button><a href="/logout">Logout</a>',
                    url="https://www.storyblocks.com/dashboard",
                )
            )
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=context_factory,
                session_probe=StoryblocksSessionProbe(),
            )

            state = manager.check_authorization(
                profile.profile_id, persist_handle=False
            )

            self.assertEqual(state.health, SessionHealth.READY)
            self.assertNotIn(profile.profile_id, manager._active_sessions)
            self.assertEqual(context_factory.closed_handles, 1)

    def test_storyblocks_backend_search_reopens_session_safely_after_ui_check(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            html = """
            <div data-account-email="editor@example.com"></div>
            <a href="/logout">Logout</a>
            <label>Input Search:</label>
            <button aria-label="Submit Search">Search</button>
            <div data-testid="justified-gallery-item-asset-123">
              <a href="/video/stock/asset-123"><img src="/thumb.jpg" alt="Jungle boat chase" /></a>
            </div>
            <button>Download</button>
            """
            context_factory = ThreadBoundContextFactory(
                html=html,
                url="https://www.storyblocks.com/dashboard",
            )
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=context_factory,
                session_probe=StoryblocksSessionProbe(),
            )
            descriptor = container.provider_registry.get("storyblocks_video")
            assert descriptor is not None
            backend = StoryblocksCandidateSearchBackend(
                provider_id="storyblocks_video",
                capability=descriptor.capability,
                descriptor=descriptor,
                session_manager=manager,
                search_adapter=StoryblocksVideoSearchAdapter(),
                dom_checker=StoryblocksDomContractChecker(),
            )

            ui_state = manager.check_authorization(
                profile.profile_id, persist_handle=False
            )
            self.assertEqual(ui_state.health, SessionHealth.READY)
            self.assertNotIn(profile.profile_id, manager._active_sessions)

            result_holder: dict[str, object] = {}

            def worker() -> None:
                try:
                    result_holder["result"] = backend.search(
                        ParagraphUnit(
                            paragraph_no=1,
                            original_index=1,
                            text="River boat scene",
                            query_bundle=QueryBundle(
                                video_queries=["river boat"], image_queries=[]
                            ),
                        ),
                        "river boat",
                        5,
                    )
                finally:
                    manager.close_browsers_owned_by_current_thread()

            thread = threading.Thread(target=worker, name="storyblocks-search-worker")
            thread.start()
            thread.join(timeout=5.0)

            self.assertFalse(thread.is_alive())
            result = result_holder["result"]
            self.assertEqual(result.candidates[0].asset_id, "asset-123")
            self.assertNotIn(profile.profile_id, manager._active_sessions)

    def test_session_probe_retries_transient_navigation_before_reading_content(
        self,
    ) -> None:
        page = FlakyNavigatingPage(
            html='<div data-account-email="editor@example.com"></div><button>Download</button><a href="/logout">Logout</a>',
            url="https://www.storyblocks.com/dashboard",
        )

        snapshot = StoryblocksSessionProbe().inspect_page(page)

        self.assertEqual(snapshot.health, SessionHealth.READY)
        self.assertEqual(page.content_calls, 2)

    def test_session_probe_prefers_ready_markers_over_generic_challenge_word(
        self,
    ) -> None:
        html = """
        <html>
          <body>
            <a href="/logout">Logout</a>
            <button>Download</button>
            <script>
              window.__featureFlags = { challengeMode: false };
            </script>
          </body>
        </html>
        """

        snapshot = StoryblocksSessionProbe().inspect_document(
            html, current_url="https://www.storyblocks.com/"
        )

        self.assertEqual(snapshot.health, SessionHealth.READY)

    def test_session_probe_does_not_treat_search_redirect_url_as_ready_when_login_is_required(
        self,
    ) -> None:
        html = "<html><body><h1>Sign in</h1><p>Login to continue</p></body></html>"

        snapshot = StoryblocksSessionProbe().inspect_document(
            html,
            current_url="https://www.storyblocks.com/login?next=%2Fall-video%2Fsearch%2Friver-boat",
        )

        self.assertEqual(snapshot.health, SessionHealth.LOGIN_REQUIRED)

    def test_session_probe_marks_browser_internal_page_as_unknown(self) -> None:
        snapshot = StoryblocksSessionProbe().inspect_document(
            "<html><body>Chrome page</body></html>",
            current_url="chrome://omnibox-popup.top-chrome/omnibox_popup.html",
        )

        self.assertEqual(snapshot.health, SessionHealth.UNKNOWN)
        self.assertIn("non-Storyblocks", snapshot.message)

    def test_playwright_download_driver_saves_file_when_remote_path_is_unavailable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            page = DownloadCapableFakePage(
                url="https://www.storyblocks.com/video/stock/asset-9"
            )
            driver = PlaywrightDownloadDriver(
                page, download_button_selectors=('button:has-text("Download")',)
            )

            path = driver.download(
                StoryblocksDownloadRequest(
                    asset_id="asset-9",
                    detail_url="https://www.storyblocks.com/video/stock/asset-9",
                    destination_dir=Path(temp_dir),
                    filename="asset-9.mp4",
                )
            )

            self.assertEqual(path, Path(temp_dir) / "asset-9.mp4")
            self.assertTrue(path.exists())

    def test_playwright_download_driver_passes_configured_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            page = TimeoutAwareDownloadPage(
                url="https://www.storyblocks.com/video/stock/asset-9"
            )
            driver = PlaywrightDownloadDriver(
                page,
                download_button_selectors=('button:has-text("Download")',),
                timeout_ms=4500,
            )

            driver.download(
                StoryblocksDownloadRequest(
                    asset_id="asset-9",
                    detail_url="https://www.storyblocks.com/video/stock/asset-9",
                    destination_dir=Path(temp_dir),
                    filename="asset-9.mp4",
                )
            )

            self.assertEqual(page.download_timeout, 4500)

    def test_playwright_download_driver_prefers_member_download_cta(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            selectors = StoryblocksSelectorCatalog().detail_download_button
            page = SelectorAwareDownloadPage(
                {
                    '.memberDownloadCta button:has-text("Download")',
                    'button:has-text("Download")',
                },
                url="https://www.storyblocks.com/video/stock/asset-9",
            )
            driver = PlaywrightDownloadDriver(page, download_button_selectors=selectors)

            driver.download(
                StoryblocksDownloadRequest(
                    asset_id="asset-9",
                    detail_url="https://www.storyblocks.com/video/stock/asset-9",
                    destination_dir=Path(temp_dir),
                    filename="asset-9.mp4",
                )
            )

            self.assertEqual(
                page.clicked_selectors,
                ['.memberDownloadCta button:has-text("Download")'],
            )

    def test_session_manager_detects_lock_and_manual_recovery_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)
            paths = container.profile_registry.paths_for(profile.profile_id)
            (paths.user_data_dir / "SingletonLock").write_text("busy", encoding="utf-8")

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=FakeContextFactory(FakePage()),
                session_probe=StoryblocksSessionProbe(),
            )

            with self.assertRaises(SessionError):
                manager.open_browser()

            (paths.user_data_dir / "SingletonLock").unlink()
            manager.open_browser()
            login_state = manager.require_manual_login(
                query="jungle boat",
                paragraph_no=3,
                rescue_url="https://www.storyblocks.com",
            )
            self.assertEqual(login_state.health, SessionHealth.LOGIN_REQUIRED)
            self.assertIsNotNone(login_state.manual_intervention)

            resumed = manager.wait_for_user(
                inspector=lambda: AuthorizationSnapshot(
                    SessionHealth.READY,
                    account="restored@example.com",
                    current_url="https://www.storyblocks.com/dashboard",
                ),
                max_checks=1,
                poll_interval_seconds=0.0,
            )
            self.assertEqual(resumed.health, SessionHealth.READY)
            self.assertIsNone(resumed.manual_intervention)
            self.assertEqual(resumed.storyblocks_account, "restored@example.com")

    def test_session_manager_starts_native_login_flow_and_attaches_to_same_browser(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            native_launcher = FakeNativeBrowserLauncher()
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=FakeContextFactory(
                    FakePage(
                        html='<div data-account-email="editor@example.com"></div><button>Download</button><a href="/logout">Logout</a>',
                        url="https://www.storyblocks.com/dashboard",
                    )
                ),
                native_browser_launcher=native_launcher,
                session_probe=StoryblocksSessionProbe(),
            )

            login_state = manager.open_native_login_browser(profile.profile_id)
            prompt = (
                login_state.manual_intervention.prompt
                if login_state.manual_intervention is not None
                else ""
            )

            self.assertEqual(login_state.health, SessionHealth.LOGIN_REQUIRED)
            self.assertEqual(native_launcher.plans[0].profile_directory_name, "Default")
            self.assertIsNotNone(login_state.native_debug_port)
            self.assertIn("then click Check Session", prompt)

            ready_state = manager.check_authorization(profile.profile_id)
            self.assertEqual(ready_state.health, SessionHealth.READY)
            self.assertTrue(ready_state.persistent_context_ready)
            attached = manager._active_sessions[profile.profile_id]
            self.assertEqual(attached.kind, "native_debug_attach")
            self.assertIn(
                f":{login_state.native_debug_port}", attached.debug_endpoint_url
            )

    def test_session_manager_prefers_storyblocks_page_after_native_attach(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            native_launcher = FakeNativeBrowserLauncher()
            pages = [
                FakePage(
                    html="<html><body>Chrome page</body></html>",
                    url="chrome://omnibox-popup.top-chrome/omnibox_popup.html",
                ),
                FakePage(
                    html='<div data-account-email="editor@example.com"></div><button>Download</button><a href="/logout">Logout</a>',
                    url="https://www.storyblocks.com/dashboard",
                ),
            ]
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=MultiPageContextFactory(pages),
                native_browser_launcher=native_launcher,
                session_probe=StoryblocksSessionProbe(),
            )

            manager.open_native_login_browser(profile.profile_id)
            ready_state = manager.check_authorization(profile.profile_id)

            self.assertEqual(ready_state.health, SessionHealth.READY)
            self.assertEqual(
                ready_state.current_url, "https://www.storyblocks.com/dashboard"
            )

    def test_storyblocks_adapters_build_urls_parse_cards_and_rescue_query(self) -> None:
        video_adapter = StoryblocksVideoSearchAdapter()
        image_adapter = StoryblocksImageSearchAdapter()
        page = FakePage()

        url = video_adapter.open_search(
            page,
            "Jungle Boat Chase",
            filters=[StoryblocksSearchFilter("orientation", "landscape")],
        )
        html = """
        <div data-testid="justified-gallery-item-asset-123">
          <a href="/video/stock/asset-123"><img src="/thumb.jpg" alt="Jungle boat chase" /></a>
        </div>
        """
        image_html = """
        <section class="stock-item-group-wrapper stock-item-v2 stock-item" data-stock-id="140522">
          <div class="box top-level-li" data-stock-id="140522">
            <div class="thumbnail-img-wrapper overflow-hidden">
              <a href="/images/stock/railroad-b5egfk8qdwj6gpk45i">
                <img src="/railroad.jpg" alt="Railroad" />
              </a>
            </div>
          </div>
        </section>
        """
        candidates = video_adapter.parse_result_cards(html, "jungle boat chase")
        image_candidates = image_adapter.parse_result_cards(image_html, "railroad")

        self.assertEqual(
            url, "https://www.storyblocks.com/all-video/search/jungle-boat-chase"
        )
        self.assertEqual(page.applied_filters, [("orientation", "landscape")])
        self.assertEqual(candidates[0].asset_id, "asset-123")
        self.assertEqual(candidates[0].kind.value, "video")
        self.assertEqual(image_candidates[0].asset_id, "140522")
        self.assertEqual(image_candidates[0].kind.value, "image")
        self.assertEqual(
            image_candidates[0].source_url,
            "https://www.storyblocks.com/images/stock/railroad-b5egfk8qdwj6gpk45i",
        )
        self.assertEqual(
            image_adapter.build_direct_search_url("forest mist"),
            "https://www.storyblocks.com/images/search/forest-mist",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)
            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=FakeContextFactory(page),
                session_probe=StoryblocksSessionProbe(),
            )
            state = manager.rescue_storyblocks_query(
                "Jungle Boat Chase", search_adapter=video_adapter
            )
            self.assertEqual(
                state.rescue_url,
                "https://www.storyblocks.com/all-video/search/jungle-boat-chase",
            )

    def test_storyblocks_pipeline_backend_uses_persistent_session_and_parses_results(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            html = """
            <div data-account-email="editor@example.com"></div>
            <a href="/logout">Logout</a>
            <label>Input Search:</label>
            <button aria-label="Submit Search">Search</button>
            <div data-testid="justified-gallery-item-asset-123">
              <a href="/video/stock/asset-123"><img src="/thumb.jpg" alt="Jungle boat chase" /></a>
            </div>
            <button>Download</button>
            """
            page = FakePage(html=html, url="https://www.storyblocks.com/dashboard")
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=FakeContextFactory(page),
                session_probe=StoryblocksSessionProbe(),
            )
            descriptor = container.provider_registry.get("storyblocks_video")
            assert descriptor is not None
            backend = StoryblocksCandidateSearchBackend(
                provider_id="storyblocks_video",
                capability=descriptor.capability,
                descriptor=descriptor,
                session_manager=manager,
                search_adapter=StoryblocksVideoSearchAdapter(),
                dom_checker=StoryblocksDomContractChecker(),
            )

            result = backend.search(
                ParagraphUnit(
                    paragraph_no=1,
                    original_index=1,
                    text="River boat scene",
                    query_bundle=QueryBundle(
                        video_queries=["river boat"], image_queries=[]
                    ),
                ),
                "river boat",
                5,
            )

            self.assertEqual(result.provider_name, "storyblocks_video")
            self.assertEqual(result.candidates[0].asset_id, "asset-123")
            self.assertTrue(result.diagnostics["dom_contract_valid"])

    def test_storyblocks_backend_uses_manual_override_until_real_page_validation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            html = """
            <div data-account-email="editor@example.com"></div>
            <a href="/logout">Logout</a>
            <label>Input Search:</label>
            <button aria-label="Submit Search">Search</button>
            <div data-testid="justified-gallery-item-asset-123">
              <a href="/video/stock/asset-123"><img src="/thumb.jpg" alt="Jungle boat chase" /></a>
            </div>
            <button>Download</button>
            """
            page = FakePage(html=html, url="https://www.storyblocks.com/dashboard")
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=FakeContextFactory(page),
                session_probe=StoryblocksSessionProbe(),
            )
            manager.set_manual_ready_override(profile.profile_id)
            descriptor = container.provider_registry.get("storyblocks_video")
            assert descriptor is not None
            backend = StoryblocksCandidateSearchBackend(
                provider_id="storyblocks_video",
                capability=descriptor.capability,
                descriptor=descriptor,
                session_manager=manager,
                search_adapter=StoryblocksVideoSearchAdapter(),
                dom_checker=StoryblocksDomContractChecker(),
            )

            result = backend.search(
                ParagraphUnit(
                    paragraph_no=1,
                    original_index=1,
                    text="River boat scene",
                    query_bundle=QueryBundle(
                        video_queries=["river boat"], image_queries=[]
                    ),
                ),
                "river boat",
                5,
            )

            self.assertEqual(result.candidates[0].asset_id, "asset-123")
            self.assertFalse(
                manager.current_state(profile.profile_id).manual_ready_override
            )

    def test_import_service_discovers_and_copies_existing_chrome_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            external_root = (
                Path(temp_dir) / "external" / "Google" / "Chrome" / "User Data"
            )
            source_profile = external_root / "Profile 7"
            (source_profile / "Network").mkdir(parents=True, exist_ok=True)
            (source_profile / "Local Storage").mkdir(parents=True, exist_ok=True)
            (source_profile / "Preferences").write_text(
                '{"profile": {"name": "Storyblocks Personal"}}', encoding="utf-8"
            )
            (source_profile / "Network" / "Cookies").write_bytes(b"cookie-db")
            (external_root / "Local State").write_text(
                '{"os_crypt": {"encrypted_key": "dummy"}}', encoding="utf-8"
            )

            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            container.profile_registry.set_active(profile.profile_id)

            service = ChromiumProfileImportService(
                container.profile_registry,
                explicit_user_data_roots={"chrome": [external_root]},
            )
            discovered = service.discover_profiles("chrome")

            self.assertEqual(len(discovered), 1)
            self.assertEqual(discovered[0].profile_label, "Storyblocks Personal")

            imported = service.import_profile(discovered[0], profile.profile_id)
            paths = container.profile_registry.paths_for(profile.profile_id)

            self.assertEqual(imported.import_source_browser, "chrome")
            self.assertEqual(imported.launch_profile_dir_name, "Profile 7")
            self.assertEqual(imported.import_source_profile_dir_name, "Profile 7")
            self.assertEqual(
                imported.import_source_profile_name, "Storyblocks Personal"
            )
            self.assertTrue(
                (paths.user_data_dir / "Profile 7" / "Preferences").exists()
            )
            self.assertTrue((paths.user_data_dir / "Local State").exists())
            self.assertTrue((paths.diagnostics_dir / "imported_session.json").exists())
            local_state = (paths.user_data_dir / "Local State").read_text(
                encoding="utf-8"
            )
            self.assertIn('"last_used": "Profile 7"', local_state)
            self.assertIn('"last_active_profiles": [', local_state)

    def test_session_manager_launches_selected_profile_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            profile = container.profile_registry.create_profile(
                "Primary",
                container.workspace.paths.browser_profiles_dir,
            )
            profile.launch_profile_dir_name = "Profile 7"
            container.profile_registry.save_profile(profile)
            container.profile_registry.set_active(profile.profile_id)

            chrome_path = Path(temp_dir) / "chrome.exe"
            chrome_path.write_bytes(b"x")
            context_factory = FakeContextFactory(
                FakePage(
                    html='<div data-account-email="editor@example.com"></div><button>Download</button><a href="/logout">Logout</a>',
                    url="https://www.storyblocks.com/dashboard",
                )
            )
            manager = BrowserSessionManager(
                container.profile_registry,
                container.settings.browser,
                channel_resolver=BrowserChannelResolver(
                    explicit_candidates={"chrome": [chrome_path]}
                ),
                context_factory=context_factory,
                session_probe=StoryblocksSessionProbe(),
            )

            manager.open_browser()

            self.assertEqual(
                context_factory.plans[0].profile_directory_name, "Profile 7"
            )

    def test_dom_contract_checker_reports_missing_selectors(self) -> None:
        checker = StoryblocksDomContractChecker()
        valid_html = """
        <label>Input Search:</label>
        <button aria-label="Submit Search">Search</button>
        <div data-testid="justified-gallery-item-asset-1"></div>
        <button>Download</button>
        """
        valid_image_html = """
        <label>Input Search:</label>
        <button aria-label="Submit Search">Search</button>
        <section class="stock-item-group-wrapper stock-item-v2 stock-item" data-stock-id="140522">
          <a href="/images/stock/railroad-b5egfk8qdwj6gpk45i">
            <img src="/railroad.jpg" alt="Railroad" />
          </a>
        </section>
        <button>Download Preview</button>
        """
        invalid_html = "<html><body>No search form here</body></html>"

        self.assertTrue(checker.validate_markup(valid_html).valid)
        self.assertTrue(checker.validate_markup(valid_image_html).valid)
        missing = checker.validate_markup(invalid_html)
        self.assertFalse(missing.valid)
        self.assertIn("search_input", missing.missing)
        self.assertIn("detail_download_button", missing.missing)

    def test_download_manager_retries_and_deduplicates_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = StoryblocksDownloadManager(FlakyDownloadDriver(), max_retries=2)
            request = StoryblocksDownloadRequest(
                asset_id="asset-42",
                detail_url="https://www.storyblocks.com/video/stock/asset-42",
                destination_dir=Path(temp_dir),
                filename="asset-42.mp4",
            )
            duplicate = StoryblocksDownloadRequest(
                asset_id="asset-42",
                detail_url="https://www.storyblocks.com/video/stock/asset-42",
                destination_dir=Path(temp_dir),
                filename="asset-42-copy.mp4",
            )

            manager.enqueue(request)
            manager.enqueue(duplicate)
            results = manager.run_queue()

            self.assertEqual(results[0].status, "completed")
            self.assertEqual(results[0].attempts, 2)
            self.assertEqual(results[1].status, "deduplicated")
            self.assertTrue((Path(temp_dir) / "asset-42.mp4").exists())

    def test_slow_mode_pacer_increases_backoff_on_instability(self) -> None:
        sleeps: list[float] = []
        pacer = BrowserActionPacer(
            SlowModePolicy(
                enabled=True,
                action_delay_ms=200,
                failure_backoff_step_seconds=0.5,
                max_backoff_seconds=1.0,
            ),
            sleep_func=sleeps.append,
        )

        self.assertEqual(pacer.before_action(), 0.2)
        self.assertEqual(sleeps, [0.2])
        self.assertEqual(pacer.record_failure(), 0.5)
        self.assertEqual(pacer.next_delay_seconds(), 0.7)
        self.assertEqual(pacer.record_failure(), 1.0)
        self.assertEqual(pacer.record_success(), 0.5)


if __name__ == "__main__":
    unittest.main()
