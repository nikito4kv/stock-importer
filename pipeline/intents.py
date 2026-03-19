from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

from domain.models import (
    ParagraphIntent,
    ParagraphUnit,
    QueryBundle,
    ScriptDocument,
    utc_now,
)
from legacy_core.common import normalize_whitespace, safe_float
from services.genai_client import get_transient_exceptions
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_JSON = "output/paragraph_intents.json"
DEFAULT_STRICTNESS = "balanced"
SUPPORTED_STRICTNESS = {"simple", "balanced", "strict"}
TRANSIENT_EXCEPTIONS = get_transient_exceptions() or (RuntimeError,)

QUERY_LIMITS = {
    "simple": 2,
    "balanced": 3,
    "strict": 2,
}

QUERY_WORD_LIMIT = 2

STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "he",
    "her",
    "him",
    "his",
    "in",
    "into",
    "is",
    "it",
    "its",
    "more",
    "no",
    "not",
    "of",
    "on",
    "or",
    "our",
    "she",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "those",
    "through",
    "to",
    "under",
    "until",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "were",
    "with",
    "without",
    "you",
    "your",
}

LOW_VALUE_QUERY_TOKENS = {
    "everything",
    "happened",
    "happening",
    "powerful",
    "required",
    "requirement",
    "situation",
    "thing",
    "things",
    "told",
    "telling",
}

ABSTRACT_TOKENS = {
    "anguish",
    "apathy",
    "chaos",
    "confidence",
    "despair",
    "doom",
    "doubt",
    "emotion",
    "faith",
    "fate",
    "fear",
    "feeling",
    "glory",
    "hope",
    "horror",
    "illusion",
    "indifference",
    "inevitability",
    "instinct",
    "isolation",
    "madness",
    "memory",
    "misery",
    "nightmare",
    "pain",
    "panic",
    "powerlessness",
    "pride",
    "reality",
    "sacrifice",
    "sanity",
    "spirit",
    "suffering",
    "survival",
    "terror",
    "torment",
    "uncertainty",
    "unknown",
    "vitality",
    "weakness",
    "will",
}

SETTING_TOKENS = {
    "amazon",
    "bank",
    "banks",
    "boat",
    "camp",
    "canoe",
    "deck",
    "expedition",
    "forest",
    "jungle",
    "mountain",
    "mountains",
    "ocean",
    "river",
    "sea",
    "settlement",
    "shore",
    "ship",
    "valley",
    "village",
    "water",
}

ACTION_TOKENS = {
    "attack",
    "attacking",
    "drifting",
    "fighting",
    "hiding",
    "marching",
    "rowing",
    "sailing",
    "sitting",
    "standing",
    "storming",
    "waiting",
    "watching",
}

GENERIC_QUERY_TOKENS = {
    "civilization",
    "crowd",
    "event",
    "expedition",
    "group",
    "history",
    "journey",
    "people",
    "scene",
    "story",
}


def _clean_model_text(text: str) -> str:
    cleaned = normalize_whitespace(text)
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    return cleaned


def _parse_json_object(raw_text: str) -> dict[str, Any]:
    cleaned = _clean_model_text(raw_text)
    if not cleaned:
        raise ValueError("Empty response from model")

    parsed: Any = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("Response is not a JSON object")
    return parsed


def _normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return normalize_whitespace(str(value))


def _normalize_string_list(value: Any, *, limit: int | None = None) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        current = _normalize_string(item)
        if not current:
            continue
        key = current.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(current)
        if limit is not None and len(normalized) >= limit:
            break
    return normalized


def _detect_language(text: str) -> str:
    if re.search(r"[А-Яа-яЁё]", text):
        return "ru"
    return "en"


def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", text.casefold(), flags=re.UNICODE)


def _tokenize_query_words(text: str) -> list[str]:
    return re.findall(
        r"[^\W\d_]+(?:[-'][^\W\d_]+)*",
        normalize_whitespace(text),
        flags=re.UNICODE,
    )


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _extract_focus_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _tokenize_words(text):
        if len(token) < 3:
            continue
        if (
            token in STOPWORDS
            or token in ABSTRACT_TOKENS
            or token in LOW_VALUE_QUERY_TOKENS
        ):
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def _meaningful_query_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in _tokenize_query_words(text):
        key = token.casefold()
        if len(key) < 3:
            continue
        if key in STOPWORDS or key in LOW_VALUE_QUERY_TOKENS:
            continue
        tokens.append(token)
    return tokens


