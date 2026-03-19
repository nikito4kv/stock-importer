from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any


def get_transient_exceptions() -> tuple[type[Exception], ...]:
    try:
        from google.api_core import exceptions as google_exceptions
    except Exception:
        return ()
    return (
        google_exceptions.ResourceExhausted,
        google_exceptions.ServiceUnavailable,
        google_exceptions.InternalServerError,
    )


def ensure_gemini_sdk_available() -> None:
    try:
        from google import genai as _genai  # noqa: F401
    except Exception as exc:
        raise RuntimeError(
            "google-genai is required for Gemini integration. Install it with: pip install google-genai"
        ) from exc


@dataclass(slots=True)
class GeminiModelAdapter:
    api_key: str
    model_name: str
    _client: Any | None = None

    def _build_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _normalize_contents(self, contents: Any) -> Any:
        try:
            from google.genai import types
        except Exception:
            return contents

        if not isinstance(contents, list):
            return contents

        normalized: list[Any] = []
        for item in contents:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict) and "data" in item and "mime_type" in item:
                normalized.append(
                    types.Part.from_bytes(
                        data=item["data"],
                        mime_type=str(item["mime_type"]),
                    )
                )
            else:
                normalized.append(item)
        return normalized

    def generate_content(self, contents: Any) -> Any:
        client = self._build_client()
        response = client.models.generate_content(
            model=self.model_name,
            contents=self._normalize_contents(contents),
        )
        text = getattr(response, "text", None)
        if text is None:
            text = str(response)
        return SimpleNamespace(text=text)


def create_gemini_model(*, api_key: str, model_name: str) -> GeminiModelAdapter:
    ensure_gemini_sdk_available()
    if not api_key.strip():
        raise RuntimeError("Gemini API key is empty")
    return GeminiModelAdapter(api_key=api_key.strip(), model_name=model_name)
