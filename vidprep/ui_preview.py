from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402

from vidprep.util import format_timecode

# Codecs that often fail Gtk/GStreamer preview on Linux (marks still work via ffprobe).
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

        self._set_controls_sensitive(False)

    def connect_mark_in(self, callback) -> None:
        self.mark_in_btn.connect("clicked", callback)

    def connect_mark_out(self, callback) -> None:
        self.mark_out_btn.connect("clicked", callback)

    def clear(self) -> None:
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
        self.duration_label.set_label(format_timecode(self._ffprobe_duration))
        self.scrub.set_range(0.0, max(self._ffprobe_duration, 0.001))
        self.scrub.set_value(0.0)

        hard = self._codec in HARD_PREVIEW_CODECS
        if hard:
            self.fallback.set_label(
                f"Preview often unavailable for {self._codec or 'this codec'}. "
                "Use In/Out fields below — duration comes from ffprobe."
            )
            self.fallback.set_visible(True)
            self.status_label.set_label("Preview fallback — marks still work via timecode fields.")
            self._set_controls_sensitive(True)  # allow marking via entries; play may fail
            # Still try to open — some systems may decode it.
        else:
            self.fallback.set_label("Loading preview…")
            self.fallback.set_visible(True)
            self.status_label.set_label("")

        try:
            media = Gtk.MediaFile.new_for_file(Gio.File.new_for_path(str(path)))
        except Exception as exc:  # noqa: BLE001
            self._show_unavailable(f"Could not open preview: {exc}")
            return

        self._media = media
        media.connect("notify::prepared", self._on_prepared)
        media.connect("notify::error", self._on_error)
        media.connect("notify::timestamp", self._on_timestamp)
        media.connect("notify::duration", self._on_duration)
        media.connect("notify::playing", self._on_playing)
        media.connect("notify::ended", self._on_ended)
        self.video.set_media_stream(media)
        # Give GStreamer a moment; prepared/error callbacks drive UI.
        GLib.timeout_add_seconds(3, self._check_still_unprepared)

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

    def _check_still_unprepared(self) -> bool:
        if self._media is None:
            return False
        if self._media.get_prepared():
            return False
        if self._media.get_error() is not None:
            return False
        if not self._available:
            self._show_unavailable(
                "Preview did not become ready (missing decoder or unsupported codec). "
                "Marks still work via In/Out fields."
            )
        return False

    def _on_prepared(self, media: Gtk.MediaFile, _pspec) -> None:
        if not media.get_prepared():
            return
        err = media.get_error()
        if err is not None:
            self._show_unavailable(err.message)
            return
        self._available = True
        self.fallback.set_visible(False)
        dur_us = media.get_duration()
        if dur_us > 0:
            self._ffprobe_duration = dur_us / 1_000_000.0
            self.duration_label.set_label(format_timecode(self._ffprobe_duration))
            self.scrub.set_range(0.0, max(self._ffprobe_duration, 0.001))
        self._set_controls_sensitive(True)
        self.status_label.set_label("Preview ready — scrub or play, then Mark In / Mark Out.")
        self._start_tick()

    def _on_error(self, media: Gtk.MediaFile, _pspec) -> None:
        err = media.get_error()
        if err is not None:
            self._show_unavailable(err.message)

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
