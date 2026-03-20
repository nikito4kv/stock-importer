from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from time import sleep
from types import SimpleNamespace
from typing import Any

from app.runtime import DesktopApplication
from domain.enums import AssetKind, ProviderCapability, RunStatus
from domain.models import AssetCandidate, ProviderResult, ScriptDocument
from pipeline import MediaSelectionConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_FIXTURES = REPO_ROOT / "docs" / "benchmarks" / "fixtures"
DEFAULT_PARAGRAPH_WORKERS = 1
DEFAULT_QUEUE_SIZE = 4
DEFAULT_SYNTHETIC_SEARCH_DELAY_MS = 12
DEFAULT_SYNTHETIC_DOWNLOAD_DELAY_MS = 8


@dataclass(frozen=True)
class ScenarioConfig:
    name: str
    fixture_path: Path
    paragraphs: int


@dataclass(slots=True)
class ScenarioSummary:
    paragraph_total_ms: list[int] = field(default_factory=list)
    provider_search_ms: list[int] = field(default_factory=list)
    download_ms: list[int] = field(default_factory=list)
    persist_ms: list[int] = field(default_factory=list)
    finalize_ms: list[int] = field(default_factory=list)
    intent_total_ms: list[int] = field(default_factory=list)
    intent_errors_total: int = 0

    def add_paragraph_record(self, metrics: dict[str, Any]) -> None:
        self.paragraph_total_ms.append(_safe_int(metrics.get("paragraph_total_ms")))
        self.provider_search_ms.append(_safe_int(metrics.get("provider_search_ms")))
        self.download_ms.append(_safe_int(metrics.get("download_ms")))
        self.persist_ms.append(_safe_int(metrics.get("persist_ms")))
        self.finalize_ms.append(_safe_int(metrics.get("finalize_ms")))

    def to_table_row(self, *, name: str, paragraphs: int) -> str:
        return (
            f"| {name} | {paragraphs} "
            f"| {_percentile(self.paragraph_total_ms, 0.50)} "
            f"| {_percentile(self.paragraph_total_ms, 0.95)} "
            f"| {_percentile(self.provider_search_ms, 0.50)} "
            f"| {_percentile(self.provider_search_ms, 0.95)} "
            f"| {_percentile(self.download_ms, 0.50)} "
            f"| {_percentile(self.download_ms, 0.95)} "
            f"| {_percentile(self.persist_ms, 0.50)} "
            f"| {_percentile(self.persist_ms, 0.95)} "
            f"| {_percentile(self.finalize_ms, 0.50)} "
            f"| {_percentile(self.finalize_ms, 0.95)} "
            f"| {_percentile(self.intent_total_ms, 0.50)} "
            f"| {_percentile(self.intent_total_ms, 0.95)} "
            f"| {self.intent_errors_total} |"
        )


@dataclass(frozen=True)
class BenchmarkRunConfig:
    paragraph_workers: int = DEFAULT_PARAGRAPH_WORKERS
    queue_size: int = DEFAULT_QUEUE_SIZE
    synthetic_search_delay_ms: int = DEFAULT_SYNTHETIC_SEARCH_DELAY_MS
    synthetic_download_delay_ms: int = DEFAULT_SYNTHETIC_DOWNLOAD_DELAY_MS


class _SyntheticIntentModel:
    def generate_content(self, prompt: str) -> SimpleNamespace:
        _ = prompt
        return SimpleNamespace(
            text=json.dumps(
                {
                    "subject": "expedition crew",
                    "action": "rowing",
                    "setting": "jungle river",
                    "mood": "tense",
                    "style": "documentary",
                    "negative_terms": ["illustration"],
                    "source_language": "en",
                    "translated_queries": [],
                    "estimated_duration_seconds": 9,
                    "primary_video_queries": ["expedition crew", "jungle river"],
                    "image_queries": ["jungle river", "rowing boat"],
                }
            )
        )


