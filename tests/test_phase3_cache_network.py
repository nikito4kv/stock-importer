from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bootstrap import bootstrap_application
from domain.enums import ProviderCapability, RunStatus
from domain.models import (
    ParagraphIntent,
    ParagraphUnit,
    Project,
    QueryBundle,
    ScriptDocument,
)
from legacy_core.image_providers import (
    OpenverseProvider,
    PexelsProvider,
    PixabayProvider,
)
from pipeline import MediaSelectionConfig
from pipeline.media import FreeImageCandidateSearchBackend
from providers import (
    ImageLicensePolicy,
    ImageProviderSearchService,
    SearchCandidate,
    build_default_provider_registry,
)
from providers.base import ProviderDescriptor
from providers.images.caching import SearchResultCache
from providers.images.service import ImageSearchDiagnostics
from services.errors import ProviderSearchError
from services.retry import (
    build_retry_profile,
    compute_retry_delay_seconds,
    sleep_for_retry_attempt,
)


class FakeProvider:
    def __init__(
        self,
        descriptor: ProviderDescriptor,
        candidates: list[SearchCandidate],
    ):
        self.provider_id = descriptor.provider_id
        self.descriptor = descriptor
        self._candidates = list(candidates)

    def search(
        self,
        query: str,
        limit: int,
        *,
        timeout_seconds: float | None = None,
    ) -> list[SearchCandidate]:
        del timeout_seconds
        return [
            SearchCandidate(
                source=item.source,
                url=item.url,
                referrer_url=item.referrer_url,
                query_used=query,
                license_name=item.license_name,
                license_url=item.license_url,
                author=item.author,
                commercial_allowed=item.commercial_allowed,
                attribution_required=item.attribution_required,
                rank_hint=item.rank_hint,
            )
            for item in self._candidates[:limit]
        ]


class FakeWrappedProvider:
    def __init__(self, descriptor: ProviderDescriptor):
        self.provider_id = descriptor.provider_id
        self.descriptor = descriptor
        self.http_client = None

    def close(self) -> None:
        return None


class FakeHttpClient:
    def __init__(self, exc: Exception):
        self._exc = exc

    def get_json(self, *args, **kwargs):
        del args, kwargs
        raise self._exc

    def close(self) -> None:
        return None


class FlakyFreeImageSearchService:
    def __init__(
        self, provider_id: str, *, retryable: bool, succeed_on_attempt: int | None
    ):
        self.provider_id = provider_id
        self.retryable = retryable
        self.succeed_on_attempt = succeed_on_attempt
        self.calls = 0

    def search_provider(
        self,
        keyword: str,
        paragraph_text: str,
        provider,
        *,
        max_candidates_per_keyword: int,
        license_policy: ImageLicensePolicy,
    ):
        del paragraph_text, max_candidates_per_keyword, license_policy
        self.calls += 1
        if (
            self.succeed_on_attempt is not None
            and self.calls >= self.succeed_on_attempt
        ):
            return (
                [
                    SearchCandidate(
                        source=provider.provider_id,
                        url=f"https://images.example.com/{keyword.replace(' ', '-')}.jpg",
                        referrer_url="https://images.example.com/item",
                        query_used=keyword,
                        license_name="CC0",
                        license_url=None,
                        author="Author",
                        commercial_allowed=True,
                        attribution_required=False,
                        rank_hint=9.0,
                    )
                ],
                [],
                ImageSearchDiagnostics(
                    provider_queries={provider.provider_id: [keyword]},
                    rejected_prefilters=[],
                    cache_hits=0,
                    provider_cache_hits={provider.provider_id: 0},
                    provider_rejected_prefilters={provider.provider_id: []},
                ),
            )
        raise ProviderSearchError(
            code="provider_http_503" if self.retryable else "provider_http_401",
            message=f"{provider.provider_id} upstream failure",
            provider_id=provider.provider_id,
            retryable=self.retryable,
            details={"provider_queries": [keyword], "failed_query": keyword},
        )


