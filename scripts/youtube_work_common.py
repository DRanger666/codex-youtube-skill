#!/usr/bin/env python3

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


FILE_FORMAT_VERSION = 1
LOCAL_STATE_DIRECTORY = "youtube-video-work"
VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
OFFSET_PATTERN = re.compile(r"^([0-9]+(?:\.[0-9]{1,3})?)s$")

CONTENT_CLASSES = {
    "video_material",
    "task_specific_observation",
    "direct_answer",
}
OUTPUT_FORMATS = {
    "transcript": {"name": "gemini-transcript", "version": 1},
    "summary": {"name": "gemini-free-form-text", "version": 1},
    "systematic_visual_description": {
        "name": "gemini-free-form-text",
        "version": 1,
    },
    "systematic_onscreen_text": {
        "name": "gemini-free-form-text",
        "version": 1,
    },
}


class YouTubeWorkError(ValueError):
    pass


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise YouTubeWorkError(f"{label} must be a non-empty timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise YouTubeWorkError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise YouTubeWorkError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def canonical_json_bytes(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def stored_json_bytes(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_sha256(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value):
        raise YouTubeWorkError(f"{label} must be a lowercase SHA-256 hex digest")


def load_json(path: Path):
    path = Path(path).resolve()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise YouTubeWorkError(f"Cannot read JSON from {path}: {error}") from error


def write_json(path: Path, value) -> None:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(stored_json_bytes(value))
    os.replace(temporary, path)


def require_exact_fields(value, required, optional, label) -> None:
    if not isinstance(value, dict):
        raise YouTubeWorkError(f"{label} must be an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing:
        raise YouTubeWorkError(f"{label} is missing fields: {sorted(missing)}")
    if extra:
        raise YouTubeWorkError(f"{label} has unsupported fields: {sorted(extra)}")


def normalize_youtube_video_id(source: str) -> str:
    if not isinstance(source, str) or not source.strip():
        raise YouTubeWorkError("YouTube video source must be a non-empty string")
    source = source.strip()
    if VIDEO_ID_PATTERN.fullmatch(source):
        return source

    parsed = urlparse(source if "://" in source else f"https://{source}")
    host = (parsed.hostname or "").lower()
    for prefix in ("www.", "m."):
        if host.startswith(prefix):
            host = host[len(prefix) :]

    video_id = None
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif host in {"youtube.com", "youtube-nocookie.com"}:
        path_parts = [part for part in parsed.path.split("/") if part]
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
        elif len(path_parts) >= 2 and path_parts[0] in {
            "embed",
            "shorts",
            "live",
        }:
            video_id = path_parts[1]

    if not video_id or not VIDEO_ID_PATTERN.fullmatch(video_id):
        raise YouTubeWorkError(f"Cannot normalize a YouTube video ID from: {source}")
    return video_id


def canonical_youtube_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={normalize_youtube_video_id(video_id)}"


def parse_offset_ms(value: str, label: str) -> int:
    if not isinstance(value, str):
        raise YouTubeWorkError(f"{label} must be seconds ending in s")
    match = OFFSET_PATTERN.fullmatch(value)
    if not match:
        raise YouTubeWorkError(f"{label} must be seconds ending in s")
    number = match.group(1)
    whole, dot, fraction = number.partition(".")
    milliseconds = int(whole) * 1000
    if dot:
        milliseconds += int(fraction.ljust(3, "0"))
    return milliseconds


def normalize_intervals(intervals, label="time ranges"):
    if not isinstance(intervals, list):
        raise YouTubeWorkError(f"{label} must be an array")
    normalized = []
    for index, interval in enumerate(intervals):
        require_exact_fields(
            interval,
            {"startMs", "endMs"},
            set(),
            f"{label}[{index}]",
        )
        start = interval["startMs"]
        end = interval["endMs"]
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or start < 0
            or end <= start
        ):
            raise YouTubeWorkError(
                f"{label}[{index}] must satisfy 0 <= startMs < endMs"
            )
        normalized.append({"startMs": start, "endMs": end})

    normalized.sort(key=lambda item: (item["startMs"], item["endMs"]))
    merged = []
    for interval in normalized:
        if not merged or interval["startMs"] > merged[-1]["endMs"]:
            merged.append(dict(interval))
        else:
            merged[-1]["endMs"] = max(merged[-1]["endMs"], interval["endMs"])
    return merged


def intersect_intervals(left, right):
    left = normalize_intervals(left, "left time ranges")
    right = normalize_intervals(right, "right time ranges")
    intersections = []
    left_index = 0
    right_index = 0
    while left_index < len(left) and right_index < len(right):
        start = max(left[left_index]["startMs"], right[right_index]["startMs"])
        end = min(left[left_index]["endMs"], right[right_index]["endMs"])
        if start < end:
            intersections.append({"startMs": start, "endMs": end})
        if left[left_index]["endMs"] <= right[right_index]["endMs"]:
            left_index += 1
        else:
            right_index += 1
    return normalize_intervals(intersections, "intersections")


def subtract_intervals(requested, covered):
    requested = normalize_intervals(requested, "requested time ranges")
    covered = normalize_intervals(covered, "covered time ranges")
    gaps = []
    for target in requested:
        cursor = target["startMs"]
        for interval in covered:
            if interval["endMs"] <= cursor:
                continue
            if interval["startMs"] >= target["endMs"]:
                break
            if interval["startMs"] > cursor:
                gaps.append(
                    {
                        "startMs": cursor,
                        "endMs": min(interval["startMs"], target["endMs"]),
                    }
                )
            cursor = max(cursor, min(interval["endMs"], target["endMs"]))
            if cursor >= target["endMs"]:
                break
        if cursor < target["endMs"]:
            gaps.append({"startMs": cursor, "endMs": target["endMs"]})
    return normalize_intervals(gaps, "missing time ranges")


def validate_output_format(output_type: str, output_format) -> dict:
    if output_type not in OUTPUT_FORMATS:
        raise YouTubeWorkError(f"Unsupported reusable output type: {output_type}")
    require_exact_fields(output_format, {"name", "version"}, set(), "output format")
    expected = OUTPUT_FORMATS[output_type]
    if output_format != expected:
        raise YouTubeWorkError(
            f"{output_type} requires output format "
            f"{expected['name']} version {expected['version']}"
        )
    return dict(output_format)


def is_subset(intervals, container) -> bool:
    return not subtract_intervals(intervals, container)
