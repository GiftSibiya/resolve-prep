from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

ROTATE_OPTIONS = (0, 90, 180, 270)
SPEED_OPTIONS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 4.0)


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
    in_s: float | None = None
    out_s: float | None = None
    mute: bool = False
    rotate: int = 0
    fade_in: float = 0.0
    fade_out: float = 0.0
    speed: float = 1.0
    row: Gtk.ListBoxRow | None = field(default=None, repr=False)
    progress_bar: Gtk.ProgressBar | None = field(default=None, repr=False)
    status_label: Gtk.Label | None = field(default=None, repr=False)
    marks_label: Gtk.Label | None = field(default=None, repr=False)

    def clear_marks(self) -> None:
        self.in_s = None
        self.out_s = None

    def effective_in(self) -> float:
        return 0.0 if self.in_s is None else self.in_s

    def effective_out(self) -> float:
        if self.out_s is None:
            return max(self.duration, 0.0)
        return self.out_s

    def has_trim(self) -> bool:
        return self.in_s is not None or self.out_s is not None

    def output_size_wh(self) -> tuple[int, int]:
        w, h = self.width or 1280, self.height or 720
        if self.rotate in (90, 270):
            return h, w
        return w, h

    def normalize_marks(self) -> str | None:
        """Clamp/validate marks. Returns an error message, or None if OK."""
        if self.rotate not in ROTATE_OPTIONS:
            self.rotate = 0
        if self.speed <= 0:
            self.speed = 1.0
        self.fade_in = max(0.0, float(self.fade_in or 0.0))
        self.fade_out = max(0.0, float(self.fade_out or 0.0))

        duration = max(self.duration, 0.0)
        if self.in_s is not None:
            if self.in_s < 0:
                self.in_s = 0.0
            if duration and self.in_s > duration:
                self.in_s = duration
        if self.out_s is not None:
            if self.out_s < 0:
                self.out_s = 0.0
            if duration and self.out_s > duration:
                self.out_s = duration

        in_s = self.effective_in()
        out_s = self.effective_out()
        if self.has_trim() and out_s <= in_s:
            return "Out must be after In"
        clip_len = max(0.0, out_s - in_s) if (self.has_trim() or duration) else 0.0
        if clip_len and self.fade_in + self.fade_out > clip_len:
            return "Fade in + fade out longer than clip"
        return None

    def marks_summary(self) -> str:
        bits: list[str] = []
        if self.in_s is not None or self.out_s is not None:
            from vidprep.util import format_timecode

            bits.append(
                f"{format_timecode(self.effective_in())} → {format_timecode(self.effective_out())}"
            )
        if self.mute:
            bits.append("mute")
        if self.rotate:
            bits.append(f"rot {self.rotate}°")
        if abs(self.speed - 1.0) > 1e-3:
            bits.append(f"{self.speed:g}x")
        if self.fade_in:
            bits.append(f"fi {self.fade_in:g}s")
        if self.fade_out:
            bits.append(f"fo {self.fade_out:g}s")
        return " · ".join(bits)
