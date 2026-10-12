#!/usr/bin/env python3
"""Resolve Prep — convert videos to DaVinci Resolve-friendly DNxHR intermediates."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

APP_ID = "dev.bobo.resolve-prep"
APP_NAME = "Resolve Prep"
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

PROFILES = [
    ("dnxhr_lb", "DNxHR LB — smallest, draft / proxy"),
    ("dnxhr_sq", "DNxHR SQ — recommended edit quality"),
    ("dnxhr_hq", "DNxHR HQ — higher quality, larger files"),
    ("dnxhr_hqx", "DNxHR HQX — 10-bit, largest"),
]

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


@dataclass
class Job:
    path: Path
    duration: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
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


def output_path_for(source: Path, out_dir: Path) -> Path:
    stem = re.sub(r"[^\w.\-]+", "_", source.stem).strip("_") or "clip"
    candidate = out_dir / f"{stem}_dnxhr.mov"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = out_dir / f"{stem}_dnxhr_{n}.mov"
        if not candidate.exists():
            return candidate
        n += 1


def build_ffmpeg_cmd(
    source: Path,
    dest: Path,
    profile: str,
    tonemap: bool,
) -> list[str]:
    ffmpeg = which_ffmpeg()
    assert ffmpeg

    pix_fmt = "yuv422p10le" if profile == "dnxhr_hqx" else "yuv422p"
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
            f"zscale=t=bt709:m=bt709:r=tv,format={pix_fmt}"
        )
    else:
        filters.append(f"format={pix_fmt}")

    cmd.extend(["-vf", ",".join(filters)])
    cmd.extend(
        [
            "-c:v",
            "dnxhd",
            "-profile:v",
            profile,
            "-c:a",
            "pcm_s16le",
            str(dest),
        ]
    )
    return cmd


def parse_progress_line(line: str, duration: float) -> float | None:
    if line.startswith("out_time_ms="):
        try:
            ms = int(line.split("=", 1)[1])
            if duration > 0:
                return min(max(ms / (duration * 1_000_000), 0.0), 1.0)
        except ValueError:
            return None
    if line.startswith("out_time="):
        match = TIME_RE.search(line.replace("out_time=", "time="))
        if match and duration > 0:
            h, m, s = match.groups()
            seconds = int(h) * 3600 + int(m) * 60 + float(s)
            return min(max(seconds / duration, 0.0), 1.0)
    return None


class ResolvePrepWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=APP_NAME, default_width=780, default_height=620)
        self.set_icon_name("applications-multimedia")

        self.jobs: list[Job] = []
        self._worker: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._busy = False

        downloads = Path.home() / "Downloads" / "davinci"
        downloads.mkdir(parents=True, exist_ok=True)
        self.output_dir = downloads

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

        self.profile_row = Adw.ComboRow(title="Codec profile")
        model = Gtk.StringList()
        for _, label in PROFILES:
            model.append(label)
        self.profile_row.set_model(model)
        self.profile_row.set_selected(1)  # SQ
        prefs.add(self.profile_row)

        self.tonemap_row = Adw.SwitchRow(
            title="Tone-map HDR to Rec.709",
            subtitle="Recommended for iPhone / HLG / Dolby Vision clips on Resolve Free",
            active=True,
        )
        prefs.add(self.tonemap_row)

        queue_group = Adw.PreferencesGroup(title="Queue")
        outer.append(queue_group)

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

        self.status = Gtk.Label(label=self._ffmpeg_status_text(), xalign=0)
        self.status.add_css_class("dim-label")
        outer.append(self.status)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)

    def _ffmpeg_status_text(self) -> str:
        if which_ffmpeg() and which_ffprobe():
            return "Ready — converts to DNxHR in .mov with PCM audio for DaVinci Resolve (Linux)."
        return "ffmpeg/ffprobe not found. Install ffmpeg to convert."

    def _selected_profile(self) -> str:
        idx = self.profile_row.get_selected()
        return PROFILES[idx][0]

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
            job = Job(path=path, duration=duration, width=width, height=height, codec=codec)
            self.jobs.append(job)
            self._append_row(job)
            existing.add(resolved)
        self._update_sensitivity()

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
        self._update_sensitivity()

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
        profile = self._selected_profile()
        tonemap = self.tonemap_row.get_active()
        jobs = list(self.jobs)
        self._worker = threading.Thread(
            target=self._run_jobs,
            args=(jobs, profile, tonemap, self.output_dir),
            daemon=True,
        )
        self._worker.start()

    def _run_jobs(self, jobs: list[Job], profile: str, tonemap: bool, out_dir: Path) -> None:
        total = len(jobs)
        for index, job in enumerate(jobs):
            if self._cancel.is_set():
                GLib.idle_add(self._set_job_status, job, "Cancelled", job.progress)
                break

            dest = output_path_for(job.path, out_dir)
            GLib.idle_add(self._set_job_status, job, f"Converting → {dest.name}", 0.0)
            GLib.idle_add(self.overall.set_text, f"File {index + 1} of {total}: {job.path.name}")

            cmd = build_ffmpeg_cmd(job.path, dest, profile, tonemap)
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
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

            for line in self._proc.stdout:
                line = line.strip()
                frac = parse_progress_line(line, job.duration)
                if frac is not None:
                    overall = (index + frac) / total
                    GLib.idle_add(self._set_job_status, job, f"Converting… {int(frac * 100)}%", frac)
                    GLib.idle_add(self.overall.set_fraction, overall)
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
                size_gb = dest.stat().st_size / (1024**3)
                GLib.idle_add(
                    self._set_job_status,
                    job,
                    f"Done — {dest.name} ({size_gb:.1f} GB)",
                    1.0,
                )
            else:
                err = "".join(stderr_chunks[-20:]).strip() or f"ffmpeg exited {code}"
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                GLib.idle_add(self._set_job_status, job, f"Failed: {err.splitlines()[-1]}", 0.0)

        def finish() -> None:
            self._busy = False
            if self._cancel.is_set():
                self.overall.set_text("Cancelled")
            else:
                self.overall.set_fraction(1.0)
                self.overall.set_text("Finished")
            self._update_sensitivity()

        GLib.idle_add(finish)

    def _set_job_status(self, job: Job, status: str, progress: float) -> None:
        job.status = status
        job.progress = progress
        if job.status_label:
            job.status_label.set_label(status)
        if job.progress_bar:
            job.progress_bar.set_fraction(progress)

    def _toast(self, message: str) -> None:
        self.status.set_label(message)


class ResolvePrepApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window: ResolvePrepWindow | None = None

    def do_activate(self) -> None:  # noqa: N802
        if not self.window:
            self.window = ResolvePrepWindow(self)
        self.window.present()

    def do_open(self, files: list[Gio.File], _n_files: int, _hint: str) -> None:  # noqa: N802
        self.activate()
        assert self.window
        paths = [Path(f.get_path()) for f in files if f.get_path()]
        self.window._add_paths(paths)


def main() -> int:
    Adw.init()
    app = ResolvePrepApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
