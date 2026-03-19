import argparse
import logging
import os
from pathlib import Path
from typing import Any

from legacy_core.env import get_env_path as shared_get_env_path
from legacy_core.env import load_dotenv as shared_load_dotenv
from legacy_core.ingestion import read_script_paragraphs as shared_read_script_paragraphs
from pipeline import ParagraphIntentService, ScriptIngestionService
from services.genai_client import create_gemini_model


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_OUTPUT_JSON = "output/paragraph_intents.json"
DEFAULT_STRICTNESS = "balanced"
ENV_FILENAME = ".env"
ENV_API_KEY = "GEMINI_API_KEY"

_INTENT_SERVICE = ParagraphIntentService()
_INGESTION_SERVICE = ScriptIngestionService()


def get_env_path() -> Path:
    return shared_get_env_path(__file__, env_filename=ENV_FILENAME)


def load_dotenv(dotenv_path: Path | None = None) -> None:
    shared_load_dotenv(dotenv_path, anchor_file=__file__, env_filename=ENV_FILENAME)


def setup_model(model_name: str = DEFAULT_MODEL):
    dotenv_path = shared_load_dotenv(anchor_file=__file__, env_filename=ENV_FILENAME)
    key = os.getenv(ENV_API_KEY, "").strip()
    if not key or key.lower() == "your_gemini_api_key_here":
        raise ValueError(
            f"{ENV_API_KEY} not found. Create {dotenv_path} from .env.example or set the environment variable."
        )
    return create_gemini_model(api_key=key, model_name=model_name)


def read_script_paragraphs(
    file_path: str | Path,
) -> tuple[str, list[dict[str, str | int | bool]]]:
    return shared_read_script_paragraphs(file_path)


def extract_intents_for_script(
    model: Any,
    document,
    *,
    strictness: str = DEFAULT_STRICTNESS,
    delay_seconds: float = 0.0,
    fail_fast: bool = False,
    max_workers: int = 10,
    start_jitter_seconds: float = 0.15,
    include_generic_web_image: bool = False,
):
    return _INTENT_SERVICE.extract_document(
        model,
        document,
        strictness=strictness,
        delay_seconds=delay_seconds,
        fail_fast=fail_fast,
        max_workers=max_workers,
        start_jitter_seconds=start_jitter_seconds,
        include_generic_web_image=include_generic_web_image,
    )


def save_paragraph_intents_json(
    document,
    items: list[dict[str, object]],
    *,
    output_path: str | Path = DEFAULT_OUTPUT_JSON,
    model_name: str = DEFAULT_MODEL,
    strictness: str = DEFAULT_STRICTNESS,
    include_generic_web_image: bool = False,
) -> Path:
    return _INTENT_SERVICE.save_intents_json(
        document,
        items,
        output_path=output_path,
        model_name=model_name,
        strictness=strictness,
        include_generic_web_image=include_generic_web_image,
    )


def run_intent_extraction(
    input_file: str | Path,
    output_path: str | Path = DEFAULT_OUTPUT_JSON,
    model_name: str = DEFAULT_MODEL,
    strictness: str = DEFAULT_STRICTNESS,
    delay_seconds: float = 0.0,
    max_workers: int = 10,
    max_paragraphs: int | None = None,
    fail_fast: bool = False,
    include_generic_web_image: bool = False,
):
    document = _INGESTION_SERVICE.ingest(input_file)
    if document.numbering_issues:
        raise ValueError("; ".join(document.numbering_issues))
    if not document.paragraphs:
        raise ValueError("No valid numbered paragraphs found in DOCX")

    if max_paragraphs is not None and max_paragraphs > 0:
        document.paragraphs = document.paragraphs[:max_paragraphs]

    model = setup_model(model_name=model_name)
    intents_by_paragraph, items, updated_document = extract_intents_for_script(
        model,
        document,
        strictness=strictness,
        delay_seconds=delay_seconds,
        fail_fast=fail_fast,
        max_workers=max_workers,
        include_generic_web_image=include_generic_web_image,
    )
    out_file = save_paragraph_intents_json(
        updated_document,
        items,
        output_path=output_path,
        model_name=model_name,
        strictness=strictness,
        include_generic_web_image=include_generic_web_image,
    )
    return intents_by_paragraph, items, out_file


def run_keyword_extraction(
    input_file: str | Path,
    output_path: str | Path = DEFAULT_OUTPUT_JSON,
    model_name: str = DEFAULT_MODEL,
    delay_seconds: float = 0.0,
    max_workers: int = 10,
    max_paragraphs: int | None = None,
    fail_fast: bool = False,
):
    return run_intent_extraction(
        input_file=input_file,
        output_path=output_path,
        model_name=model_name,
        strictness=DEFAULT_STRICTNESS,
        delay_seconds=delay_seconds,
        max_workers=max_workers,
        max_paragraphs=max_paragraphs,
        fail_fast=fail_fast,
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract structured paragraph intents for stock-media search with Gemini"
    )
    parser.add_argument("input_file", help="Path to source DOCX file")
    parser.add_argument(
        "--output",
        "-o",
        default=DEFAULT_OUTPUT_JSON,
        help="Output JSON path",
    )
    parser.add_argument(
        "--model",
        "-m",
        default=DEFAULT_MODEL,
        help="Gemini model name",
    )
    parser.add_argument(
        "--strictness",
        choices=["simple", "balanced", "strict"],
        default=DEFAULT_STRICTNESS,
        help="Intent extraction strictness",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Delay between paragraph requests in seconds",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of parallel workers for intent extraction",
    )
    parser.add_argument(
        "--max-paragraphs",
        type=int,
        help="Optional limit of paragraphs to process",
    )
    parser.add_argument(
        "--include-generic-web-image",
        action="store_true",
        help="Also generate opt-in generic web image queries",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first paragraph error",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    intents_by_paragraph, _, out_file = run_intent_extraction(
        input_file=args.input_file,
        output_path=args.output,
        model_name=args.model,
        strictness=args.strictness,
        delay_seconds=args.delay,
        max_workers=args.workers,
        max_paragraphs=args.max_paragraphs,
        fail_fast=args.fail_fast,
        include_generic_web_image=args.include_generic_web_image,
    )
    logger.info(
        "Done. Extracted intents for %s paragraph(s). Saved to: %s",
        len(intents_by_paragraph),
        out_file,
    )


if __name__ == "__main__":
    main()
