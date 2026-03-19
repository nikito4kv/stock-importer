from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import image_fetcher
from app.bootstrap import bootstrap_application
from config.settings import default_settings
from domain.enums import ProviderCapability
from providers import (
    ImageLicensePolicy,
    ImageProviderSearchService,
    ImageQueryPlanner,
    SearchCandidate,
    build_default_provider_registry,
)
from providers.base import ProviderDescriptor


class FakeProvider:
    def __init__(
        self, descriptor: ProviderDescriptor, candidates: list[SearchCandidate]
    ):
        self.provider_id = descriptor.provider_id
        self.descriptor = descriptor
        self._candidates = list(candidates)

    def search(self, query: str, limit: int) -> list[SearchCandidate]:
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


class ImageProviderArchitectureTests(unittest.TestCase):
    def test_registry_groups_priorities_and_bing_opt_in(self) -> None:
        registry = build_default_provider_registry()
        settings = default_settings().providers

        grouped = registry.list_by_group("free_stock_api", ProviderCapability.IMAGE)
        enabled = registry.resolve_enabled(
            settings, capability=ProviderCapability.IMAGE
        )
        strategy = registry.resolve_image_strategy(settings)

        self.assertEqual([item.provider_id for item in grouped], ["pexels", "pixabay"])
        self.assertEqual(enabled[0].provider_id, "storyblocks_image")
        self.assertNotIn("bing", [item.provider_id for item in enabled])
        self.assertIn(
            "bing",
            [
                item.provider_id
                for item in registry.resolve_enabled(
                    settings, capability=ProviderCapability.IMAGE, include_opt_in=True
                )
            ],
        )
        self.assertEqual(
            [item.provider_id for item in strategy["primary"]], ["storyblocks_image"]
        )
        self.assertIn("pexels", [item.provider_id for item in strategy["fallback"]])

        free_only_settings = default_settings().providers
        free_only_settings.free_images_only = True
        free_strategy = registry.resolve_image_strategy(free_only_settings)
        self.assertNotIn(
            "storyblocks_image", [item.provider_id for item in free_strategy["primary"]]
        )

    def test_query_planner_and_default_sources_favor_non_generic_paths(self) -> None:
        registry = build_default_provider_registry()
        planner = ImageQueryPlanner()
        pexels = registry.get("pexels")
        bing = registry.get("bing")
        self.assertIsNotNone(pexels)
        self.assertIsNotNone(bing)
        assert pexels is not None
        assert bing is not None
        stock_plan = planner.rewrite_for_provider(
            pexels,
            "river boat",
            "A river boat moving through morning mist.",
        )
        web_plan = planner.rewrite_for_provider(
            bing,
            "river boat",
            "A river boat moving through morning mist.",
        )

        self.assertIn("river boat photo", stock_plan.queries)
        self.assertEqual(web_plan.queries[0], "river boat photo")
        self.assertNotIn("bing", image_fetcher.DEFAULT_SOURCES)

    def test_search_and_metadata_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(), temp_dir
            )
            descriptor = ProviderDescriptor(
                provider_id="openverse",
                display_name="Openverse",
                capability=ProviderCapability.IMAGE,
                provider_group="open_license_repository",
                priority=70,
            )
            provider = FakeProvider(
                descriptor,
                [
                    SearchCandidate(
                        source="openverse",
                        url="https://example.com/good-photo.jpg",
                        referrer_url="https://example.com/item",
                        query_used="river boat",
                        license_name="CC0",
                        license_url=None,
                        author="Author",
                        commercial_allowed=True,
                        attribution_required=False,
                    )
                ],
            )

            first, errors, diagnostics = service.search_keyword(
                "river boat",
                "A river boat at dawn",
                [provider],
                max_candidates_per_keyword=10,
                license_policy=ImageLicensePolicy(
                    commercial_only=True, allow_attribution_licenses=False
                ),
            )
            second, _, second_diagnostics = service.search_keyword(
                "river boat",
                "A river boat at dawn",
                [provider],
                max_candidates_per_keyword=10,
                license_policy=ImageLicensePolicy(
                    commercial_only=True, allow_attribution_licenses=False
                ),
            )
            service.close()

            self.assertEqual(errors, [])
            self.assertEqual(first[0].url, "https://example.com/good-photo.jpg")
            self.assertEqual(second[0].url, "https://example.com/good-photo.jpg")
            self.assertEqual(diagnostics.cache_hits, 0)
            self.assertGreaterEqual(second_diagnostics.cache_hits, 1)

    def test_provider_settings_round_trip_preserves_phase5_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            settings = container.settings
            settings.providers.allow_generic_web_image = True
            settings.providers.free_images_only = True
            settings.providers.mixed_image_fallback = False
            settings.providers.image_provider_priority = [
                "openverse",
                "wikimedia",
                "bing",
            ]
            settings.providers.default_image_providers = ["openverse", "wikimedia"]

            container.settings_manager.save(settings)
            reloaded = container.settings_manager.load()

            self.assertTrue(reloaded.providers.allow_generic_web_image)
            self.assertTrue(reloaded.providers.free_images_only)
            self.assertFalse(reloaded.providers.mixed_image_fallback)
            self.assertEqual(
                reloaded.providers.image_provider_priority,
                ["openverse", "wikimedia", "bing"],
            )

    def test_filter_pipeline_rejects_low_quality_generic_web_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(), temp_dir
            )
            generic_descriptor = ProviderDescriptor(
                provider_id="bing",
                display_name="Bing",
                capability=ProviderCapability.IMAGE,
                provider_group="generic_web_image",
                priority=20,
                opt_in=True,
            )
            trusted_descriptor = ProviderDescriptor(
                provider_id="openverse",
                display_name="Openverse",
                capability=ProviderCapability.IMAGE,
                provider_group="open_license_repository",
                priority=70,
            )
            generic = FakeProvider(
                generic_descriptor,
                [
                    SearchCandidate(
                        source="bing",
                        url="https://example.com/logo-ui-meme.jpg",
                        referrer_url=None,
                        query_used="river boat",
                        license_name="unknown",
                        license_url=None,
                        author=None,
                        commercial_allowed=False,
                        attribution_required=True,
                    ),
                    SearchCandidate(
                        source="bing",
                        url="https://example.com/river-boat-photo.jpg",
                        referrer_url="https://example.com/source",
                        query_used="river boat",
                        license_name="unknown",
                        license_url=None,
                        author=None,
                        commercial_allowed=False,
                        attribution_required=True,
                    ),
                ],
            )
            trusted = FakeProvider(
                trusted_descriptor,
                [
                    SearchCandidate(
                        source="openverse",
                        url="https://images.example.com/river-boat.jpg",
                        referrer_url="https://images.example.com/item",
                        query_used="river boat",
                        license_name="CC0",
                        license_url=None,
                        author="A",
                        commercial_allowed=True,
                        attribution_required=False,
                    )
                ],
            )

            found, _, diagnostics = service.search_keyword(
                "river boat",
                "A river boat near the shore",
                [generic, trusted],
                max_candidates_per_keyword=10,
                license_policy=ImageLicensePolicy(
                    commercial_only=False, allow_attribution_licenses=True
                ),
            )
            service.close()

            self.assertEqual(found[0].source, "openverse")
            self.assertNotIn(
                "https://example.com/logo-ui-meme.jpg", [item.url for item in found]
            )
            self.assertTrue(
                any(
                    reason.startswith("bing:low_quality_prefilter")
                    for reason in diagnostics.rejected_prefilters
                )
            )

    def test_search_keyword_keeps_attribution_licensed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(), temp_dir
            )
            descriptor = ProviderDescriptor(
                provider_id="wikimedia",
                display_name="Wikimedia Commons",
                capability=ProviderCapability.IMAGE,
                provider_group="open_license_repository",
                priority=65,
            )
            provider = FakeProvider(
                descriptor,
                [
                    SearchCandidate(
                        source="wikimedia",
                        url="https://images.example.com/boat.jpg",
                        referrer_url="https://images.example.com/item",
                        query_used="river boat",
                        license_name="CC BY-SA 4.0",
                        license_url=None,
                        author="Author",
                        commercial_allowed=False,
                        attribution_required=True,
                    )
                ],
            )

            found, errors, _diagnostics = service.search_keyword(
                "river boat",
                "A river boat near the shore",
                [provider],
                max_candidates_per_keyword=10,
                license_policy=ImageLicensePolicy(
                    commercial_only=True,
                    allow_attribution_licenses=False,
                ),
            )
            service.close()

            self.assertEqual(errors, [])
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].url, "https://images.example.com/boat.jpg")


if __name__ == "__main__":
    unittest.main()
