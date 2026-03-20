from __future__ import annotations

import json
import tempfile
import unittest

import image_fetcher
from app.bootstrap import bootstrap_application
from config.settings import default_settings
from domain.enums import ProviderCapability
from providers import (
    ImageLicensePolicy,
    ImageProviderBuildContext,
    ImageProviderSearchService,
    ImageQueryPlanner,
    SearchCandidate,
    build_default_provider_registry,
    build_image_provider_clients,
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
    def test_registry_exposes_supported_provider_allowlist(self) -> None:
        registry = build_default_provider_registry()
        settings = default_settings().providers

        all_ids = [item.provider_id for item in registry.list_all()]
        enabled = registry.resolve_enabled(
            settings,
            capability=ProviderCapability.IMAGE,
        )

        self.assertEqual(
            all_ids,
            [
                "storyblocks_video",
                "storyblocks_image",
                "pexels",
                "pixabay",
                "openverse",
            ],
        )
        self.assertEqual(
            [item.provider_id for item in enabled],
            ["storyblocks_image", "pexels", "pixabay", "openverse"],
        )

    def test_registry_preserves_explicit_enabled_provider_order(self) -> None:
        registry = build_default_provider_registry()
        settings = default_settings().providers
        settings.enabled_providers = [
            "storyblocks_video",
            "openverse",
            "pixabay",
            "storyblocks_image",
        ]

        self.assertEqual(
            [
                item.provider_id
                for item in registry.resolve_enabled(
                    settings,
                    capability=ProviderCapability.IMAGE,
                )
            ],
            ["openverse", "pixabay", "storyblocks_image"],
        )

    def test_query_planner_and_default_sources_use_supported_paths_only(self) -> None:
        registry = build_default_provider_registry()
        planner = ImageQueryPlanner()
        pexels = registry.get("pexels")
        storyblocks_image = registry.get("storyblocks_image")

        self.assertIsNotNone(pexels)
        self.assertIsNotNone(storyblocks_image)
        assert pexels is not None
        assert storyblocks_image is not None

        stock_plan = planner.rewrite_for_provider(
            pexels,
            "river boat",
            "A river boat moving through morning mist.",
        )
        storyblocks_plan = planner.rewrite_for_provider(
            storyblocks_image,
            "river boat",
            "A river boat moving through morning mist.",
        )

        self.assertIn("river boat photo", stock_plan.queries)
        self.assertIn("river boat cinematic", storyblocks_plan.queries)
        self.assertEqual(image_fetcher.DEFAULT_SOURCES, "pexels,pixabay,openverse")

    def test_build_image_provider_clients_keeps_input_order(self) -> None:
        registry = build_default_provider_registry()

        providers = build_image_provider_clients(
            registry,
            ["openverse", "pixabay"],
            ImageProviderBuildContext(
                timeout_seconds=5.0,
                user_agent="test-agent",
                pixabay_api_key="PIXABAY_DUMMY_KEY_123456",
            ),
        )

        self.assertEqual(
            [provider.provider_id for provider in providers],
            ["openverse", "pixabay"],
        )

    def test_search_and_metadata_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(), temp_dir
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

    def test_settings_repository_normalizes_legacy_removed_providers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            container = bootstrap_application(temp_dir)
            settings_path = container.workspace.paths.config_dir / "settings.json"
            removed_open_license = "wiki" + "media"
            removed_generic_web = "bi" + "ng"
            removed_flag = "allow_generic_" + "web_image"
            settings_path.write_text(
                json.dumps(
                    {
                        "providers": {
                            "project_mode": "sb_video_plus_free_images",
                            "enabled_providers": [
                                "storyblocks_video",
                                removed_open_license,
                                removed_generic_web,
                            ],
                            "default_image_providers": [removed_open_license],
                            "image_provider_priority": [
                                removed_open_license,
                                removed_generic_web,
                                "openverse",
                            ],
                            "mixed_image_fallback": True,
                            removed_flag: True,
                        }
                    }
                ),
                encoding="utf-8",
            )

            reloaded = container.settings_manager.load()

            self.assertEqual(reloaded.providers.project_mode, "sb_video_plus_free_images")
            self.assertEqual(
                reloaded.providers.enabled_providers,
                ["storyblocks_video", "pexels", "pixabay", "openverse"],
            )
            self.assertEqual(
                reloaded.providers.default_image_providers,
                ["pexels", "pixabay", "openverse"],
            )
            self.assertFalse(hasattr(reloaded.providers, "image_provider_priority"))
            self.assertFalse(hasattr(reloaded.providers, "mixed_image_fallback"))

            container.settings_manager.save(reloaded)
            saved_payload = settings_path.read_text(encoding="utf-8")
            self.assertNotIn(removed_flag, saved_payload)
            self.assertNotIn(removed_open_license, saved_payload)
            self.assertNotIn(removed_generic_web, saved_payload)
            self.assertNotIn("image_provider_priority", saved_payload)
            self.assertNotIn("mixed_image_fallback", saved_payload)

    def test_filter_pipeline_rejects_low_quality_results_from_supported_providers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(), temp_dir
            )
            noisy_descriptor = ProviderDescriptor(
                provider_id="openverse",
                display_name="Openverse",
                capability=ProviderCapability.IMAGE,
            )
            trusted_descriptor = ProviderDescriptor(
                provider_id="pexels",
                display_name="Pexels",
                capability=ProviderCapability.IMAGE,
            )
            noisy = FakeProvider(
                noisy_descriptor,
                [
                    SearchCandidate(
                        source="openverse",
                        url="https://example.com/logo-ui-meme.jpg",
                        referrer_url=None,
                        query_used="river boat",
                        license_name="CC0",
                        license_url=None,
                        author=None,
                        commercial_allowed=True,
                        attribution_required=False,
                    )
                ],
            )
            trusted = FakeProvider(
                trusted_descriptor,
                [
                    SearchCandidate(
                        source="pexels",
                        url="https://images.example.com/river-boat.jpg",
                        referrer_url="https://images.example.com/item",
                        query_used="river boat",
                        license_name="pexels-license",
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
                [noisy, trusted],
                max_candidates_per_keyword=10,
                license_policy=ImageLicensePolicy(
                    commercial_only=True, allow_attribution_licenses=False
                ),
            )
            service.close()

            self.assertEqual(found[0].source, "pexels")
            self.assertTrue(
                any(
                    reason.startswith("openverse:low_quality_prefilter")
                    for reason in diagnostics.rejected_prefilters
                )
            )

    def test_search_keyword_keeps_attribution_licensed_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = ImageProviderSearchService(
                build_default_provider_registry(), temp_dir
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