class Phase3CacheNetworkTests(unittest.TestCase):
    def _paragraph(self, paragraph_no: int, text: str) -> ParagraphUnit:
        return ParagraphUnit(
            paragraph_no=paragraph_no,
            original_index=paragraph_no,
            text=text,
            intent=ParagraphIntent(
                paragraph_no=paragraph_no,
                subject="river boat",
                action="drifting",
                setting="jungle river",
                primary_video_queries=[f"video query {paragraph_no}"],
                image_queries=[f"image query {paragraph_no}"],
            ),
            query_bundle=QueryBundle(
                video_queries=[f"video query {paragraph_no}"],
                image_queries=[f"image query {paragraph_no}"],
                provider_queries={"openverse": [f"free image query {paragraph_no}"]},
            ),
        )

    def _create_project(self, temp_dir: str) -> tuple[object, Project]:
        container = bootstrap_application(temp_dir)
        project = Project(
            project_id="phase3-project",
            name="Phase 3",
            workspace_path=container.workspace.paths.projects_dir,
            script_document=ScriptDocument(
                source_path=Path(temp_dir) / "story.docx",
                header_text="HEADER",
                paragraphs=[
                    self._paragraph(1, "A river boat drifting through the jungle.")
                ],
            ),
        )
        return container, container.project_repository.save(project)

    def _fake_download_asset(
        self,
        backend: FreeImageCandidateSearchBackend,
        asset,
        *,
        destination_dir: Path,
        filename: str,
        timeout_seconds: float | None = None,
    ):
        del backend, timeout_seconds
        destination_dir.mkdir(parents=True, exist_ok=True)
        local_path = destination_dir / filename
        local_path.write_bytes(b"image-bytes")
        asset.local_path = local_path
        return asset

    def test_search_service_reuses_same_candidate_without_quality_rejection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(),
                temp_dir,
            )
            descriptor = ProviderDescriptor(
                provider_id="openverse",
                display_name="Openverse",
                capability=ProviderCapability.IMAGE,
            )
            provider = FakeProvider(
                descriptor,
                [
                    SearchCandidate(
                        source="openverse",
                        url="https://example.com/logo-photo.jpg",
                        referrer_url="https://example.com/item",
                        query_used="company logo",
                        license_name="CC0",
                        license_url=None,
                        author="Author",
                        commercial_allowed=True,
                        attribution_required=False,
                    )
                ],
            )

            accepted, errors, _ = service.search_keyword(
                "company logo",
                "A clean company logo on white background",
                [provider],
                max_candidates_per_keyword=8,
                license_policy=ImageLicensePolicy(
                    commercial_only=True,
                    allow_attribution_licenses=False,
                ),
            )
            repeated, repeated_errors, diagnostics = service.search_keyword(
                "river boat",
                "A river boat drifting through mist",
                [provider],
                max_candidates_per_keyword=8,
                license_policy=ImageLicensePolicy(
                    commercial_only=True,
                    allow_attribution_licenses=False,
                ),
            )
            service.close()

            self.assertEqual(errors, [])
            self.assertEqual(repeated_errors, [])
            self.assertEqual(len(accepted), 1)
            self.assertEqual(len(repeated), 1)
            self.assertEqual(accepted[0].url, repeated[0].url)
            self.assertEqual(diagnostics.rejected_prefilters, [])
            self.assertFalse(
                (Path(temp_dir) / "provider_cache" / "metadata.sqlite").exists()
            )

    def test_search_cache_ttl_and_purge_expired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            clock = {"now": 1_000.0}
            cache = SearchResultCache(
                Path(temp_dir) / "search.sqlite",
                ttl_seconds=10,
                time_fn=lambda: clock["now"],
                cleanup_every_operations=100,
            )
            fresh = SearchCandidate(
                source="openverse",
                url="https://example.com/fresh.jpg",
                referrer_url="https://example.com/item",
                query_used="river boat",
                license_name="CC0",
                license_url=None,
                author="Author",
                commercial_allowed=True,
                attribution_required=False,
            )
            keep = SearchCandidate(
                source="openverse",
                url="https://example.com/keep.jpg",
                referrer_url="https://example.com/item",
                query_used="river keep",
                license_name="CC0",
                license_url=None,
                author="Author",
                commercial_allowed=True,
                attribution_required=False,
            )
            stale_payload = json.dumps(
                [
                    {
                        "source": "openverse",
                        "url": "https://example.com/stale.jpg",
                        "referrer_url": "https://example.com/item",
                        "query_used": "stale",
                        "license_name": "CC0",
                        "license_url": None,
                        "author": "Author",
                        "commercial_allowed": True,
                        "attribution_required": False,
                        "rank_hint": 0.0,
                    }
                ],
                ensure_ascii=False,
            )
            cache.set("openverse", "river boat", 8, [fresh])
            self.assertIsNotNone(cache.get("openverse", "river boat", 8))

            clock["now"] += 11.0
            self.assertIsNone(cache.get("openverse", "river boat", 8))

            cache.set("openverse", "river keep", 8, [keep])
            with cache._lock:
                cache._conn.execute(
                    """
                    INSERT OR REPLACE INTO search_cache(
                        provider_id,
                        query,
                        limit_value,
                        payload,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    ("openverse", "stale", 8, stale_payload, 0.0),
                )
                cache._conn.commit()

            purged = cache.purge_expired()
            self.assertEqual(purged, 1)
            self.assertIsNotNone(cache.get("openverse", "river keep", 8))
            cache.close()

    def test_search_cache_uses_persistent_connection_and_close_is_idempotent(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_connect = sqlite3.connect
            connect_calls = {"count": 0}

            def counting_connect(*args, **kwargs):
                connect_calls["count"] += 1
                return original_connect(*args, **kwargs)

            with patch(
                "providers.images.caching.sqlite3.connect", side_effect=counting_connect
            ):
                cache = SearchResultCache(Path(temp_dir) / "search.sqlite")
                candidate = SearchCandidate(
                    source="openverse",
                    url="https://example.com/river.jpg",
                    referrer_url="https://example.com/item",
                    query_used="river boat",
                    license_name="CC0",
                    license_url=None,
                    author="Author",
                    commercial_allowed=True,
                    attribution_required=False,
                )
                cache.set("openverse", "river boat", 8, [candidate])
                found = cache.get("openverse", "river boat", 8)
                self.assertIsNotNone(found)
                self.assertEqual(connect_calls["count"], 1)
                cache.close()
                cache.close()
                with self.assertRaises(RuntimeError):
                    cache.get("openverse", "river boat", 8)

    def test_search_cache_enables_wal_and_survives_concurrent_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "search.sqlite"
            cache_a = SearchResultCache(path, ttl_seconds=300)
            cache_b = SearchResultCache(path, ttl_seconds=300)
            errors: list[Exception] = []
            barrier = threading.Barrier(4)

            def writer(prefix: str, cache: SearchResultCache) -> None:
                try:
                    barrier.wait(timeout=2.0)
                    for index in range(50):
                        cache.set(
                            "openverse",
                            f"{prefix}-{index}",
                            8,
                            [
                                SearchCandidate(
                                    source="openverse",
                                    url=f"https://example.com/{prefix}-{index}.jpg",
                                    referrer_url="https://example.com/item",
                                    query_used=f"{prefix}-{index}",
                                    license_name="CC0",
                                    license_url=None,
                                    author="Author",
                                    commercial_allowed=True,
                                    attribution_required=False,
                                )
                            ],
                        )
                except Exception as exc:  # pragma: no cover - asserted via list
                    errors.append(exc)

            def reader(prefix: str, cache: SearchResultCache) -> None:
                try:
                    barrier.wait(timeout=2.0)
                    for index in range(50):
                        cache.get("openverse", f"{prefix}-{index}", 8)
                except Exception as exc:  # pragma: no cover - asserted via list
                    errors.append(exc)

            threads = [
                threading.Thread(target=writer, args=("a", cache_a)),
                threading.Thread(target=writer, args=("b", cache_b)),
                threading.Thread(target=reader, args=("a", cache_b)),
                threading.Thread(target=reader, args=("b", cache_a)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(cache_a.pragma_state.get("journal_mode"), "wal")
            self.assertEqual(errors, [])
            cached_a = cache_a.get("openverse", "a-49", 8)
            cached_b = cache_b.get("openverse", "b-49", 8)
            self.assertIsNotNone(cached_a)
            self.assertIsNotNone(cached_b)
            assert cached_a is not None
            assert cached_b is not None
            self.assertEqual(cached_a[0].url, "https://example.com/a-49.jpg")
            self.assertEqual(cached_b[0].url, "https://example.com/b-49.jpg")
            cache_a.close()
            cache_b.close()

    def test_legacy_free_image_providers_propagate_retryable_search_errors(
        self,
    ) -> None:
        provider_factories = [
            lambda client: PexelsProvider("key", 5.0, "UA", http_client=client),
            lambda client: PixabayProvider("key", 5.0, "UA", http_client=client),
            lambda client: OpenverseProvider(5.0, "UA", http_client=client),
        ]
        for factory in provider_factories:
            provider = factory(
                FakeHttpClient(
                    ProviderSearchError(
                        code="provider_http_503",
                        message="upstream unavailable",
                        provider_id="shared",
                        retryable=True,
                    )
                )
            )
            with self.assertRaises(ProviderSearchError) as ctx:
                provider.search("river boat", 5)
            self.assertTrue(ctx.exception.retryable)

    def test_free_image_backend_retryable_failure_retries_at_pipeline_level(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]
            flaky_service = FlakyFreeImageSearchService(
                "openverse",
                retryable=True,
                succeed_on_attempt=2,
            )
            container.media_pipeline.register_backend(
                FreeImageCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    provider=FakeWrappedProvider(descriptor),
                    image_search_service=flaky_service,
                    license_policy=ImageLicensePolicy(
                        commercial_only=True,
                        allow_attribution_licenses=False,
                    ),
                )
            )

            with patch.object(
                FreeImageCandidateSearchBackend,
                "download_asset",
                autospec=True,
                side_effect=self._fake_download_asset,
            ):
                run, manifest = container.media_run_service.create_and_execute(
                    project.project_id,
                    config=MediaSelectionConfig(
                        video_enabled=False,
                        storyblocks_images_enabled=False,
                        free_images_enabled=True,
                        supporting_image_limit=0,
                        fallback_image_limit=1,
                        retry_budget=2,
                        early_stop_when_satisfied=False,
                    ),
                )

            retry_events = [
                event
                for event in container.event_recorder.by_run(run.run_id)
                if event.name == "provider.search.retry"
            ]
            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(flaky_service.calls, 2)
            self.assertEqual(len(retry_events), 1)
            self.assertEqual(retry_events[0].payload["attempt_count"], 1)
            self.assertEqual(retry_events[0].payload["error_code"], "provider_http_503")
            self.assertEqual(retry_events[0].payload["final_status"], "retrying")
            entry = manifest.paragraph_entries[0]
            assert entry.selection is not None
            self.assertEqual(
                entry.selection.fallback_assets[0].provider_name, "openverse"
            )
            container.close()

    def test_free_image_backend_non_retryable_failure_stays_single_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container, project = self._create_project(temp_dir)
            descriptor = container.provider_registry.get("openverse")
            assert descriptor is not None
            container.media_pipeline._provider_settings.enabled_providers = [
                "openverse"
            ]
            flaky_service = FlakyFreeImageSearchService(
                "openverse",
                retryable=False,
                succeed_on_attempt=None,
            )
            container.media_pipeline.register_backend(
                FreeImageCandidateSearchBackend(
                    provider_id="openverse",
                    capability=ProviderCapability.IMAGE,
                    descriptor=descriptor,
                    provider=FakeWrappedProvider(descriptor),
                    image_search_service=flaky_service,
                    license_policy=ImageLicensePolicy(
                        commercial_only=True,
                        allow_attribution_licenses=False,
                    ),
                )
            )

            with patch.object(
                FreeImageCandidateSearchBackend,
                "download_asset",
                autospec=True,
                side_effect=self._fake_download_asset,
            ):
                run, manifest = container.media_run_service.create_and_execute(
                    project.project_id,
                    config=MediaSelectionConfig(
                        video_enabled=False,
                        storyblocks_images_enabled=False,
                        free_images_enabled=True,
                        supporting_image_limit=0,
                        fallback_image_limit=1,
                        retry_budget=2,
                        early_stop_when_satisfied=False,
                    ),
                )

            retry_events = [
                event
                for event in container.event_recorder.by_run(run.run_id)
                if event.name == "provider.search.retry"
            ]
            warning_events = [
                event
                for event in container.event_recorder.by_run(run.run_id)
                if event.name == "provider.search.warning"
            ]
            self.assertEqual(run.status, RunStatus.COMPLETED)
            self.assertEqual(flaky_service.calls, 1)
            self.assertEqual(retry_events, [])
            self.assertTrue(warning_events)
            self.assertEqual(warning_events[-1].payload["attempt_count"], 1)
            self.assertEqual(
                warning_events[-1].payload["error_code"], "provider_http_401"
            )
            self.assertEqual(manifest.paragraph_entries[0].status, "no_match")
            container.close()

    def test_application_container_close_closes_free_image_resources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            backend = container.media_pipeline._image_backends.get("openverse")
            _ = container.image_provider_search_service._search_cache

            container.close()
            container.close()

            self.assertTrue(
                container.image_provider_search_service._search_cache.closed
            )
            self.assertFalse(
                (Path(temp_dir) / "provider_cache" / "metadata.sqlite").exists()
            )
            self.assertIsInstance(backend, FreeImageCandidateSearchBackend)
            assert isinstance(backend, FreeImageCandidateSearchBackend)
            self.assertTrue(backend.http_client.closed)

    def test_retry_backoff_profile_is_deterministic_under_patched_jitter(self) -> None:
        profile = build_retry_profile(
            2,
            base_delay_seconds=0.1,
            max_delay_seconds=1.0,
            jitter_seconds=0.05,
        )
        with patch("services.retry.random.uniform", return_value=0.02):
            self.assertAlmostEqual(compute_retry_delay_seconds(profile, 1), 0.12)
            self.assertAlmostEqual(compute_retry_delay_seconds(profile, 2), 0.22)
        with (
            patch("services.retry.random.uniform", return_value=0.02),
            patch("services.retry.time.sleep") as sleep_mock,
        ):
            delay = sleep_for_retry_attempt(profile, 1)
        self.assertAlmostEqual(delay, 0.12)
        sleep_mock.assert_called_once_with(0.12)


if __name__ == "__main__":
    unittest.main()