@dataclass(slots=True)
class _SyntheticVideoBenchmarkBackend:
    provider_id: str
    capability: ProviderCapability
    descriptor: Any
    search_fn: Any
    search_delay_ms: int = DEFAULT_SYNTHETIC_SEARCH_DELAY_MS
    download_delay_ms: int = DEFAULT_SYNTHETIC_DOWNLOAD_DELAY_MS

    def search(self, paragraph, query, limit):
        if self.search_delay_ms > 0:
            sleep(self.search_delay_ms / 1000.0)
        candidates = list(self.search_fn(paragraph, query, limit))
        for index, candidate in enumerate(candidates, start=1):
            candidate.provider_name = self.provider_id
            candidate.kind = (
                AssetKind.VIDEO
                if self.capability == ProviderCapability.VIDEO
                else AssetKind.IMAGE
            )
            candidate.metadata.setdefault("search_query", query)
            candidate.metadata.setdefault("rank_hint", float(max(1, limit - index + 1)))
        return ProviderResult(
            provider_name=self.provider_id,
            capability=self.capability,
            query=query,
            candidates=candidates[:limit],
        )

    def download_asset(
        self,
        asset: AssetCandidate,
        *,
        destination_dir: Path,
        filename: str,
    ) -> AssetCandidate:
        if self.download_delay_ms > 0:
            sleep(self.download_delay_ms / 1000.0)
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / filename
        destination.write_bytes(b"synthetic-benchmark-video")
        asset.local_path = destination
        asset.metadata["synthetic_download"] = True
        return asset


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _percentile(values: list[int], quantile: float) -> int:
    if not values:
        return 0
    ordered = sorted(max(0, int(item)) for item in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * max(0.0, min(1.0, float(quantile)))
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * weight
    return int(round(interpolated))


def _benchmark_scenarios() -> dict[str, ScenarioConfig]:
    return {
        "small": ScenarioConfig(
            name="small",
            fixture_path=BENCH_FIXTURES / "small-3.docx",
            paragraphs=3,
        ),
        "medium": ScenarioConfig(
            name="medium",
            fixture_path=BENCH_FIXTURES / "medium-15.docx",
            paragraphs=15,
        ),
        "large": ScenarioConfig(
            name="large",
            fixture_path=BENCH_FIXTURES / "large-40.docx",
            paragraphs=40,
        ),
    }


def _register_synthetic_backends(
    application: DesktopApplication, run_config: BenchmarkRunConfig
) -> None:
    video_descriptor = application.container.provider_registry.get("storyblocks_video")
    image_descriptor = application.container.provider_registry.get("storyblocks_image")
    if video_descriptor is None:
        raise RuntimeError("Provider descriptor 'storyblocks_video' is not available")
    if image_descriptor is None:
        raise RuntimeError("Provider descriptor 'storyblocks_image' is not available")

    def _build_search_fn(kind: str):
        def _search(paragraph, query, limit):
            _ = limit
            suffix = "mp4" if kind == "video" else "jpg"
            asset_kind = AssetKind.VIDEO if kind == "video" else AssetKind.IMAGE
            provider_id = "storyblocks_video" if kind == "video" else "storyblocks_image"
            return [
                AssetCandidate(
                    asset_id=f"{kind}-{paragraph.paragraph_no}",
                    provider_name=provider_id,
                    kind=asset_kind,
                    source_url=f"https://example.com/{kind}-{paragraph.paragraph_no}.{suffix}",
                    license_name="synthetic",
                    metadata={
                        "rank_hint": 10.0,
                        "search_query": query,
                    },
                )
            ]

        return _search

    application.container.media_pipeline.register_backend(
        _SyntheticVideoBenchmarkBackend(
            provider_id="storyblocks_video",
            capability=ProviderCapability.VIDEO,
            descriptor=video_descriptor,
            search_fn=_build_search_fn("video"),
            search_delay_ms=max(0, int(run_config.synthetic_search_delay_ms)),
            download_delay_ms=max(0, int(run_config.synthetic_download_delay_ms)),
        )
    )
    application.container.media_pipeline.register_backend(
        _SyntheticVideoBenchmarkBackend(
            provider_id="storyblocks_image",
            capability=ProviderCapability.IMAGE,
            descriptor=image_descriptor,
            search_fn=_build_search_fn("image"),
            search_delay_ms=max(0, int(run_config.synthetic_search_delay_ms)),
            download_delay_ms=max(0, int(run_config.synthetic_download_delay_ms)),
        )
    )


def _detect_total_ram_gib() -> str:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        phys_pages = int(os.sysconf("SC_PHYS_PAGES"))
        total_bytes = page_size * phys_pages
        if total_bytes > 0:
            gib = total_bytes / (1024**3)
            return f"{gib:.1f} GiB"
    except (AttributeError, OSError, ValueError):
        pass
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        for line in meminfo.read_text(encoding="utf-8").splitlines():
            if not line.startswith("MemTotal:"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                kib = int(parts[1])
            except ValueError:
                return "unknown"
            gib = kib / (1024**2)
            return f"{gib:.1f} GiB"
    return "unknown"


def _extract_intent_samples(
    application: DesktopApplication, document: ScriptDocument
) -> tuple[list[int], int]:
    model = _SyntheticIntentModel()
    copied_document = ScriptDocument.from_dict(document.to_dict())
    _intents, items, _updated = application.container.intent_service.extract_document(
        model,
        copied_document,
        strictness="balanced",
        max_workers=1,
        start_jitter_seconds=0.0,
    )
    timings = []
    for item in items:
        metrics = item.get("metrics")
        if not isinstance(metrics, dict):
            continue
        timings.append(_safe_int(metrics.get("intent_total_ms")))
    errors_total = _safe_int(
        application.container.intent_service.last_extract_metrics().get(
            "intent_errors_total"
        )
    )
    return timings, errors_total


def _read_paragraph_perf_records(perf_log_path: Path) -> list[dict[str, Any]]:
    if not perf_log_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for raw_line in perf_log_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        payload = json.loads(line)
        if payload.get("event") != "paragraph.perf":
            continue
        metrics = payload.get("metrics")
        if not isinstance(metrics, dict):
            continue
        records.append(metrics)
    return records


def _run_once(
    scenario: ScenarioConfig,
    *,
    repeat_index: int,
    workspace_root: Path,
    run_config: BenchmarkRunConfig,
) -> tuple[list[dict[str, Any]], list[int], int]:
    workspace = workspace_root / f"{scenario.name}-run-{repeat_index:02d}"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    application = DesktopApplication.create(workspace)
    application.container.orchestrator.configure(
        max_workers=max(1, int(run_config.paragraph_workers)),
        queue_size=max(1, int(run_config.queue_size)),
    )
    project = application.create_project(
        f"benchmark-{scenario.name}-{repeat_index}",
        scenario.fixture_path,
    )
    _register_synthetic_backends(application, run_config)

    assert project.script_document is not None
    intent_samples, intent_errors = _extract_intent_samples(
        application, project.script_document
    )
    run, _manifest = application.container.media_run_service.create_and_execute(
        project.project_id,
        config=MediaSelectionConfig(
            storyblocks_images_enabled=True,
            free_images_enabled=False,
        ),
    )
    if run.status != RunStatus.COMPLETED:
        raise RuntimeError(
            f"Scenario {scenario.name} repeat {repeat_index} failed with {run.status}"
        )
    perf_records = _read_paragraph_perf_records(workspace / "logs" / "perf.jsonl")
    return perf_records, intent_samples, intent_errors


def _run_benchmark(
    selected_scenarios: list[ScenarioConfig],
    *,
    repeats: int,
    workspace_root: Path,
    run_config: BenchmarkRunConfig,
) -> dict[str, ScenarioSummary]:
    summaries = {scenario.name: ScenarioSummary() for scenario in selected_scenarios}
    for scenario in selected_scenarios:
        summary = summaries[scenario.name]
        for repeat_index in range(1, repeats + 1):
            perf_records, intent_samples, intent_errors = _run_once(
                scenario,
                repeat_index=repeat_index,
                workspace_root=workspace_root,
                run_config=run_config,
            )
            for metrics in perf_records:
                summary.add_paragraph_record(metrics)
            summary.intent_total_ms.extend(intent_samples)
            summary.intent_errors_total += intent_errors
    return summaries


def _build_report(
    summaries: dict[str, ScenarioSummary],
    scenarios: list[ScenarioConfig],
    *,
    repeats: int,
    run_config: BenchmarkRunConfig,
) -> str:
    today = date.today().isoformat()
    lines = [
        f"# Baseline Report ({today})",
        "",
        "## Контекст запуска",
        "",
        f"- Дата: {today}",
        f"- ОС: {platform.platform()}",
        f"- CPU cores: {max(1, int(os.cpu_count() or 1))}",
        f"- RAM: {_detect_total_ram_gib()}",
        f"- Python: {platform.python_version()}",
        f"- Повторы: `{repeats}` на сценарий",
        f"- paragraph_workers: `{max(1, int(run_config.paragraph_workers))}`",
        f"- queue_size: `{max(1, int(run_config.queue_size))}`",
        "- Метод: synthetic run с backends "
        "`storyblocks_video`/`storyblocks_image` + deterministic delays",
        f"- synthetic search delay: `{max(0, int(run_config.synthetic_search_delay_ms))} ms`",
        f"- synthetic download delay: `{max(0, int(run_config.synthetic_download_delay_ms))} ms`",
        "- Intent method: synthetic model через "
        "`ParagraphIntentService.extract_document`",
        "- Источник метрик media: `paragraph.perf` события из `perf.jsonl`",
        "",
        "## Результаты (p50/p95, ms)",
        "",
        "| Scenario | Paragraphs | paragraph_total p50 | paragraph_total p95 | "
        "provider_search p50 | provider_search p95 | download p50 | "
        "download p95 | persist p50 | persist p95 | finalize p50 | "
        "finalize p95 | intent_total p50 | intent_total p95 | "
        "intent_errors_total |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | "
        "---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario in scenarios:
        summary = summaries[scenario.name]
        lines.append(
            summary.to_table_row(name=scenario.name, paragraphs=scenario.paragraphs)
        )
    lines.extend(
        [
            "",
            "## Ограничения",
            "",
            "- Baseline synthetic: замеряет pipeline c детерминированными latency "
            "и без внешнего network I/O.",
            "- Для production-сравнения нужен второй baseline с реальными "
            "provider calls и теми же сценариями.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    scenarios = _benchmark_scenarios()
    parser = argparse.ArgumentParser(
        description=(
            "Run synthetic phase-0 benchmark scenarios and emit baseline report."
        )
    )
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=sorted(scenarios),
        default=["small", "medium", "large"],
        help="Scenario ids to execute.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="How many repeats to run per scenario.",
    )
    parser.add_argument(
        "--paragraph-workers",
        type=int,
        default=DEFAULT_PARAGRAPH_WORKERS,
        help="Run orchestrator paragraph worker count.",
    )
    parser.add_argument(
        "--queue-size",
        type=int,
        default=DEFAULT_QUEUE_SIZE,
        help="Run orchestrator queue size.",
    )
    parser.add_argument(
        "--synthetic-search-delay-ms",
        type=int,
        default=DEFAULT_SYNTHETIC_SEARCH_DELAY_MS,
        help="Synthetic provider search delay per request.",
    )
    parser.add_argument(
        "--synthetic-download-delay-ms",
        type=int,
        default=DEFAULT_SYNTHETIC_DOWNLOAD_DELAY_MS,
        help="Synthetic download delay per asset.",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=REPO_ROOT / "workspace-benchmarks" / "phase0",
        help="Root directory for benchmark workspaces.",
    )
    parser.add_argument(
        "--write-baseline",
        type=Path,
        default=None,
        help="Optional path to write markdown baseline report.",
    )
    args = parser.parse_args()

    repeats = max(1, int(args.repeats))
    run_config = BenchmarkRunConfig(
        paragraph_workers=max(1, int(args.paragraph_workers)),
        queue_size=max(1, int(args.queue_size)),
        synthetic_search_delay_ms=max(0, int(args.synthetic_search_delay_ms)),
        synthetic_download_delay_ms=max(0, int(args.synthetic_download_delay_ms)),
    )
    selected = [scenarios[item] for item in args.scenarios]
    summaries = _run_benchmark(
        selected,
        repeats=repeats,
        workspace_root=Path(args.workspace_root),
        run_config=run_config,
    )
    report = _build_report(
        summaries,
        selected,
        repeats=repeats,
        run_config=run_config,
    )
    if args.write_baseline is not None:
        output_path = Path(args.write_baseline)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
