from __future__ import annotations

from typing import Any, Iterable


def init_provider_stats(
    provider_names: Iterable[str],
    *,
    saved_key: str,
) -> dict[str, dict[str, int]]:
    return {
        str(provider_name): {
            "search_calls": 0,
            "search_errors": 0,
            "candidates_returned": 0,
            "candidates_kept": 0,
            saved_key: 0,
        }
        for provider_name in provider_names
    }


def bump_provider_stat(
    provider_stats: dict[str, dict[str, int]],
    lock: Any,
    provider_name: str,
    key: str,
    amount: int = 1,
) -> None:
    with lock:
        stats = provider_stats.setdefault(
            provider_name,
            {
                "search_calls": 0,
                "search_errors": 0,
                "candidates_returned": 0,
                "candidates_kept": 0,
                "saved_assets": 0,
            },
        )
        stats[key] = int(stats.get(key, 0)) + int(amount)


def build_provider_limit_summary(
    *,
    provider_chain: list[str],
    provider_stats: dict[str, dict[str, int]],
    paragraph_results: list[dict[str, object]],
    asset_list_key: str,
    saved_key: str,
) -> dict[str, dict[str, object]]:
    provider_limits: dict[str, dict[str, object]] = {}

    for provider_name in provider_chain:
        stats = provider_stats.get(str(provider_name), {})
        provider_limits[str(provider_name)] = {
            "search_calls": int(stats.get("search_calls", 0)),
            "search_errors": int(stats.get("search_errors", 0)),
            "candidates_returned": int(stats.get("candidates_returned", 0)),
            "candidates_kept": int(stats.get("candidates_kept", 0)),
            saved_key: 0,
            "rate_limit": "unknown",
            "remaining": "unknown",
            "reset": "unknown",
        }

    for paragraph in paragraph_results:
        keywords = paragraph.get("keywords", [])
        if not isinstance(keywords, list):
            continue
        for keyword_info in keywords:
            if not isinstance(keyword_info, dict):
                continue
            assets = keyword_info.get(asset_list_key, [])
            if not isinstance(assets, list):
                continue
            for asset_info in assets:
                if not isinstance(asset_info, dict):
                    continue
                provider_name = str(asset_info.get("provider", "unknown"))
                stats = provider_limits.setdefault(
                    provider_name,
                    {
                        "search_calls": 0,
                        "search_errors": 0,
                        "candidates_returned": 0,
                        "candidates_kept": 0,
                        saved_key: 0,
                        "rate_limit": "unknown",
                        "remaining": "unknown",
                        "reset": "unknown",
                    },
                )
                stats[saved_key] = int(stats.get(saved_key, 0)) + 1

    return provider_limits