def _query_variants_from_text(
    text: str,
    *,
    max_words: int = QUERY_WORD_LIMIT,
    prefer_tail: bool = False,
) -> list[str]:
    tokens = _meaningful_query_tokens(text)
    if not tokens:
        normalized = _limit_query_words(_normalize_string(text), max_words=max_words)
        return [normalized] if normalized else []

    if len(tokens) <= max_words:
        candidate = " ".join(tokens)
        return [candidate] if candidate else []

    candidates: list[str] = []
    windows = [
        " ".join(tokens[index : index + max_words])
        for index in range(len(tokens) - max_words + 1)
    ]
    if prefer_tail:
        windows = list(reversed(windows))
    candidates.extend(windows)
    candidates.append(
        " ".join(tokens[-max_words:] if prefer_tail else tokens[:max_words])
    )
    candidates.append(
        " ".join(tokens[:max_words] if prefer_tail else tokens[-max_words:])
    )
    return _unique_strings([item for item in candidates if item])


def _rank_query_candidate(
    query: str,
    *,
    subject: str,
    action: str,
    setting: str,
    paragraph_text: str,
) -> float:
    normalized = _normalize_string(query)
    tokens = [token.casefold() for token in _tokenize_query_words(normalized)]
    if not tokens:
        return -100.0

    candidate = " ".join(tokens)
    focus_terms = [token.casefold() for token in _extract_focus_terms(paragraph_text)]
    focus_bigrams = {
        f"{focus_terms[index]} {focus_terms[index + 1]}"
        for index in range(len(focus_terms) - 1)
    }
    subject_variants = {item.casefold() for item in _query_variants_from_text(subject)}
    action_variants = {item.casefold() for item in _query_variants_from_text(action)}
    setting_variants = {
        item.casefold() for item in _query_variants_from_text(setting, prefer_tail=True)
    }

    subject_match = candidate in subject_variants
    action_match = candidate in action_variants
    setting_match = candidate in setting_variants
    overlap = len(set(tokens) & set(focus_terms))
    generic_hits = sum(1 for token in tokens if token in GENERIC_QUERY_TOKENS)
    low_value_hits = sum(1 for token in tokens if token in LOW_VALUE_QUERY_TOKENS)
    abstract_hits = sum(1 for token in tokens if token in ABSTRACT_TOKENS)

    score = 0.0
    score += 1.4 if len(tokens) == QUERY_WORD_LIMIT else 0.6
    score += overlap * 1.15
    score += 8.0 if subject_match else 0.0
    score += 2.1 if setting_match else 0.0
    score += 1.8 if action_match else 0.0
    score += 2.0 if candidate in focus_bigrams else 0.0
    score += 0.8 if any(token in SETTING_TOKENS for token in tokens) else 0.0
    score += (
        0.6
        if any(token in ACTION_TOKENS or token.endswith("ing") for token in tokens)
        else 0.0
    )
    score -= 0.5 * generic_hits if subject_match else generic_hits * 2.5
    score -= low_value_hits * 4.0
    score -= abstract_hits * 3.0
    if len(tokens) == 1 and tokens[0] in GENERIC_QUERY_TOKENS:
        score -= 5.0
    return score


def _sort_query_candidates(
    candidates: list[str],
    *,
    subject: str,
    action: str,
    setting: str,
    paragraph_text: str,
) -> list[str]:
    unique = _unique_strings([_normalize_string(item) for item in candidates if item])
    return sorted(
        unique,
        key=lambda item: (
            -_rank_query_candidate(
                item,
                subject=subject,
                action=action,
                setting=setting,
                paragraph_text=paragraph_text,
            ),
            item,
        ),
    )


def _fallback_query_candidates(
    *,
    subject: str,
    action: str,
    setting: str,
    paragraph_text: str,
) -> list[str]:
    candidates: list[str] = []
    candidates.extend(_query_variants_from_text(subject))
    candidates.extend(_query_variants_from_text(setting, prefer_tail=True))
    candidates.extend(_query_variants_from_text(action))

    focus_terms = _extract_focus_terms(paragraph_text)
    for index in range(len(focus_terms) - 1):
        candidates.append(f"{focus_terms[index]} {focus_terms[index + 1]}")
    candidates.extend(focus_terms)
    filtered = [
        candidate for candidate in candidates if not _is_query_too_abstract(candidate)
    ]
    return _sort_query_candidates(
        filtered,
        subject=subject,
        action=action,
        setting=setting,
        paragraph_text=paragraph_text,
    )


