from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Target:
    id: str
    label: str
    default_subdir: str
    tonemap_default: bool
    tonemap_subtitle: str


TARGETS: list[Target] = [
    Target(
        id="iphone",
        label="iPhone / iOS playback",
        default_subdir="Videos/VidPrep",
        tonemap_default=True,
        tonemap_subtitle="Tone-map HDR so Photos and the iOS player show correct brightness",
    ),
    Target(
        id="resolve",
        label="DaVinci Resolve (edit)",
        default_subdir="Downloads/davinci",
        tonemap_default=True,
        tonemap_subtitle="Recommended for iPhone / HLG / Dolby Vision sources on Resolve Free",
    ),
    Target(
        id="prores",
        label="ProRes (Studio / Mac)",
        default_subdir="Downloads/davinci",
        tonemap_default=True,
        tonemap_subtitle="Tone-map HDR unless you are finishing in an HDR timeline",
    ),
    Target(
        id="web",
        label="Web / YouTube / share",
        default_subdir="Videos/VidPrep",
        tonemap_default=True,
        tonemap_subtitle="Tone-map HDR for browsers and social uploads",
    ),
]


@dataclass(frozen=True)
class Preset:
    id: str
    target: str
    label: str
    tag: str
    description: str
    container: str
    pix_fmt: str
    video_args: tuple[str, ...]
    audio_args: tuple[str, ...]
    extra_args: tuple[str, ...] = ()


PRESETS: list[Preset] = [
    Preset(
        id="iphone_h264",
        target="iphone",
        label="H.264 — Most compatible",
        tag="iPhone, iPad, Apple TV, AirDrop",
        description=(
            "MP4 with H.264 + AAC and faststart. Best default for Photos, Messages, and older devices. "
            "Use this for Resolve APV/FLAC exports that will not play on iOS."
        ),
        container=".mp4",
        pix_fmt="yuv420p",
        video_args=(
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-profile:v",
            "high",
            "-level",
            "4.2",
        ),
        audio_args=("-c:a", "aac", "-b:a", "192k"),
        extra_args=("-movflags", "+faststart"),
    ),
    Preset(
        id="iphone_h264_small",
        target="iphone",
        label="H.264 — Smaller file",
        tag="iPhone / iPad",
        description="Same compatibility as H.264 above, with a higher CRF for less disk/iCloud use.",
        container=".mp4",
        pix_fmt="yuv420p",
        video_args=(
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "23",
            "-profile:v",
            "high",
            "-level",
            "4.2",
        ),
        audio_args=("-c:a", "aac", "-b:a", "160k"),
        extra_args=("-movflags", "+faststart"),
    ),
    Preset(
        id="iphone_hevc",
        target="iphone",
        label="HEVC — Smaller (newer iPhones)",
        tag="iPhone 7 and later · tagged hvc1 for Apple",
        description=(
            "H.265/HEVC MP4 — typically much smaller than H.264 at similar quality. "
            "Prefer H.264 if you also send the file to non-Apple devices."
        ),
        container=".mp4",
        pix_fmt="yuv420p",
        video_args=("-c:v", "libx265", "-preset", "medium", "-crf", "22", "-tag:v", "hvc1"),
        audio_args=("-c:a", "aac", "-b:a", "192k"),
        extra_args=("-movflags", "+faststart"),
    ),
    Preset(
        id="dnxhr_lb",
        target="resolve",
        label="DNxHR LB — Proxy / draft",
        tag="DaVinci Resolve Free & Studio",
        description="Smallest DNxHR intermediate. Good for quick offline edits or tight disk space.",
        container=".mov",
        pix_fmt="yuv422p",
        video_args=("-c:v", "dnxhd", "-profile:v", "dnxhr_lb"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="dnxhr_sq",
        target="resolve",
        label="DNxHR SQ — Recommended edit quality",
        tag="DaVinci Resolve Free & Studio",
        description="Best balance of quality and file size for general editing.",
        container=".mov",
        pix_fmt="yuv422p",
        video_args=("-c:v", "dnxhd", "-profile:v", "dnxhr_sq"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="dnxhr_hq",
        target="resolve",
        label="DNxHR HQ — Higher quality",
        tag="DaVinci Resolve Free & Studio",
        description="Extra quality headroom for heavier color work; larger files than SQ.",
        container=".mov",
        pix_fmt="yuv422p",
        video_args=("-c:v", "dnxhd", "-profile:v", "dnxhr_hq"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="dnxhr_hqx",
        target="resolve",
        label="DNxHR HQX — 10-bit finishing",
        tag="DaVinci Resolve Studio recommended",
        description="10-bit 4:2:2 for HDR or color-critical finishing. Largest DNxHR files.",
        container=".mov",
        pix_fmt="yuv422p10le",
        video_args=("-c:v", "dnxhd", "-profile:v", "dnxhr_hqx"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="cineform",
        target="resolve",
        label="GoPro CineForm — Lightweight alternative",
        tag="DaVinci Resolve Free & Studio",
        description=(
            "Visually-lossless 10-bit intermediate. Encodes/decodes faster and is usually "
            "smaller than DNxHR HQX."
        ),
        container=".mov",
        pix_fmt="yuv422p10le",
        video_args=("-c:v", "cfhd", "-quality", "film3+"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="prores422",
        target="prores",
        label="ProRes 422 — Cross-platform delivery",
        tag="Resolve Studio / macOS — often undecodable on Resolve Free for Linux",
        description=(
            "Apple ProRes 422, 10-bit. Good when sharing with macOS editors or Studio. "
            "Resolve Free on Linux typically cannot decode ProRes — use DNxHR there."
        ),
        container=".mov",
        pix_fmt="yuv422p10le",
        video_args=("-c:v", "prores_ks", "-profile:v", "2", "-vendor", "apl0"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="prores422hq",
        target="prores",
        label="ProRes 422 HQ — Higher-quality ProRes",
        tag="Resolve Studio / macOS",
        description="Higher-bitrate ProRes for critical finishing with more headroom than standard 422.",
        container=".mov",
        pix_fmt="yuv422p10le",
        video_args=("-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0"),
        audio_args=("-c:a", "pcm_s16le"),
    ),
    Preset(
        id="web_h264",
        target="web",
        label="H.264 — YouTube / web",
        tag="Browsers, YouTube, Discord, social",
        description="Balanced MP4 for upload and embedding. Faststart enabled for quick scrubbing.",
        container=".mp4",
        pix_fmt="yuv420p",
        video_args=("-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high"),
        audio_args=("-c:a", "aac", "-b:a", "192k"),
        extra_args=("-movflags", "+faststart"),
    ),
    Preset(
        id="h264proxy",
        target="web",
        label="H.264 — Quick review proxy",
        tag="Review / sync only",
        description=(
            "Faster encode for rough cuts and client previews. "
            "Re-convert to DNxHR/ProRes/CineForm before grading."
        ),
        container=".mp4",
        pix_fmt="yuv420p",
        video_args=("-c:v", "libx264", "-preset", "fast", "-crf", "23"),
        audio_args=("-c:a", "aac", "-b:a", "160k"),
        extra_args=("-movflags", "+faststart"),
    ),
]

DEFAULT_TARGET_INDEX = next(i for i, t in enumerate(TARGETS) if t.id == "iphone")
DEFAULT_PRESET_ID = "iphone_h264"


def presets_for_target(target_id: str) -> list[Preset]:
    return [p for p in PRESETS if p.target == target_id]


def default_output_dir(target: Target) -> Path:
    path = Path.home() / target.default_subdir
    path.mkdir(parents=True, exist_ok=True)
    return path
