from __future__ import annotations

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


def format_timecode(seconds: float) -> str:
    """Format seconds as H:MM:SS.mmm (or M:SS.mmm when under one hour)."""
    seconds = max(0.0, float(seconds))
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    h, rem = divmod(whole, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}.{millis:03d}"
    return f"{m}:{s:02d}.{millis:03d}"


def parse_timecode(text: str) -> float | None:
    """Parse H:MM:SS(.mmm), M:SS(.mmm), or plain seconds."""
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        if ":" not in text:
            return max(0.0, float(text))
        parts = text.split(":")
        if len(parts) == 2:
            m, s = parts
            return max(0.0, int(m) * 60 + float(s))
        if len(parts) == 3:
            h, m, s = parts
            return max(0.0, int(h) * 3600 + int(m) * 60 + float(s))
    except ValueError:
        return None
    return None