def _derive_subject_action_setting(text: str) -> tuple[str, str, str]:
    focus_terms = _extract_focus_terms(text)

    subject = " ".join(focus_terms[:2])
    action = ""
    setting = ""

    for token in focus_terms:
        if not action and (token in ACTION_TOKENS or token.endswith("ing")):
            action = token
        if not setting and token in SETTING_TOKENS:
            setting = token
        if action and setting:
            break

    if not setting and len(focus_terms) >= 3:
        setting = focus_terms[2]

    return subject, action, setting


def _is_query_too_abstract(query: str) -> bool:
    tokens = [token for token in _tokenize_words(query) if token not in STOPWORDS]
    if not tokens:
        return True

    if len(tokens) <= QUERY_WORD_LIMIT and any(
        token in LOW_VALUE_QUERY_TOKENS for token in tokens
    ):
        return True

    if tokens[0] in ABSTRACT_TOKENS and len(tokens) <= 4:
        return True

    concrete_tokens = [token for token in tokens if token not in ABSTRACT_TOKENS]
    if not concrete_tokens:
        return True

    if len(concrete_tokens) == 1 and concrete_tokens[0] in {
        "civilization",
        "journey",
        "history",
        "expedition",
    }:
        return True

    return False


def _compose_visual_query(
    *,
    subject: str,
    action: str,
    setting: str,
    paragraph_text: str,
) -> str:
    candidates = _fallback_query_candidates(
        subject=subject,
        action=action,
        setting=setting,
        paragraph_text=paragraph_text,
    )
    if candidates:
        return candidates[0]

    return normalize_whitespace(paragraph_text)[:80]


def _sanitize_queries(
    raw_queries: list[str],
    *,
    subject: str,
    action: str,
    setting: str,
    paragraph_text: str,
    fallback_queries: list[str],
    limit: int,
) -> list[str]:
    kept: list[str] = []
    for fallback_query in fallback_queries:
        for candidate in _query_variants_from_text(fallback_query):
            if _is_query_too_abstract(candidate):
                continue
            kept.append(candidate)

    for raw_query in raw_queries:
        for candidate in _query_variants_from_text(raw_query):
            if _is_query_too_abstract(candidate):
                continue
            kept.append(candidate)

    kept = _limit_query_list_words(_unique_strings(kept), max_words=QUERY_WORD_LIMIT)
    kept = _sort_query_candidates(
        kept,
        subject=subject,
        action=action,
        setting=setting,
        paragraph_text=paragraph_text,
    )
    if not kept:
        kept = _fallback_query_candidates(
            subject=subject,
            action=action,
            setting=setting,
            paragraph_text=paragraph_text,
        )
    if not kept:
        kept = [
            _limit_query_words(
                normalize_whitespace(paragraph_text), max_words=QUERY_WORD_LIMIT
            )
        ]
    return kept[:limit]


def _append_photo_hint(query: str) -> str:
    lowered = query.casefold()
    if any(
        token in lowered for token in ("photo", "photograph", "portrait", "realistic")
    ):
        return query
    if (
        len([token for token in normalize_whitespace(query).split() if token])
        >= QUERY_WORD_LIMIT
    ):
        return query
    return f"{query} photo"


def _limit_query_words(query: str, *, max_words: int) -> str:
    normalized = normalize_whitespace(query)
    if not normalized or max_words < 1:
        return normalized
    words = normalized.split()
    return " ".join(words[:max_words])


def _limit_query_list_words(queries: list[str], *, max_words: int) -> list[str]:
    limited = [
        _limit_query_words(query, max_words=max_words)
        for query in queries
        if _normalize_string(query)
    ]
    return _unique_strings([query for query in limited if query])


