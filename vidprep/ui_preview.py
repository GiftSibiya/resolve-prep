from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from vidprep.ffmpeg_ops import build_proxy_cmd, preview_proxy_path, which_ffmpeg
from vidprep.preview_diag import PreviewErrorLog
from vidprep.util import format_timecode

# Codecs that often fail Gtk/GStreamer preview on Linux; these go straight to an ffmpeg proxy.
HARD_PREVIEW_CODECS = frozenset(
    {
        "apv",
        "dnxhd",
        "dnxhr",
        "prores",
        "cfhd",
        "rawvideo",
        "v210",
        "r210",
    }
)


class PreviewPanel(Gtk.Box):
    """Gtk.Video preview with scrubber and playhead helpers for Mark In/Out."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        self._media: Gtk.MediaFile | None = None
        self._ffprobe_duration = 0.0
        self._scrubbing = False
        self._tick_id = 0
        self._path: Path | None = None
        self._codec = ""
        self._available = False
        self._using_proxy = False
        self._proxy_proc: subprocess.Popen | None = None
        self._load_gen = 0
        self._errors = PreviewErrorLog()
        self._opened: Path | None = None

        frame = Gtk.Frame()
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        frame.set_size_request(-1, 220)

        overlay = Gtk.Overlay()
        frame.set_child(overlay)

        self.video = Gtk.Video()
        # GTK 4.24+ dropped Gtk.Video:controls; we use our own transport bar.
        if hasattr(self.video, "set_controls"):
            self.video.set_controls(False)
        self.video.set_autoplay(False)
        self.video.set_loop(False)
        self.video.set_hexpand(True)
        self.video.set_vexpand(True)
        overlay.set_child(self.video)

        self.fallback = Gtk.Label(
            label="Select a clip to preview",
            wrap=True,
            justify=Gtk.Justification.CENTER,
        )
        self.fallback.add_css_class("dim-label")
        self.fallback.set_margin_top(24)
        self.fallback.set_margin_bottom(24)
        self.fallback.set_margin_start(16)
        self.fallback.set_margin_end(16)
        self.fallback.set_valign(Gtk.Align.CENTER)
        self.fallback.set_halign(Gtk.Align.CENTER)
        overlay.add_overlay(self.fallback)

        self.append(frame)

        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.append(controls)

        self.play_btn = Gtk.Button.new_from_icon_name("media-playback-start-symbolic")
        self.play_btn.set_tooltip_text("Play / Pause")
        self.play_btn.connect("clicked", self._on_play_clicked)
        controls.append(self.play_btn)

        self.position_label = Gtk.Label(label="0:00.000")
        self.position_label.set_width_chars(10)
        controls.append(self.position_label)

        self.scrub = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.0, 1.0, 0.001)
        self.scrub.set_draw_value(False)
        self.scrub.set_hexpand(True)
        self.scrub.connect("change-value", self._on_scrub_change)
        click = Gtk.GestureClick()
        click.connect("pressed", lambda *_: self._on_scrub_press(None))
        click.connect("released", lambda *_: self._on_scrub_release(None))
        self.scrub.add_controller(click)
        controls.append(self.scrub)

        self.duration_label = Gtk.Label(label="0:00.000")
        self.duration_label.set_width_chars(10)
        controls.append(self.duration_label)

        marks = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.append(marks)

        self.mark_in_btn = Gtk.Button(label="Mark In")
        self.mark_in_btn.add_css_class("suggested-action")
        marks.append(self.mark_in_btn)

        self.mark_out_btn = Gtk.Button(label="Mark Out")
        self.mark_out_btn.add_css_class("suggested-action")
        marks.append(self.mark_out_btn)

        self.status_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.status_label.add_css_class("dim-label")
        self.status_label.add_css_class("caption")
        self.append(self.status_label)

        self.append(self._build_notice())

        self._set_controls_sensitive(False)

    def connect_mark_in(self, callback) -> None:
        self.mark_in_btn.connect("clicked", callback)

    def connect_mark_out(self, callback) -> None:
        self.mark_out_btn.connect("clicked", callback)

    def clear(self) -> None:
        self._load_gen += 1
        self._cancel_proxy()
        self._using_proxy = False
        self._errors = PreviewErrorLog()
        self.notice.set_visible(False)
        self._stop_tick()
        self._disconnect_media()
        self.video.set_media_stream(None)
        self._media = None
        self._path = None
        self._codec = ""
        self._ffprobe_duration = 0.0
        self._available = False
        self.scrub.set_value(0)
        self.position_label.set_label("0:00.000")
        self.duration_label.set_label("0:00.000")
        self.fallback.set_label("Select a clip to preview")
        self.fallback.set_visible(True)
        self.status_label.set_label("")
        self.play_btn.set_icon_name("media-playback-start-symbolic")
        self._set_controls_sensitive(False)

    def load(self, path: Path, *, duration: float = 0.0, codec: str = "") -> None:
        self.clear()
        self._path = path
        self._ffprobe_duration = max(duration, 0.0)
        self._codec = (codec or "").lower()
        self._errors = PreviewErrorLog(path=path, codec=self._codec)
        self.duration_label.set_label(format_timecode(self._ffprobe_duration))
        self.scrub.set_range(0.0, max(self._ffprobe_duration, 0.001))
        self.scrub.set_value(0.0)

        self._set_controls_sensitive(True)  # marks via timecode fields work before decode
        self.play_btn.set_sensitive(False)

        proxy = self._proxy_path(path)
        self._errors.proxy = proxy
        if self._codec in HARD_PREVIEW_CODECS:
            self._errors.add(
                "VidPrep",
                f"{self._codec} is not reliably decodable by GStreamer; previewing an ffmpeg proxy.",
            )
        if proxy is not None and proxy.exists():
            self._using_proxy = True
            self._open_media(proxy, "Loading preview proxy…")
        elif self._codec in HARD_PREVIEW_CODECS and proxy is not None:
            self._start_proxy(proxy)
        else:
            self._open_media(path, "Loading preview…")

    def _proxy_path(self, path: Path) -> Path | None:
        if not which_ffmpeg():
            return None
        try:
            return preview_proxy_path(path)
        except OSError:
            return None

    def _open_media(self, path: Path, message: str) -> None:
        self.fallback.set_label(message)
        self.fallback.set_visible(True)
        try:
            media = Gtk.MediaFile.new_for_file(Gio.File.new_for_path(str(path)))
        except Exception as exc:  # noqa: BLE001
            self._preview_failed(f"Could not open preview: {exc}")
            return

        self._opened = path
        self._media = media
        media.connect("notify::prepared", self._on_prepared)
        media.connect("notify::error", self._on_error)
        media.connect("notify::timestamp", self._on_timestamp)
        media.connect("notify::duration", self._on_duration)
        media.connect("notify::playing", self._on_playing)
        media.connect("notify::ended", self._on_ended)
        self.video.set_media_stream(media)
        # Give GStreamer a moment; prepared/error callbacks drive UI.
        GLib.timeout_add_seconds(3, self._check_still_unprepared, media)

    def _preview_failed(self, message: str) -> None:
        """GStreamer could not play the source: transcode a proxy once, else give up."""
        which = "proxy" if self._using_proxy else "original"
        self._errors.add(f"GStreamer ({which}: {self._opened})", message)
        if self._using_proxy or self._proxy_proc is not None or self._path is None:
            if self._proxy_proc is None:
                self._show_unavailable(message)
            return
        proxy = self._proxy_path(self._path)
        if proxy is None:
            self._show_unavailable(message)
            return
        self._start_proxy(proxy)

    def _start_proxy(self, proxy: Path) -> None:
        assert self._path is not None
        self._using_proxy = True
        self._stop_tick()
        self.video.set_media_stream(None)
        self._media = None
        self._available = False
        self.play_btn.set_sensitive(False)
        label = self._codec.upper() if self._codec else "this codec"
        self.fallback.set_label(f"Building preview proxy for {label}…")
        self.fallback.set_visible(True)
        self.status_label.set_label(
            "GStreamer can't decode this clip; transcoding a low-res preview with ffmpeg "
            "(cached for next time). Timecode fields work meanwhile."
        )

        proxy.parent.mkdir(parents=True, exist_ok=True)
        tmp = proxy.with_name(proxy.stem + ".part.mp4")
        try:
            proc = subprocess.Popen(
                build_proxy_cmd(self._path, tmp),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, RuntimeError) as exc:
            self._errors.add("ffmpeg (proxy)", str(exc))
            self._show_unavailable(f"Could not build preview proxy: {exc}")
            return
        self._proxy_proc = proc
        gen = self._load_gen

        def wait() -> None:
            _, err = proc.communicate()
            ok = proc.returncode == 0
            if ok:
                tmp.replace(proxy)
            else:
                tmp.unlink(missing_ok=True)
            GLib.idle_add(self._proxy_done, gen, proc, proxy, ok, (err or "").strip())

        threading.Thread(target=wait, daemon=True).start()

    def _proxy_done(self, gen: int, proc: subprocess.Popen, proxy: Path, ok: bool, err: str) -> bool:
        if self._proxy_proc is proc:
            self._proxy_proc = None
        if gen != self._load_gen:
            return False
        if ok:
            self._open_media(proxy, "Loading preview proxy…")
        else:
            last = err.splitlines()[-1] if err else "ffmpeg failed"
            self._errors.add(f"ffmpeg (proxy, exit {proc.returncode})", err or "ffmpeg failed")
            self._show_unavailable(f"Could not build preview proxy: {last}")
        return False

    def _cancel_proxy(self) -> None:
        proc, self._proxy_proc = self._proxy_proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    # --- friendly warning card -------------------------------------------------

    def _build_notice(self) -> Gtk.Widget:
        self.notice = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.notice.add_css_class("card")
        for side in ("top", "bottom", "start", "end"):
            getattr(self.notice, f"set_margin_{side}")(0)
        self.notice.set_visible(False)

        self.notice_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self.notice_icon.set_pixel_size(24)
        self.notice_icon.set_valign(Gtk.Align.START)
        self.notice_icon.set_margin_top(12)
        self.notice_icon.set_margin_start(12)
        self.notice.append(self.notice_icon)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.set_hexpand(True)
        body.set_margin_top(10)
        body.set_margin_bottom(10)
        self.notice.append(body)

        self.notice_title = Gtk.Label(xalign=0, wrap=True)
        self.notice_title.add_css_class("heading")
        body.append(self.notice_title)

        self.notice_body = Gtk.Label(xalign=0, wrap=True)
        self.notice_body.add_css_class("caption")
        body.append(self.notice_body)

        self.notice_cmd_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.notice_cmd = Gtk.Label(xalign=0, selectable=True, wrap=True)
        self.notice_cmd.add_css_class("monospace")
        self.notice_cmd.set_hexpand(True)
        self.notice_cmd_row.append(self.notice_cmd)
        copy_cmd = Gtk.Button.new_from_icon_name("edit-copy-symbolic")
        copy_cmd.set_tooltip_text("Copy install command")
        copy_cmd.add_css_class("flat")
        copy_cmd.set_valign(Gtk.Align.CENTER)
        copy_cmd.connect("clicked", lambda b: self._copy(self.notice_cmd.get_label(), b))
        self.notice_cmd_row.append(copy_cmd)
        body.append(self.notice_cmd_row)

        details = Gtk.Button(label="Details…")
        details.set_valign(Gtk.Align.START)
        details.set_margin_top(10)
        details.set_margin_end(10)
        details.connect("clicked", self._on_details_clicked)
        self.notice.append(details)
        return self.notice

    def _show_notice(self, *, error: bool) -> None:
        title, body, command = self._errors.friendly()
        if not error:
            label = self._codec.upper() or "This clip"
            title = "Showing a low-res preview copy"
            body = (
                f"GStreamer can't play {label} directly, so VidPrep made a preview copy with "
                "ffmpeg. Trims and conversions still use the original file."
            )
            if command:
                body += " Installing the package below may let it play directly."
        for cls in ("warning", "accent"):
            self.notice_icon.remove_css_class(cls)
        self.notice_icon.set_from_icon_name(
            "dialog-warning-symbolic" if error else "dialog-information-symbolic"
        )
        self.notice_icon.add_css_class("warning" if error else "accent")
        self.notice_title.set_label(title)
        self.notice_body.set_label(body)
        self.notice_cmd.set_label(command or "")
        self.notice_cmd_row.set_visible(bool(command))
        self.notice.set_visible(True)
        if error:
            self.fallback.set_label(title)

    def _on_details_clicked(self, _btn: Gtk.Button) -> None:
        title, _, _ = self._errors.friendly()
        report = self._errors.report()

        buffer = Gtk.TextBuffer()
        buffer.set_text(report)
        view = Gtk.TextView(buffer=buffer, editable=False, monospace=True, cursor_visible=False)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        for side in ("top", "bottom", "left", "right"):
            getattr(view, f"set_{side}_margin")(8)
        scroller = Gtk.ScrolledWindow(child=view)
        scroller.set_min_content_height(260)
        scroller.set_min_content_width(520)
        scroller.add_css_class("card")

        copy = Gtk.Button(label="Copy to Clipboard")
        copy.set_halign(Gtk.Align.START)
        copy.connect("clicked", lambda b: self._copy(report, b))

        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        extra.append(scroller)
        extra.append(copy)

        dialog = Adw.AlertDialog(
            heading=title,
            body="Full error details. Copy these when reporting a problem.",
        )
        dialog.set_extra_child(extra)
        dialog.add_response("close", "Close")
        dialog.set_default_response("close")
        dialog.set_close_response("close")
        dialog.present(self)

    def _copy(self, text: str, button: Gtk.Button | None = None) -> None:
        self.get_clipboard().set(text)
        if button is None:
            return
        if button.get_label():
            old = button.get_label()
            button.set_label("Copied!")
            GLib.timeout_add(1500, lambda: (button.set_label(old), False)[1])
        else:
            old_icon = button.get_icon_name()
            button.set_icon_name("object-select-symbolic")
            GLib.timeout_add(1500, lambda: (button.set_icon_name(old_icon), False)[1])

    def get_position_seconds(self) -> float:
        if self._media is not None and self._available:
            # GTK MediaStream timestamps are microseconds.
            ts = self._media.get_timestamp()
            if ts >= 0:
                return ts / 1_000_000.0
        # Fall back to scrubber (works without decoder).
        return float(self.scrub.get_value())

    def seek_seconds(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        if self._ffprobe_duration:
            seconds = min(seconds, self._ffprobe_duration)
        self.scrub.set_value(seconds)
        self.position_label.set_label(format_timecode(seconds))
        if self._media is not None and self._available and self._media.is_seekable():
            self._media.seek(int(seconds * 1_000_000))

    def _set_controls_sensitive(self, enabled: bool) -> None:
        self.play_btn.set_sensitive(enabled)
        self.scrub.set_sensitive(enabled)
        self.mark_in_btn.set_sensitive(enabled)
        self.mark_out_btn.set_sensitive(enabled)

    def _disconnect_media(self) -> None:
        # MediaFile is replaced wholesale; GObject disconnect-by-data not needed if we drop refs.
        pass

    def _show_unavailable(self, message: str) -> None:
        self._available = False
        self.fallback.set_label(message)
        self.fallback.set_visible(True)
        self.status_label.set_label(
            "Preview unavailable — set In/Out with the timecode fields (ffprobe duration)."
        )
        self._set_controls_sensitive(True)
        self.play_btn.set_sensitive(False)
        self._show_notice(error=True)

    def _check_still_unprepared(self, media: Gtk.MediaFile) -> bool:
        if self._media is None or media is not self._media:
            return False
        if self._media.is_prepared():
            return False
        if self._media.get_error() is not None:
            return False
        if not self._available:
            self._preview_failed(
                "Preview did not become ready (missing decoder or unsupported codec). "
                "Marks still work via In/Out fields."
            )
        return False

    def _on_prepared(self, media: Gtk.MediaFile, _pspec) -> None:
        if media is not self._media or not media.is_prepared():
            return
        err = media.get_error()
        if err is not None:
            self._preview_failed(err.message)
            return
        self._available = True
        self.fallback.set_visible(False)
        dur_us = media.get_duration()
        if dur_us > 0:
            self._ffprobe_duration = dur_us / 1_000_000.0
            self.duration_label.set_label(format_timecode(self._ffprobe_duration))
            self.scrub.set_range(0.0, max(self._ffprobe_duration, 0.001))
        self._set_controls_sensitive(True)
        suffix = " (low-res proxy)" if self._using_proxy else ""
        self.status_label.set_label(f"Preview ready{suffix} — scrub or play, then Mark In / Mark Out.")
        if self._using_proxy and self._errors:
            self._show_notice(error=False)
        self._start_tick()

    def _on_error(self, media: Gtk.MediaFile, _pspec) -> None:
        if media is not self._media:
            return
        err = media.get_error()
        if err is not None:
            self._preview_failed(err.message)

    def _on_timestamp(self, media: Gtk.MediaFile, _pspec) -> None:
        if self._scrubbing:
            return
        ts = media.get_timestamp()
        if ts < 0:
            return
        seconds = ts / 1_000_000.0
        self.position_label.set_label(format_timecode(seconds))
        self.scrub.set_value(seconds)

    def _on_duration(self, media: Gtk.MediaFile, _pspec) -> None:
        dur_us = media.get_duration()
        if dur_us > 0:
            self._ffprobe_duration = dur_us / 1_000_000.0
            self.duration_label.set_label(format_timecode(self._ffprobe_duration))
            self.scrub.set_range(0.0, max(self._ffprobe_duration, 0.001))

    def _on_playing(self, media: Gtk.MediaFile, _pspec) -> None:
        if media.get_playing():
            self.play_btn.set_icon_name("media-playback-pause-symbolic")
            self._start_tick()
        else:
            self.play_btn.set_icon_name("media-playback-start-symbolic")

    def _on_ended(self, media: Gtk.MediaFile, _pspec) -> None:
        if media.get_ended():
            media.pause()
            self.play_btn.set_icon_name("media-playback-start-symbolic")

    def _on_play_clicked(self, _btn: Gtk.Button) -> None:
        if self._media is None or not self._available:
            return
        if self._media.get_playing():
            self._media.pause()
        else:
            self._media.play()

    def _on_scrub_press(self, _btn) -> None:
        self._scrubbing = True

    def _on_scrub_release(self, _btn) -> None:
        self._scrubbing = False
        self.seek_seconds(float(self.scrub.get_value()))

    def _on_scrub_change(self, _scale: Gtk.Scale, _scroll, value: float) -> bool:
        # While dragging, update label; seek on release (and when not playing via change-value end).
        self.position_label.set_label(format_timecode(value))
        if not self._scrubbing and self._media is not None and self._available:
            self.seek_seconds(value)
        return False

    def _start_tick(self) -> None:
        if self._tick_id:
            return
        self._tick_id = GLib.timeout_add(100, self._tick)

    def _stop_tick(self) -> None:
        if self._tick_id:
            GLib.source_remove(self._tick_id)
            self._tick_id = 0

    def _tick(self) -> bool:
        if self._media is None:
            self._tick_id = 0
            return False
        if self._scrubbing:
            return True
        if self._available and self._media.get_playing():
            ts = self._media.get_timestamp()
            if ts >= 0:
                seconds = ts / 1_000_000.0
                self.position_label.set_label(format_timecode(seconds))
                self.scrub.set_value(seconds)
        return True
