from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from .common import normalize_keywords, safe_int


@dataclass(slots=True)
class ParagraphKeywordTask:
    paragraph_no: int
    original_index: int | None
    text: str
    keywords: list[str]


def load_keywords_payload(path_value: str | Path) -> dict[str, object]:
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(f"Keywords JSON not found: {path}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Keywords JSON must contain object at root")
    return payload


def _extract_queries_from_item(raw_item: dict[str, object], *, query_kind: str) -> list[str]:
    keywords = normalize_keywords(raw_item.get("keywords"))
    if keywords:
        return keywords

    query_bundle = raw_item.get("query_bundle")
    intent = raw_item.get("intent")

    if isinstance(query_bundle, dict):
        if query_kind == "image":
            queries = normalize_keywords(query_bundle.get("image_queries"))
        else:
            queries = normalize_keywords(query_bundle.get("video_queries"))
        if queries:
            return queries

        raw_provider_queries = query_bundle.get("provider_queries")
        if isinstance(raw_provider_queries, dict):
            preferred_provider_keys = (
                ["storyblocks_image", "free_image", "generic_web_image"]
                if query_kind == "image"
                else ["storyblocks_video"]
            )
            for provider_key in preferred_provider_keys:
                queries = normalize_keywords(raw_provider_queries.get(provider_key))
                if queries:
                    return queries

    if isinstance(intent, dict):
        if query_kind == "image":
            return normalize_keywords(intent.get("image_queries"))
        return normalize_keywords(intent.get("primary_video_queries"))

    return []


def extract_paragraph_tasks(
    payload: dict[str, object],
    max_paragraphs: int | None = None,
    query_kind: str = "video",
) -> list[ParagraphKeywordTask]:
    tasks: list[ParagraphKeywordTask] = []
    items = payload.get("items")

    if isinstance(items, list):
        for idx, raw_item in enumerate(items, start=1):
            if not isinstance(raw_item, dict):
                continue

            paragraph_no = safe_int(raw_item.get("paragraph_no"), idx) or idx
            original_index = safe_int(raw_item.get("original_index"), None)
            text = str(raw_item.get("text", "")).strip()
            keywords = _extract_queries_from_item(raw_item, query_kind=query_kind)
            tasks.append(
                ParagraphKeywordTask(
                    paragraph_no=paragraph_no,
                    original_index=original_index,
                    text=text,
                    keywords=keywords,
                )
            )

    if not tasks:
        raw_map = payload.get("keywords_by_paragraph")
        if isinstance(raw_map, dict):
            sortable_keys: list[tuple[int, str]] = []
            for key in raw_map.keys():
                try:
                    sortable_keys.append((int(str(key)), str(key)))
                except (TypeError, ValueError):
                    continue

            for paragraph_no, original_key in sorted(sortable_keys, key=lambda item: item[0]):
                keywords = normalize_keywords(raw_map.get(original_key))
                tasks.append(
                    ParagraphKeywordTask(
                        paragraph_no=paragraph_no,
                        original_index=None,
                        text="",
                        keywords=keywords,
                    )
                )

    if max_paragraphs is not None and max_paragraphs > 0:
        tasks = tasks[:max_paragraphs]

    return tasks