class ParagraphIntentService:
    def _limit_intent_and_bundle_query_words(
        self,
        intent: ParagraphIntent,
        query_bundle: QueryBundle,
        *,
        max_words: int = QUERY_WORD_LIMIT,
    ) -> tuple[ParagraphIntent, QueryBundle]:
        intent.primary_video_queries = _limit_query_list_words(
            intent.primary_video_queries,
            max_words=max_words,
        )
        intent.image_queries = _limit_query_list_words(
            intent.image_queries,
            max_words=max_words,
        )

        provider_queries = {
            provider_id: _limit_query_list_words(queries, max_words=max_words)
            for provider_id, queries in query_bundle.provider_queries.items()
        }
        query_bundle.video_queries = _limit_query_list_words(
            query_bundle.video_queries,
            max_words=max_words,
        )
        query_bundle.image_queries = _limit_query_list_words(
            query_bundle.image_queries,
            max_words=max_words,
        )
        query_bundle.provider_queries = provider_queries
        return intent, query_bundle

    def _validate_strictness(self, strictness: str) -> str:
        normalized = _normalize_string(strictness).casefold() or DEFAULT_STRICTNESS
        if normalized not in SUPPORTED_STRICTNESS:
            raise ValueError(
                f"strictness must be one of: {', '.join(sorted(SUPPORTED_STRICTNESS))}"
            )
        return normalized

    def build_prompt(
        self,
        paragraph_no: int,
        paragraph_text: str,
        *,
        strictness: str,
        manual_prompt: str = "",
        full_script_context: str = "",
    ) -> str:
        strictness_value = self._validate_strictness(strictness)
        strictness_rules = {
            "simple": "Prefer direct, broad visual phrasing and keep the query set small.",
            "balanced": "Balance precision and recall; keep queries concrete and stock-friendly.",
            "strict": "Be very literal; avoid metaphor, emotion-only language, and abstract themes.",
        }[strictness_value]

        cleaned_paragraph = re.sub(
            r"^\s*\d+\s*[\.)]\s*", "", paragraph_text or ""
        ).strip()
        payload_text = (
            f"{paragraph_no}. {cleaned_paragraph}"
            if cleaned_paragraph
            else f"{paragraph_no}."
        )
        sections = [
            "Analyze the paragraph as a paragraph-first stock media task.\n"
            "Return ONLY valid JSON with this exact schema:\n"
            "{\n"
            '  "subject": "",\n'
            '  "action": "",\n'
            '  "setting": "",\n'
            '  "mood": "",\n'
            '  "style": "",\n'
            '  "negative_terms": [""],\n'
            '  "source_language": "",\n'
            '  "translated_queries": [""],\n'
            '  "estimated_duration_seconds": 0,\n'
            '  "primary_video_queries": [""],\n'
            '  "image_queries": [""]\n'
            "}\n"
            "Rules:\n"
            f"- {strictness_rules}\n"
            "- Primary video queries and image queries must be short stock-search keywords, each 1 or 2 English words only.\n"
            "- Choose the most important concrete people, objects, places, or actions from the paragraph.\n"
            "- Do not copy the opening words mechanically and do not return helper phrases like 'what told', 'situation required', or 'powerful civilization'.\n"
            "- Primary video queries must stay visually concrete, not abstract emotions or summaries.\n"
            "- Image queries may be slightly broader but still must stay visually concrete.\n"
            "- Keep source language in `source_language`.\n"
            "- Fill `translated_queries` only when translation into English helps search quality, and keep them to 1 or 2 words each.\n"
            "- `estimated_duration_seconds` is a soft hint for a single paragraph, not a segment plan.\n"
            "- Avoid words like fear, despair, fate, memory, uncertainty as standalone search keys.\n\n"
        ]
        manual_prompt_text = _normalize_string(manual_prompt)
        if manual_prompt_text:
            sections.append(f"Additional operator guidance:\n{manual_prompt_text}\n\n")
        script_context_text = _normalize_string(full_script_context)
        if script_context_text:
            sections.append(f"Optional full script context:\n{script_context_text}\n\n")
        sections.append(f"Paragraph:\n{payload_text}")
        return "".join(sections)

    def build_document_context(
        self, document: ScriptDocument, *, char_budget: int = 12000
    ) -> str:
        lines = [f"Header: {document.header_text}"]
        lines.extend(
            f"{paragraph.paragraph_no}. {normalize_whitespace(paragraph.text)}"
            for paragraph in document.paragraphs
        )
        context = "\n".join(line for line in lines if line.strip())
        budget = max(1000, int(char_budget))
        if len(context) <= budget:
            return context
        return context[: budget - 16].rstrip() + "\n[truncated context]"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(TRANSIENT_EXCEPTIONS),
        reraise=True,
    )
    def _generate_intent_raw(self, model: Any, prompt_text: str) -> str:
        response = model.generate_content(prompt_text)
        text = getattr(response, "text", "")
        return _normalize_string(text)

    def _finalize_intent(
        self,
        intent: ParagraphIntent,
        *,
        paragraph_text: str,
        strictness: str,
    ) -> ParagraphIntent:
        strictness_value = self._validate_strictness(strictness)
        limit = QUERY_LIMITS[strictness_value]

        subject = _normalize_string(intent.subject)
        action = _normalize_string(intent.action)
        setting = _normalize_string(intent.setting)
        if not subject or not setting:
            fallback_subject, fallback_action, fallback_setting = (
                _derive_subject_action_setting(paragraph_text)
            )
            subject = subject or fallback_subject
            action = action or fallback_action
            setting = setting or fallback_setting

        fallback_queries = _fallback_query_candidates(
            subject=subject,
            action=action,
            setting=setting,
            paragraph_text=paragraph_text,
        )
        visual_query = (
            fallback_queries[0]
            if fallback_queries
            else _compose_visual_query(
                subject=subject,
                action=action,
                setting=setting,
                paragraph_text=paragraph_text,
            )
        )

        intent.subject = subject
        intent.action = action
        intent.setting = setting
        intent.mood = _normalize_string(intent.mood)
        intent.style = _normalize_string(intent.style)
        intent.negative_terms = _normalize_string_list(intent.negative_terms, limit=6)
        intent.source_language = _normalize_string(
            intent.source_language
        ) or _detect_language(paragraph_text)
        intent.translated_queries = _normalize_string_list(
            intent.translated_queries, limit=6
        )
        translated_queries: list[str] = []
        for raw_query in intent.translated_queries:
            for candidate in _query_variants_from_text(raw_query):
                if _is_query_too_abstract(candidate):
                    continue
                translated_queries.append(candidate)
        intent.translated_queries = _unique_strings(translated_queries)[:6]

        duration = safe_float(intent.estimated_duration_seconds, None)
        if duration is None or duration <= 0:
            intent.estimated_duration_seconds = None
        else:
            intent.estimated_duration_seconds = round(duration, 2)

        intent.primary_video_queries = _sanitize_queries(
            _normalize_string_list(intent.primary_video_queries, limit=limit + 1),
            subject=subject,
            action=action,
            setting=setting,
            paragraph_text=paragraph_text,
            fallback_queries=fallback_queries or [visual_query],
            limit=limit,
        )
        intent.image_queries = _sanitize_queries(
            _normalize_string_list(intent.image_queries, limit=limit + 1),
            subject=subject,
            action=action,
            setting=setting,
            paragraph_text=paragraph_text,
            fallback_queries=fallback_queries or [visual_query],
            limit=limit,
        )
        return intent

    def parse_intent_response(
        self,
        raw_text: str,
        *,
        paragraph_no: int,
        paragraph_text: str,
        strictness: str,
    ) -> ParagraphIntent:
        payload = _parse_json_object(raw_text)
        intent = ParagraphIntent(
            paragraph_no=paragraph_no,
            primary_video_queries=_normalize_string_list(
                payload.get("primary_video_queries") or payload.get("video_queries"),
                limit=4,
            ),
            image_queries=_normalize_string_list(payload.get("image_queries"), limit=4),
            subject=_normalize_string(payload.get("subject")),
            action=_normalize_string(payload.get("action")),
            setting=_normalize_string(payload.get("setting")),
            mood=_normalize_string(payload.get("mood")),
            style=_normalize_string(payload.get("style")),
            negative_terms=_normalize_string_list(
                payload.get("negative_terms"), limit=6
            ),
            source_language=_normalize_string(payload.get("source_language")),
            translated_queries=_normalize_string_list(
                payload.get("translated_queries"), limit=6
            ),
            estimated_duration_seconds=safe_float(
                payload.get("estimated_duration_seconds"), None
            ),
        )
        return self._finalize_intent(
            intent, paragraph_text=paragraph_text, strictness=strictness
        )

    def _build_storyblocks_video_queries(
        self, intent: ParagraphIntent, *, strictness: str
    ) -> list[str]:
        limit = QUERY_LIMITS[strictness]
        queries = list(intent.primary_video_queries)
        queries.extend(_query_variants_from_text(intent.subject))
        queries.extend(_query_variants_from_text(intent.setting, prefer_tail=True))
        queries.extend(_query_variants_from_text(intent.action))
        queries = _limit_query_list_words(
            _unique_strings(queries), max_words=QUERY_WORD_LIMIT
        )
        return [query for query in queries if not _is_query_too_abstract(query)][:limit]

    def _build_storyblocks_image_queries(
        self, intent: ParagraphIntent, *, strictness: str
    ) -> list[str]:
        limit = QUERY_LIMITS[strictness]
        queries = list(intent.image_queries or intent.primary_video_queries)
        queries.extend(_query_variants_from_text(intent.subject))
        queries.extend(_query_variants_from_text(intent.setting, prefer_tail=True))
        queries.extend(_query_variants_from_text(intent.action))
        if strictness == "simple" and intent.subject:
            queries.extend(_query_variants_from_text(intent.subject))
        queries = _limit_query_list_words(
            _unique_strings(queries), max_words=QUERY_WORD_LIMIT
        )
        return [query for query in queries if not _is_query_too_abstract(query)][:limit]

    def _build_free_image_queries(
        self, intent: ParagraphIntent, *, strictness: str
    ) -> list[str]:
        base_queries = self._build_storyblocks_image_queries(
            intent, strictness=strictness
        )
        queries = [_append_photo_hint(query) for query in base_queries]
        if intent.translated_queries:
            queries.extend(intent.translated_queries)
        return _unique_strings(queries)[: QUERY_LIMITS[strictness]]

    def _build_generic_web_image_queries(
        self, intent: ParagraphIntent, *, strictness: str
    ) -> list[str]:
        queries = self._build_free_image_queries(intent, strictness=strictness)
        if intent.translated_queries:
            queries.extend(intent.translated_queries)
        queries = [_append_photo_hint(query) for query in queries]
        return _unique_strings(queries)[: QUERY_LIMITS[strictness]]

    def build_query_bundle(
        self,
        intent: ParagraphIntent,
        *,
        strictness: str,
        include_generic_web_image: bool = False,
    ) -> QueryBundle:
        strictness_value = self._validate_strictness(strictness)
        storyblocks_video = self._build_storyblocks_video_queries(
            intent, strictness=strictness_value
        )
        storyblocks_image = self._build_storyblocks_image_queries(
            intent, strictness=strictness_value
        )
        free_image = self._build_free_image_queries(intent, strictness=strictness_value)

        provider_queries: dict[str, list[str]] = {
            "storyblocks_video": storyblocks_video,
            "storyblocks_image": storyblocks_image,
            "free_image": free_image,
        }
        if include_generic_web_image:
            provider_queries["generic_web_image"] = (
                self._build_generic_web_image_queries(
                    intent,
                    strictness=strictness_value,
                )
            )

        image_queries = _unique_strings(storyblocks_image + free_image)
        return QueryBundle(
            video_queries=storyblocks_video,
            image_queries=image_queries,
            provider_queries=provider_queries,
        )

    def bootstrap_paragraph_intent(
        self,
        *,
        paragraph_no: int,
        paragraph_text: str,
        strictness: str = DEFAULT_STRICTNESS,
        include_generic_web_image: bool = False,
    ) -> tuple[ParagraphIntent, QueryBundle]:
        strictness_value = self._validate_strictness(strictness)
        subject, action, setting = _derive_subject_action_setting(paragraph_text)
        source_language = _detect_language(paragraph_text)
        translated_queries: list[str] = []
        if source_language != "en":
            translated_queries.append(
                _compose_visual_query(
                    subject=subject,
                    action=action,
                    setting=setting,
                    paragraph_text=paragraph_text,
                )
            )
        intent = ParagraphIntent(
            paragraph_no=paragraph_no,
            subject=subject,
            action=action,
            setting=setting,
            source_language=source_language,
            translated_queries=translated_queries,
            primary_video_queries=[],
            image_queries=[],
        )
        intent = self._finalize_intent(
            intent, paragraph_text=paragraph_text, strictness=strictness_value
        )
        query_bundle = self.build_query_bundle(
            intent,
            strictness=strictness_value,
            include_generic_web_image=include_generic_web_image,
        )
        return self._limit_intent_and_bundle_query_words(intent, query_bundle)

    def bootstrap_document(
        self,
        document: ScriptDocument,
        *,
        strictness: str = DEFAULT_STRICTNESS,
        include_generic_web_image: bool = False,
    ) -> ScriptDocument:
        strictness_value = self._validate_strictness(strictness)
        for paragraph in document.paragraphs:
            if paragraph.intent is not None and paragraph.query_bundle is not None:
                continue
            intent, query_bundle = self.bootstrap_paragraph_intent(
                paragraph_no=paragraph.paragraph_no,
                paragraph_text=paragraph.text,
                strictness=strictness_value,
                include_generic_web_image=include_generic_web_image,
            )
            paragraph.intent = intent
            paragraph.query_bundle = query_bundle
        return document

    def extract_paragraph_intent(
        self,
        model: Any,
        *,
        paragraph_no: int,
        paragraph_text: str,
        strictness: str = DEFAULT_STRICTNESS,
        format_retries: int = 2,
        include_generic_web_image: bool = False,
        manual_prompt: str = "",
        full_script_context: str = "",
    ) -> tuple[ParagraphIntent, QueryBundle]:
        strictness_value = self._validate_strictness(strictness)
        prompt = self.build_prompt(
            paragraph_no,
            paragraph_text,
            strictness=strictness_value,
            manual_prompt=manual_prompt,
            full_script_context=full_script_context,
        )

        attempts = max(1, int(format_retries) + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            raw = self._generate_intent_raw(model, prompt)
            try:
                intent = self.parse_intent_response(
                    raw,
                    paragraph_no=paragraph_no,
                    paragraph_text=paragraph_text,
                    strictness=strictness_value,
                )
                query_bundle = self.build_query_bundle(
                    intent,
                    strictness=strictness_value,
                    include_generic_web_image=include_generic_web_image,
                )
                return self._limit_intent_and_bundle_query_words(intent, query_bundle)
            except Exception as exc:
                last_error = exc
                if attempt < attempts:
                    logger.warning(
                        "Paragraph %s: bad intent response (%s). Retrying %s/%s...",
                        paragraph_no,
                        exc,
                        attempt,
                        attempts,
                    )
                    continue
        raise ValueError(
            f"Failed to parse paragraph intent for paragraph {paragraph_no}: {last_error}"
        )

    def build_item_payload(
        self,
        paragraph: ParagraphUnit,
        *,
        error: str | None = None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "paragraph_no": paragraph.paragraph_no,
            "original_index": paragraph.original_index,
            "text": paragraph.text,
            "numbering_valid": paragraph.numbering_valid,
            "validation_issues": list(paragraph.validation_issues),
            "intent": paragraph.intent.to_dict()
            if paragraph.intent is not None
            else None,
            "query_bundle": paragraph.query_bundle.to_dict()
            if paragraph.query_bundle is not None
            else None,
        }
        if error:
            payload["error"] = error
        return payload

    def extract_document(
        self,
        model: Any,
        document: ScriptDocument,
        *,
        strictness: str = DEFAULT_STRICTNESS,
        delay_seconds: float = 0.0,
        fail_fast: bool = False,
        max_workers: int = 4,
        start_jitter_seconds: float = 0.15,
        include_generic_web_image: bool = False,
        manual_prompt: str = "",
        full_script_context: str = "",
    ) -> tuple[dict[int, ParagraphIntent], list[dict[str, object]], ScriptDocument]:
        strictness_value = self._validate_strictness(strictness)
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")

        updated_paragraphs: dict[int, ParagraphUnit] = {}
        items_by_paragraph: dict[int, dict[str, object]] = {}
        intents_by_paragraph: dict[int, ParagraphIntent] = {}

        def process_one(
            paragraph: ParagraphUnit,
        ) -> tuple[int, ParagraphUnit, dict[str, object], Exception | None]:
            if start_jitter_seconds > 0:
                time.sleep(random.uniform(0.0, start_jitter_seconds))
            if delay_seconds > 0:
                time.sleep(delay_seconds)

            current = ParagraphUnit.from_dict(paragraph.to_dict())
            try:
                intent, query_bundle = self.extract_paragraph_intent(
                    model,
                    paragraph_no=current.paragraph_no,
                    paragraph_text=current.text,
                    strictness=strictness_value,
                    include_generic_web_image=include_generic_web_image,
                    manual_prompt=manual_prompt,
                    full_script_context=full_script_context,
                )
                current.intent = intent
                current.query_bundle = query_bundle
                return (
                    current.paragraph_no,
                    current,
                    self.build_item_payload(current),
                    None,
                )
            except Exception as exc:
                return (
                    current.paragraph_no,
                    current,
                    self.build_item_payload(current, error=str(exc)),
                    exc,
                )

        if max_workers == 1:
            results = [process_one(paragraph) for paragraph in document.paragraphs]
        else:
            results = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(process_one, paragraph): paragraph.paragraph_no
                    for paragraph in document.paragraphs
                }
                for future in as_completed(future_map):
                    paragraph_no = future_map[future]
                    try:
                        results.append(future.result())
                    except Exception as exc:
                        if fail_fast:
                            for pending in future_map:
                                if not pending.done():
                                    pending.cancel()
                            raise RuntimeError(
                                f"Intent extraction failed for paragraph {paragraph_no}: {exc}"
                            ) from exc
                        current = next(
                            paragraph
                            for paragraph in document.paragraphs
                            if paragraph.paragraph_no == paragraph_no
                        )
                        results.append(
                            (
                                paragraph_no,
                                current,
                                self.build_item_payload(current, error=str(exc)),
                                exc,
                            )
                        )

        for paragraph_no, paragraph, item, error in results:
            updated_paragraphs[paragraph_no] = paragraph
            items_by_paragraph[paragraph_no] = item
            if paragraph.intent is not None:
                intents_by_paragraph[paragraph_no] = paragraph.intent
                logger.info(
                    "Paragraph %s intent: %s",
                    paragraph_no,
                    paragraph.query_bundle.video_queries
                    if paragraph.query_bundle
                    else paragraph.intent.primary_video_queries,
                )
            elif error is not None:
                logger.error("Paragraph %s intent failed: %s", paragraph_no, error)
                if fail_fast:
                    raise error

        document.paragraphs = [
            updated_paragraphs[paragraph.paragraph_no]
            for paragraph in document.paragraphs
        ]
        items = [
            items_by_paragraph[paragraph.paragraph_no]
            for paragraph in document.paragraphs
        ]
        return intents_by_paragraph, items, document

    def build_output_payload(
        self,
        document: ScriptDocument,
        items: list[dict[str, object]],
        *,
        model_name: str,
        strictness: str,
        include_generic_web_image: bool,
    ) -> dict[str, object]:
        strictness_value = self._validate_strictness(strictness)
        intents_by_paragraph: dict[str, dict[str, object]] = {}
        for item in items:
            paragraph_key = _normalize_string(item.get("paragraph_no")) or "0"
            intent = item.get("intent")
            query_bundle = item.get("query_bundle")
            if isinstance(intent, dict) or isinstance(query_bundle, dict):
                intents_by_paragraph[paragraph_key] = {
                    "intent": intent,
                    "query_bundle": query_bundle,
                }

        return {
            "schema_version": 2,
            "contract": "paragraph_intents",
            "source_file": str(document.source_path),
            "header_text": document.header_text,
            "paragraphs_total": len(items),
            "model_name": model_name,
            "strictness": strictness_value,
            "include_generic_web_image": include_generic_web_image,
            "generated_at_utc": utc_now().isoformat(),
            "validation": {
                "is_valid": not bool(document.numbering_issues),
                "errors": list(document.numbering_issues),
                "warnings": [],
            },
            "paragraph_intents_by_paragraph": intents_by_paragraph,
            "items": items,
        }

    def save_intents_json(
        self,
        document: ScriptDocument,
        items: list[dict[str, object]],
        *,
        output_path: str | Path = DEFAULT_OUTPUT_JSON,
        model_name: str,
        strictness: str = DEFAULT_STRICTNESS,
        include_generic_web_image: bool = False,
    ) -> Path:
        out = Path(output_path)
        if str(out.parent) in ("", "."):
            out = Path("output") / out
        if out.suffix.lower() != ".json":
            out = out.with_suffix(".json")
        out.parent.mkdir(parents=True, exist_ok=True)

        payload = self.build_output_payload(
            document,
            items,
            model_name=model_name,
            strictness=strictness,
            include_generic_web_image=include_generic_web_image,
        )
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return out

    def apply_manual_edit(
        self,
        paragraph: ParagraphUnit,
        *,
        text: str | None = None,
        intent: ParagraphIntent | None = None,
        query_bundle: QueryBundle | None = None,
        strictness: str = DEFAULT_STRICTNESS,
        include_generic_web_image: bool = False,
    ) -> ParagraphUnit:
        strictness_value = self._validate_strictness(strictness)
        if text is not None:
            paragraph.text = normalize_whitespace(text)
        if intent is not None:
            paragraph.intent = self._finalize_intent(
                intent,
                paragraph_text=paragraph.text,
                strictness=strictness_value,
            )
        if query_bundle is not None:
            paragraph.query_bundle = query_bundle
        elif paragraph.intent is not None:
            paragraph.query_bundle = self.build_query_bundle(
                paragraph.intent,
                strictness=strictness_value,
                include_generic_web_image=include_generic_web_image,
            )
        return paragraph
