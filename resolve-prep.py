#!/usr/bin/env python3
"""VidPrep — convert videos for phones, editors, and the web."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "dev.bobo.vidprep"
APP_NAME = "VidPrep"
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".mkv",
    ".avi",
    ".m4v",
    ".webm",
    ".mts",
    ".m2ts",
    ".mxf",
    ".wmv",
    ".flv",
    ".ts",
}


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

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def format_bytes(num_bytes: float) -> str:
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


@dataclass
class Job:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    input_size: int = 0
    output_size: int = 0
    status: str = "Queued"
    progress: float = 0.0
    error: str = ""
    row: Gtk.ListBoxRow | None = field(default=None, repr=False)
    progress_bar: Gtk.ProgressBar | None = field(default=None, repr=False)
    status_label: Gtk.Label | None = field(default=None, repr=False)


def which_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def which_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def probe_media(path: Path) -> tuple[float, int, int, str]:
    ffprobe = which_ffprobe()
    if not ffprobe:
        return 0.0, 0, 0, ""
    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return 0.0, 0, 0, ""

    duration = float(data.get("format", {}).get("duration") or 0)
    width = height = 0
    codec = ""
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video":
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            codec = stream.get("codec_name") or ""
            if not duration:
                duration = float(stream.get("duration") or 0)
            break
    return duration, width, height, codec


def is_hdr_stream(path: Path) -> bool:
    ffprobe = which_ffprobe()
    if not ffprobe:
        return False
    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=color_transfer,color_primaries,color_space,side_data_list",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return False

    streams = data.get("streams") or []
    if not streams:
        return False
    stream = streams[0]
    transfer = (stream.get("color_transfer") or "").lower()
    if transfer in {"smpte2084", "arib-std-b67", "bt2020-10", "bt2020-12"}:
        return True
    for side in stream.get("side_data_list") or []:
        if "DOVI" in json.dumps(side).upper() or "dolby" in json.dumps(side).lower():
            return True
    return False


def output_path_for(source: Path, out_dir: Path, preset: Preset) -> Path:
    stem = re.sub(r"[^\w.\-]+", "_", source.stem).strip("_") or "clip"
    candidate = out_dir / f"{stem}_{preset.id}{preset.container}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = out_dir / f"{stem}_{preset.id}_{n}{preset.container}"
        if not candidate.exists():
            return candidate
        n += 1


def build_ffmpeg_cmd(
    source: Path,
    dest: Path,
    preset: Preset,
    tonemap: bool,
) -> list[str]:
    ffmpeg = which_ffmpeg()
    assert ffmpeg

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
    ]

    filters: list[str] = []
    if tonemap and is_hdr_stream(source):
        filters.append(
            "zscale=t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=hable:desat=0,"
            f"zscale=t=bt709:m=bt709:r=tv,format={preset.pix_fmt}"
        )
    else:
        filters.append(f"format={preset.pix_fmt}")

    cmd.extend(["-vf", ",".join(filters)])
    cmd.extend(preset.video_args)
    cmd.extend(preset.audio_args)
    cmd.extend(preset.extra_args)
    cmd.append(str(dest))
    return cmd


def presets_for_target(target_id: str) -> list[Preset]:
    return [p for p in PRESETS if p.target == target_id]


def default_output_dir(target: Target) -> Path:
    path = Path.home() / target.default_subdir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value.rstrip("x"))
    except ValueError:
        return 0.0


def _safe_int(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def frac_from_snapshot(snapshot: dict[str, str], duration: float) -> float | None:
    if duration <= 0:
        return None
    if "out_time_ms" in snapshot:
        try:
            ms = int(snapshot["out_time_ms"])
            return min(max(ms / (duration * 1_000_000), 0.0), 1.0)
        except ValueError:
            pass
    if "out_time" in snapshot:
        match = TIME_RE.search(f"time={snapshot['out_time']}")
        if match:
            h, m, s = match.groups()
            seconds = int(h) * 3600 + int(m) * 60 + float(s)
            return min(max(seconds / duration, 0.0), 1.0)
    return None


class VidPrepWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=APP_NAME, default_width=820, default_height=640)
        self.set_icon_name("applications-multimedia")

        self.jobs: list[Job] = []
        self._worker: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._busy = False
        self._preset_choices: list[Preset] = []
        self._auto_folder = True

        self.output_dir = default_output_dir(TARGETS[DEFAULT_TARGET_INDEX])

        self._build_ui()
        self._update_sensitivity()

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=APP_NAME))
        toolbar_view.add_top_bar(header)

        add_btn = Gtk.Button(label="Add Videos")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_clicked)
        header.pack_start(add_btn)

        clear_btn = Gtk.Button(label="Clear")
        clear_btn.connect("clicked", self._on_clear_clicked)
        header.pack_start(clear_btn)

        self.convert_btn = Gtk.Button(label="Convert")
        self.convert_btn.add_css_class("destructive-action")
        self.convert_btn.connect("clicked", self._on_convert_clicked)
        header.pack_end(self.convert_btn)

        self.cancel_btn = Gtk.Button(label="Cancel")
        self.cancel_btn.set_visible(False)
        self.cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_end(self.cancel_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        outer.set_margin_top(16)
        outer.set_margin_bottom(16)
        outer.set_margin_start(16)
        outer.set_margin_end(16)
        toolbar_view.set_content(outer)

        prefs = Adw.PreferencesGroup(title="Output")
        outer.append(prefs)

        out_row = Adw.ActionRow(title="Folder", subtitle=str(self.output_dir))
        self.out_row = out_row
        browse = Gtk.Button(label="Browse")
        browse.set_valign(Gtk.Align.CENTER)
        browse.connect("clicked", self._on_browse_output)
        out_row.add_suffix(browse)
        prefs.add(out_row)

        self.target_row = Adw.ComboRow(title="Target")
        target_model = Gtk.StringList()
        for target in TARGETS:
            target_model.append(target.label)
        self.target_row.set_model(target_model)
        self.target_row.set_selected(DEFAULT_TARGET_INDEX)
        self.target_row.connect("notify::selected", self._on_target_changed)
        prefs.add(self.target_row)

        self.preset_row = Adw.ComboRow(title="Preset")
        self.preset_row.connect("notify::selected", self._on_preset_changed)
        prefs.add(self.preset_row)

        self.tonemap_row = Adw.SwitchRow(
            title="Tone-map HDR to Rec.709",
            subtitle=TARGETS[DEFAULT_TARGET_INDEX].tonemap_subtitle,
            active=TARGETS[DEFAULT_TARGET_INDEX].tonemap_default,
        )
        prefs.add(self.tonemap_row)

        self.preset_info = Gtk.Label(wrap=True, xalign=0)
        self.preset_info.add_css_class("dim-label")
        self.preset_info.add_css_class("caption")
        self.preset_info.set_margin_start(4)
        self.preset_info.set_margin_end(4)
        outer.append(self.preset_info)

        self._reload_presets(preferred_id=DEFAULT_PRESET_ID)

        queue_group = Adw.PreferencesGroup(title="Queue")
        outer.append(queue_group)

        self.queue_summary = Gtk.Label(xalign=0, wrap=True)
        self.queue_summary.add_css_class("dim-label")
        self.queue_summary.set_visible(False)
        outer.append(self.queue_summary)

        self.drop_hint = Gtk.Label(
            label="Drop video files here, or use Add Videos",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        self.drop_hint.add_css_class("dim-label")
        self.drop_hint.set_margin_top(24)
        self.drop_hint.set_margin_bottom(24)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(220)
        scrolled.add_css_class("card")

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.set_placeholder(self.drop_hint)
        scrolled.set_child(self.list_box)
        queue_group.add(scrolled)

        self.overall = Gtk.ProgressBar()
        self.overall.set_show_text(True)
        self.overall.set_text("Idle")
        outer.append(self.overall)

        self.stats_label = Gtk.Label(xalign=0, wrap=True)
        self.stats_label.add_css_class("dim-label")
        self.stats_label.add_css_class("caption")
        self.stats_label.set_visible(False)
        outer.append(self.stats_label)

        self.status = Gtk.Label(label=self._ffmpeg_status_text(), xalign=0)
        self.status.add_css_class("dim-label")
        outer.append(self.status)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)

    def _ffmpeg_status_text(self) -> str:
        if which_ffmpeg() and which_ffprobe():
            return "Ready — convert for iPhone, DaVinci Resolve, ProRes, or the web."
        return "ffmpeg/ffprobe not found. Install ffmpeg to convert."

    def _selected_target(self) -> Target:
        idx = self.target_row.get_selected()
        if idx < 0 or idx >= len(TARGETS):
            return TARGETS[DEFAULT_TARGET_INDEX]
        return TARGETS[idx]

    def _selected_preset(self) -> Preset:
        if not self._preset_choices:
            return PRESETS[0]
        idx = self.preset_row.get_selected()
        if idx < 0 or idx >= len(self._preset_choices):
            return self._preset_choices[0]
        return self._preset_choices[idx]

    def _on_target_changed(self, _row: Adw.ComboRow, _param) -> None:
        target = self._selected_target()
        self.tonemap_row.set_subtitle(target.tonemap_subtitle)
        self.tonemap_row.set_active(target.tonemap_default)
        if self._auto_folder:
            self.output_dir = default_output_dir(target)
            self.out_row.set_subtitle(str(self.output_dir))
        preferred = "dnxhr_sq" if target.id == "resolve" else None
        if target.id == "iphone":
            preferred = "iphone_h264"
        elif target.id == "prores":
            preferred = "prores422"
        elif target.id == "web":
            preferred = "web_h264"
        self._reload_presets(preferred_id=preferred)

    def _reload_presets(self, preferred_id: str | None = None) -> None:
        target = self._selected_target()
        self._preset_choices = presets_for_target(target.id)
        model = Gtk.StringList()
        selected = 0
        for i, preset in enumerate(self._preset_choices):
            model.append(preset.label)
            if preferred_id and preset.id == preferred_id:
                selected = i
        self.preset_row.set_model(model)
        if self._preset_choices:
            self.preset_row.set_selected(selected)
        self._update_preset_info()

    def _on_preset_changed(self, _row: Adw.ComboRow, _param) -> None:
        self._update_preset_info()

    def _update_preset_info(self) -> None:
        if not self._preset_choices:
            self.preset_info.set_label("")
            return
        preset = self._selected_preset()
        self.preset_info.set_label(f"{preset.tag} — {preset.description}")

    def _update_queue_summary(self) -> None:
        if not self.jobs:
            self.queue_summary.set_visible(False)
            return
        total_size = sum(j.input_size for j in self.jobs)
        total_duration = sum(j.duration for j in self.jobs)
        count = len(self.jobs)
        self.queue_summary.set_label(
            f"{count} file{'s' if count != 1 else ''} queued · "
            f"{format_duration(total_duration)} total runtime · "
            f"{format_bytes(total_size)} total size"
        )
        self.queue_summary.set_visible(True)

    def _update_sensitivity(self) -> None:
        has_jobs = bool(self.jobs)
        can_run = has_jobs and not self._busy and which_ffmpeg() is not None
        self.convert_btn.set_sensitive(can_run)
        self.cancel_btn.set_visible(self._busy)

    def _on_browse_output(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Choose output folder")
        dialog.set_initial_folder(Gio.File.new_for_path(str(self.output_dir)))
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder:
            self.output_dir = Path(folder.get_path())
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self.out_row.set_subtitle(str(self.output_dir))
            self._auto_folder = False

    def _on_add_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Gtk.FileDialog(title="Add videos")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        video = Gtk.FileFilter()
        video.set_name("Video files")
        for ext in sorted(VIDEO_EXTENSIONS):
            video.add_suffix(ext.lstrip("."))
        filters.append(video)
        all_files = Gtk.FileFilter()
        all_files.set_name("All files")
        all_files.add_pattern("*")
        filters.append(all_files)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            files = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        if not files:
            return
        paths = []
        for i in range(files.get_n_items()):
            f = files.get_item(i)
            path = f.get_path()
            if path:
                paths.append(Path(path))
        self._add_paths(paths)

    def _on_drop(self, _target: Gtk.DropTarget, value: Gdk.FileList, _x: float, _y: float) -> bool:
        paths = []
        for f in value.get_files():
            path = f.get_path()
            if path:
                paths.append(Path(path))
        self._add_paths(paths)
        return True

    def _add_paths(self, paths: list[Path]) -> None:
        existing = {j.path.resolve() for j in self.jobs}
        for path in paths:
            if not path.is_file():
                continue
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            resolved = path.resolve()
            if resolved in existing:
                continue
            duration, width, height, codec = probe_media(path)
            try:
                input_size = path.stat().st_size
            except OSError:
                input_size = 0
            job = Job(
                path=path,
                duration=duration,
                width=width,
                height=height,
                codec=codec,
                input_size=input_size,
            )
            self.jobs.append(job)
            self._append_row(job)
            existing.add(resolved)
        self._update_sensitivity()
        self._update_queue_summary()

    def _append_row(self, job: Job) -> None:
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=job.path.name, xalign=0, hexpand=True)
        title.add_css_class("heading")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        top.append(title)

        remove = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text("Remove")
        remove.connect("clicked", lambda _b, j=job: self._remove_job(j))
        top.append(remove)
        box.append(top)

        meta_bits = []
        if job.width and job.height:
            meta_bits.append(f"{job.width}×{job.height}")
        if job.codec:
            meta_bits.append(job.codec)
        if job.duration:
            mins, secs = divmod(int(job.duration), 60)
            hrs, mins = divmod(mins, 60)
            meta_bits.append(f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}")
        if job.input_size:
            meta_bits.append(format_bytes(job.input_size))
        meta = Gtk.Label(label=" · ".join(meta_bits) or str(job.path), xalign=0)
        meta.add_css_class("dim-label")
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(meta)

        status = Gtk.Label(label=job.status, xalign=0)
        box.append(status)

        bar = Gtk.ProgressBar()
        bar.set_fraction(0)
        box.append(bar)

        row.set_child(box)
        job.row = row
        job.status_label = status
        job.progress_bar = bar
        self.list_box.append(row)

    def _remove_job(self, job: Job) -> None:
        if self._busy:
            return
        if job in self.jobs:
            self.jobs.remove(job)
        if job.row:
            self.list_box.remove(job.row)
        self._update_sensitivity()
        self._update_queue_summary()

    def _on_clear_clicked(self, _btn: Gtk.Button) -> None:
        if self._busy:
            return
        self.jobs.clear()
        while True:
            row = self.list_box.get_row_at_index(0)
            if row is None:
                break
            self.list_box.remove(row)
        self.overall.set_fraction(0)
        self.overall.set_text("Idle")
        self.stats_label.set_visible(False)
        self._update_sensitivity()
        self._update_queue_summary()

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _on_convert_clicked(self, _btn: Gtk.Button) -> None:
        if self._busy or not self.jobs:
            return
        if not which_ffmpeg():
            self._toast("ffmpeg is required")
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._cancel.clear()
        self._busy = True
        self._update_sensitivity()
        preset = self._selected_preset()
        tonemap = self.tonemap_row.get_active()
        jobs = list(self.jobs)
        self.stats_label.set_visible(True)
        self._worker = threading.Thread(
            target=self._run_jobs,
            args=(jobs, preset, tonemap, self.output_dir),
            daemon=True,
        )
        self._worker.start()

    def _run_jobs(self, jobs: list[Job], preset: Preset, tonemap: bool, out_dir: Path) -> None:
        total = len(jobs)
        run_start = time.monotonic()
        total_input_bytes = sum(j.input_size for j in jobs)
        completed_output_bytes = 0
        done = 0
        failed = 0

        for index, job in enumerate(jobs):
            if self._cancel.is_set():
                GLib.idle_add(self._set_job_status, job, "Cancelled", job.progress)
                break

            dest = output_path_for(job.path, out_dir, preset)
            job_start = time.monotonic()
            GLib.idle_add(self._set_job_status, job, f"Converting → {dest.name}", 0.0)
            GLib.idle_add(
                self._update_live_stats,
                index,
                total,
                job.path.name,
                0.0,
                completed_output_bytes,
                run_start,
            )

            cmd = build_ffmpeg_cmd(job.path, dest, preset, tonemap)
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                failed += 1
                GLib.idle_add(self._set_job_status, job, f"Failed: {exc}", 0.0)
                continue

            assert self._proc.stdout is not None
            assert self._proc.stderr is not None
            stderr_chunks: list[str] = []

            def read_stderr() -> None:
                assert self._proc and self._proc.stderr
                for line in self._proc.stderr:
                    stderr_chunks.append(line)

            err_thread = threading.Thread(target=read_stderr, daemon=True)
            err_thread.start()

            snapshot: dict[str, str] = {}
            for raw_line in self._proc.stdout:
                line = raw_line.strip()
                if not line or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                snapshot[key] = value
                if key == "progress":
                    frac = frac_from_snapshot(snapshot, job.duration)
                    if frac is not None:
                        job.progress = frac
                    fps = _safe_float(snapshot.get("fps"))
                    speed = _safe_float(snapshot.get("speed"))
                    out_bytes = _safe_int(snapshot.get("total_size"))
                    eta = None
                    if frac and frac > 0.01:
                        eta = (time.monotonic() - job_start) * (1 / frac - 1)
                    GLib.idle_add(self._update_job_progress, job, job.progress, fps, speed, eta)
                    GLib.idle_add(
                        self._update_live_stats,
                        index,
                        total,
                        job.path.name,
                        job.progress,
                        completed_output_bytes + out_bytes,
                        run_start,
                    )
                    snapshot = {}
                if self._cancel.is_set():
                    try:
                        os.killpg(self._proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    break

            code = self._proc.wait()
            err_thread.join(timeout=2)
            self._proc = None

            if self._cancel.is_set():
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                GLib.idle_add(self._set_job_status, job, "Cancelled", job.progress)
                break

            if code == 0 and dest.exists():
                output_size = dest.stat().st_size
                job.output_size = output_size
                completed_output_bytes += output_size
                done += 1
                elapsed_job = time.monotonic() - job_start
                speed = job.duration / elapsed_job if elapsed_job > 0 else 0.0
                ratio_pct = (output_size / job.input_size * 100) if job.input_size else 0.0
                status = (
                    f"Done — {dest.name} · {format_bytes(output_size)} "
                    f"({ratio_pct:.0f}% of source) · {format_duration(elapsed_job)} "
                    f"· {speed:.2g}x realtime"
                )
                GLib.idle_add(self._set_job_status, job, status, 1.0)
            else:
                failed += 1
                err = "".join(stderr_chunks[-20:]).strip() or f"ffmpeg exited {code}"
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                GLib.idle_add(self._set_job_status, job, f"Failed: {err.splitlines()[-1]}", 0.0)

        def finish() -> None:
            self._busy = False
            elapsed_total = time.monotonic() - run_start
            if self._cancel.is_set():
                self.overall.set_text("Cancelled")
                self.stats_label.set_label(
                    f"Stopped after {format_duration(elapsed_total)} · "
                    f"{done} of {total} converted"
                )
            else:
                self.overall.set_fraction(1.0)
                self.overall.set_text("Finished")
                delta = completed_output_bytes - total_input_bytes
                sign = "+" if delta >= 0 else "-"
                summary = (
                    f"{done} of {total} converted"
                    + (f", {failed} failed" if failed else "")
                    + f" · {format_bytes(total_input_bytes)} → {format_bytes(completed_output_bytes)}"
                    f" ({sign}{format_bytes(abs(delta))}) · {format_duration(elapsed_total)} elapsed"
                )
                self.stats_label.set_label(summary)
            self._update_sensitivity()

        GLib.idle_add(finish)

    def _set_job_status(self, job: Job, status: str, progress: float) -> None:
        job.status = status
        job.progress = progress
        if job.status_label:
            job.status_label.set_label(status)
        if job.progress_bar:
            job.progress_bar.set_fraction(progress)

    def _update_job_progress(
        self, job: Job, frac: float, fps: float, speed: float, eta: float | None
    ) -> None:
        bits = [f"Converting… {int(frac * 100)}%"]
        if speed > 0:
            bits.append(f"{speed:.2g}x")
        if fps > 0:
            bits.append(f"{fps:.0f}fps")
        if eta is not None and eta > 0:
            bits.append(f"ETA {format_duration(eta)}")
        self._set_job_status(job, " · ".join(bits), frac)

    def _update_live_stats(
        self,
        job_index: int,
        total_jobs: int,
        name: str,
        frac: float,
        written_bytes: int,
        run_start: float,
    ) -> None:
        overall_frac = (job_index + frac) / total_jobs
        self.overall.set_fraction(overall_frac)
        self.overall.set_text(f"File {job_index + 1} of {total_jobs}: {name} ({int(frac * 100)}%)")
        elapsed = time.monotonic() - run_start
        bits = [f"{format_bytes(written_bytes)} written", f"elapsed {format_duration(elapsed)}"]
        if overall_frac > 0.02:
            eta = elapsed * (1 / overall_frac - 1)
            bits.append(f"ETA ~{format_duration(eta)}")
        self.stats_label.set_label(" · ".join(bits))

    def _toast(self, message: str) -> None:
        self.status.set_label(message)


class VidPrepApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window: VidPrepWindow | None = None

    def do_activate(self) -> None:  # noqa: N802
        if not self.window:
            self.window = VidPrepWindow(self)
        self.window.present()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:  # noqa: N802
        self.activate()
        assert self.window
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        self.window._add_paths(paths)


def main() -> int:
    Adw.init()
    app = VidPrepApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
