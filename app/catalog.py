"""Public video model names, media limits and deterministic request validation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    id: str
    upstream_model: str
    resolution: str
    family: str

    @property
    def limits(self) -> dict[str, int]:
        return {"images": 30, "videos": 10, "audios": 10} if self.family == "2.5" else {
            "images": 9, "videos": 3, "audios": 3}

    @property
    def max_duration(self) -> int:
        return 30 if self.family == "2.5" else 15


MODELS = {
    spec.id: spec for spec in (
        ModelSpec("sd-2-0", "Seedance_2.0", "720p", "2.0"),
        ModelSpec("sd-2-0-1080p", "Seedance_2.0", "1080p", "2.0"),
        ModelSpec("sd-2-0-4k", "Seedance_2.0", "4k", "2.0"),
        ModelSpec("sd-2-0-fast", "Seedance_2.0_fast", "720p", "2.0"),
        ModelSpec("sd-2-0-mini-lite", "Seedance_2.0_mini_lite", "720p", "2.0"),
        ModelSpec("sd-2-0-mini", "Seedance_2.0_mini", "720p", "2.0"),
        ModelSpec("sd-2-0-mini-480p", "Seedance_2.0_mini", "480p", "2.0"),
        ModelSpec("sd-2-0-fast-480p", "Seedance_2.0_fast", "480p", "2.0"),
        ModelSpec("sd-2-5", "Seedance_2.5", "720p", "2.5"),
        ModelSpec("sd-2-5-480p", "Seedance_2.5", "480p", "2.5"),
        ModelSpec("sd-2-5-1080p", "Seedance_2.5", "1080p", "2.5"),
    )
}

RATIOS = {"adaptive", "16:9", "9:16", "1:1", "4:3", "3:4", "21:9"}
ALIASES = re.compile(r"(?<![\w@<])(?P<kind>视频|video|v|视|图片|image|img|图|音频|audio|音)\s*(?P<index>\d+)(?!\w)", re.I)


def _media_list(body: dict[str, Any], singular: str, plural: str) -> list[str]:
    value = body.get(plural, body.get(singular, []))
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"{plural} must be an array")
    urls: list[str] = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("url") or item.get("asset_id") or item.get("pippit_asset_id")
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{plural} contains an empty or invalid item")
        urls.append(item.strip())
    return urls


def media_alias_kind(label: str) -> str:
    word = label.lower()
    if word in {"视频", "video", "v", "视"}:
        return "videos"
    if word in {"图片", "image", "img", "图"}:
        return "images"
    return "audios"


def prompt_references(prompt: str, media: dict[str, list[str]]) -> list[tuple[str, int]]:
    found = []
    for match in ALIASES.finditer(prompt):
        kind = media_alias_kind(match.group("kind"))
        index = int(match.group("index"))
        if index < 1 or index > len(media[kind]):
            raise ValueError(f"prompt references {match.group(0)} but only {len(media[kind])} {kind} were provided")
        found.append((kind, index))
    return found


def bind_prompt(prompt: str, asset_ids: dict[str, list[str]]) -> str:
    """Convert caller labels into the asset mention format observed in Pippit Web."""
    def replace(match: re.Match[str]) -> str:
        kind = media_alias_kind(match.group("kind"))
        index = int(match.group("index"))
        if not 1 <= index <= len(asset_ids[kind]):
            raise ValueError(f"unbound media reference: {match.group(0)}")
        return f"<pippit-asset-id>{asset_ids[kind][index - 1]}</pippit-asset-id>"
    return ALIASES.sub(replace, prompt)


def normalize_request(body: dict[str, Any]) -> dict[str, Any]:
    name = str(body.get("model") or "sd-2-0-mini").strip()
    spec = MODELS.get(name)
    if spec is None:
        raise ValueError(f"unsupported model: {name}")
    prompt = str(body.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    raw_duration = body.get("duration", body.get("seconds", 4))
    if isinstance(raw_duration, bool) or not str(raw_duration).isdigit():
        raise ValueError("duration must be an integer number of seconds")
    duration = int(raw_duration)
    if not 4 <= duration <= spec.max_duration:
        raise ValueError(f"duration must be 4–{spec.max_duration} seconds for {name}")
    ratio = str(body.get("aspect_ratio") or body.get("ratio") or "16:9").lower()
    if ratio not in RATIOS:
        raise ValueError(f"unsupported aspect ratio: {ratio}")
    resolution = str(body.get("resolution") or spec.resolution).lower()
    if resolution != spec.resolution:
        raise ValueError(f"{name} uses {spec.resolution}; choose the corresponding model alias")
    media = {
        "images": _media_list(body, "images", "image_urls"),
        "videos": _media_list(body, "videos", "video_urls"),
        "audios": _media_list(body, "audios", "audio_urls"),
    }
    for kind, limit in spec.limits.items():
        if len(media[kind]) > limit:
            raise ValueError(f"{name} supports at most {limit} {kind}")
    prompt_references(prompt, media)
    return {
        "model": name, "upstream_model": spec.upstream_model,
        "duration": duration, "resolution": resolution, "aspect_ratio": ratio,
        "prompt": prompt, **media,
    }


def public_models() -> list[dict[str, Any]]:
    return [{
        "id": spec.id, "object": "model", "owned_by": "pipapi",
        "meta": {"label": spec.id},
        "capabilities": {"durations": list(range(4, spec.max_duration + 1)),
                         "resolutions": [spec.resolution],
                         "aspect_ratios": sorted(RATIOS),
                         "media_limits": spec.limits},
    } for spec in MODELS.values()]
