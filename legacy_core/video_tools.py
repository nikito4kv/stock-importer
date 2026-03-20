from __future__ import annotations

import json
import shutil
import subprocess
import urllib.parse
from pathlib import Path

from .common import safe_float, safe_int


def ensure_ffmpeg_tools_available() -> tuple[str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError(
            "ffmpeg and ffprobe are required for video validation and frame extraction. "
            "Install ffmpeg and make sure both binaries are in PATH."
        )
    return ffmpeg, ffprobe


def parse_frame_rate(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if "/" in text:
        num_raw, den_raw = text.split("/", 1)
        num = safe_float(num_raw, 0.0) or 0.0
        den = safe_float(den_raw, 0.0) or 0.0
        if den <= 0:
            return 0.0
        return max(0.0, num / den)
    return max(0.0, safe_float(text, 0.0) or 0.0)


def run_command(args: list[str], timeout_seconds: float) -> tuple[bytes, bytes]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=max(1.0, float(timeout_seconds)),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Command timed out: {' '.join(args[:4])}") from exc

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Command failed ({result.returncode}): {stderr[:240] or 'no stderr'}"
        )
    return result.stdout, result.stderr


def probe_video(
    file_path: Path,
    *,
    ffprobe_bin: str,
    timeout_seconds: float,
) -> dict[str, object]:
    stdout, _ = run_command(
        [
            ffprobe_bin,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(file_path),
        ],
        timeout_seconds=timeout_seconds,
    )

    payload = json.loads(stdout.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise ValueError("ffprobe output is not an object")

    streams = payload.get("streams")
    if not isinstance(streams, list):
        streams = []

    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and str(stream.get("codec_type")) == "video"
    ]
    if not video_streams:
        raise ValueError("No video stream found")

    def stream_score(stream: dict[str, object]) -> int:
        width = safe_int(stream.get("width"), 0) or 0
        height = safe_int(stream.get("height"), 0) or 0
        return width * height

    best_stream = max(video_streams, key=stream_score)
    format_data = payload.get("format")
    if not isinstance(format_data, dict):
        format_data = {}

    duration = safe_float(best_stream.get("duration"), None)
    if duration is None or duration <= 0:
        duration = safe_float(format_data.get("duration"), 0.0) or 0.0

    fps = parse_frame_rate(
        best_stream.get("avg_frame_rate") or best_stream.get("r_frame_rate") or ""
    )

    return {
        "width": safe_int(best_stream.get("width"), 0) or 0,
        "height": safe_int(best_stream.get("height"), 0) or 0,
        "duration_seconds": max(0.0, float(duration)),
        "fps": max(0.0, float(fps)),
        "codec_name": str(best_stream.get("codec_name") or "").strip() or "unknown",
        "pix_fmt": str(best_stream.get("pix_fmt") or "").strip() or "unknown",
        "bit_rate": safe_int(
            best_stream.get("bit_rate") or format_data.get("bit_rate"), 0
        )
        or 0,
        "format_name": str(format_data.get("format_name") or "").strip() or "unknown",
        "format_long_name": str(format_data.get("format_long_name") or "").strip()
        or "unknown",
    }


def guess_video_extension(content_type: str, final_url: str) -> str:
    mime = (content_type or "").strip().lower()
    if "mp4" in mime:
        return ".mp4"
    if "webm" in mime:
        return ".webm"
    if "quicktime" in mime:
        return ".mov"

    parsed = urllib.parse.urlparse(final_url)
    ext = Path(parsed.path).suffix.lower()
    if ext in {".mp4", ".webm", ".mov", ".m4v", ".ogv"}:
        return ext
    return ".mp4"


def validate_video_quality(
    *,
    width: int,
    height: int,
    duration_seconds: float,
    fps: float,
    min_width: int,
    min_height: int,
    min_duration_seconds: float,
    max_duration_seconds: float,
    min_fps: float,
) -> None:
    if width < min_width or height < min_height:
        raise ValueError(
            f"Video too small: {width}x{height}, minimum is {min_width}x{min_height}"
        )

    if duration_seconds < min_duration_seconds:
        raise ValueError(
            f"Video too short: {duration_seconds:.2f}s, minimum is {min_duration_seconds:.2f}s"
        )
    if duration_seconds > max_duration_seconds:
        raise ValueError(
            f"Video too long: {duration_seconds:.2f}s, maximum is {max_duration_seconds:.2f}s"
        )

    if fps <= 0:
        raise ValueError("Video FPS is missing or invalid")
    if fps < min_fps:
        raise ValueError(f"Video FPS too low: {fps:.2f}, minimum is {min_fps:.2f}")
