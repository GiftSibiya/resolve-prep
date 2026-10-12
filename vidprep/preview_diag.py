"""Turn raw GStreamer/ffmpeg preview failures into friendly messages plus a copyable report."""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from vidprep.ffmpeg_ops import which_ffmpeg

# (element, Arch package) GTK's preview needs for a given container/codec.
_CONTAINER_ELEMENTS = {
    ".mp4": ("qtdemux", "gst-plugins-good"),
    ".mov": ("qtdemux", "gst-plugins-good"),
    ".m4v": ("qtdemux", "gst-plugins-good"),
    ".mkv": ("matroskademux", "gst-plugins-good"),
    ".webm": ("matroskademux", "gst-plugins-good"),
    ".avi": ("avidemux", "gst-plugins-good"),
    ".mxf": ("mxfdemux", "gst-plugins-bad"),
    ".ts": ("tsdemux", "gst-plugins-bad"),
    ".mts": ("tsdemux", "gst-plugins-bad"),
    ".m2ts": ("tsdemux", "gst-plugins-bad"),
}
# Always needed: the proxy is H.264/AAC in MP4, plus the GTK4 video sink install.sh recommends.
_BASE_ELEMENTS = (
    ("qtdemux", "gst-plugins-good"),
    ("avdec_h264", "gst-libav"),
    ("gtk4paintablesink", "gst-plugin-gtk4"),
)
# Codecs no GStreamer package can decode yet; installing plug-ins won't help.
_NO_GST_DECODER = frozenset({"apv"})
# Suggested when GStreamer says a plug-in is missing but we can't tell which.
_GENERIC_PACKAGES = ("gst-plugins-good", "gst-plugins-bad", "gst-plugins-ugly", "gst-libav")


def install_command(packages) -> str:
    return "sudo pacman -S --needed " + " ".join(sorted(set(packages)))


def _gst():
    try:
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if not Gst.is_initialized():
            Gst.init(None)
        return Gst
    except (ImportError, ValueError):
        return None


def missing_plugins(path: Path | None) -> list[tuple[str, str]]:
    """Return (element, package) pairs the preview needs but the GStreamer registry lacks."""
    gst = _gst()
    if gst is None:
        return [("gstreamer", "gstreamer")]
    wanted: list[tuple[str, str]] = []
    if path is not None and path.suffix.lower() in _CONTAINER_ELEMENTS:
        wanted.append(_CONTAINER_ELEMENTS[path.suffix.lower()])
    wanted.extend(_BASE_ELEMENTS)
    seen: set[str] = set()
    missing = []
    for element, package in wanted:
        if element in seen:
            continue
        seen.add(element)
        if gst.ElementFactory.find(element) is None:
            missing.append((element, package))
    return missing


@dataclass
class PreviewErrorLog:
    """Everything that went wrong while trying to preview one clip."""

    path: Path | None = None
    codec: str = ""
    proxy: Path | None = None
    entries: list[tuple[str, str]] = field(default_factory=list)

    def add(self, source: str, text: str) -> None:
        text = (text or "").strip()
        if text and (source, text) not in self.entries:
            self.entries.append((source, text))

    def __bool__(self) -> bool:
        return bool(self.entries)

    def friendly(self) -> tuple[str, str, str | None]:
        """(title, body, install command or None) a non-expert can act on."""
        missing = missing_plugins(self.path)
        raw = "\n".join(text for _, text in self.entries).lower()
        codec = self.codec.upper() or "this video format"
        tail = "You can still trim with the In/Out timecode fields."

        if missing:
            pkgs = sorted({pkg for _, pkg in missing})
            names = pkgs[0] if len(pkgs) == 1 else ", ".join(pkgs[:-1]) + " and " + pkgs[-1]
            it = "it" if len(pkgs) == 1 else "them"
            return (
                "Preview needs a GStreamer plug-in",
                f"Video preview needs {names}, which {'is' if len(pkgs) == 1 else 'are'} not installed. "
                f"Install {it} with the command below, then reopen VidPrep. {tail}",
                install_command(pkg for _, pkg in missing),
            )
        if not which_ffmpeg():
            return (
                "Preview unavailable",
                f"GStreamer can't play {codec}, and ffmpeg isn't installed to make a preview copy. "
                f"Install ffmpeg to enable it. {tail}",
                install_command(["ffmpeg"]),
            )
        if any(source.startswith("ffmpeg") for source, _ in self.entries):
            return (
                "Couldn't make a preview copy",
                f"GStreamer can't play {codec} directly, and ffmpeg failed while making a "
                f"low-res preview copy. The file may be damaged or still being written. {tail}",
                None,
            )
        if self.codec in _NO_GST_DECODER:
            return (
                "This format can't be previewed",
                f"No GStreamer plug-in can decode {codec} yet, so VidPrep tried a preview copy "
                f"instead. {tail}",
                None,
            )
        if "missing a plug-in" in raw or "no suitable plugins" in raw:
            return (
                "This format can't be previewed",
                f"No installed GStreamer decoder handles {codec}. Installing the full plug-in "
                f"set below may help. {tail}",
                install_command(_GENERIC_PACKAGES),
            )
        if "did not become ready" in raw:
            return (
                "Preview is taking too long",
                f"GStreamer didn't finish opening this clip. {tail}",
                None,
            )
        return ("Preview unavailable", f"Something went wrong opening this clip. {tail}", None)

    def report(self) -> str:
        lines = ["VidPrep preview error report", ""]
        lines.append(f"File:      {self.path or '—'}")
        lines.append(f"Codec:     {self.codec or 'unknown'}")
        if self.proxy is not None:
            state = "exists" if self.proxy.exists() else "not built"
            lines.append(f"Proxy:     {self.proxy} ({state})")
        lines.append("")
        gst = _gst()
        lines.append(f"GStreamer: {gst.version_string() if gst else 'not available (PyGObject Gst missing)'}")
        lines.append(
            f"GTK:       {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}"
        )
        lines.append(f"ffmpeg:    {_ffmpeg_version()}")
        lines.append(f"System:    {platform.platform()}")
        missing = missing_plugins(self.path)
        if missing:
            listed = ", ".join(f"{el} ({pkg})" for el, pkg in missing)
            lines.append(f"Missing:   {listed}")
            lines.append(f"Fix:       {install_command(pkg for _, pkg in missing)}")
        lines.append("")
        for source, text in self.entries:
            lines.append(f"--- {source} ---")
            lines.append(text)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def _ffmpeg_version() -> str:
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        return "not found"
    try:
        out = subprocess.run(
            [ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ffmpeg
    return out.splitlines()[0] if out else ffmpeg
