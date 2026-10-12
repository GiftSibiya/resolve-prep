from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vidprep.presets import Preset


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


def preview_proxy_path(source: Path) -> Path:
    """Cache path for a low-res H.264 preview proxy, keyed by path, size and mtime."""
    st = source.stat()
    key = f"{source.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:16]
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "vidprep" / "proxies" / f"{source.stem}-{digest}.mp4"


def build_proxy_cmd(source: Path, dest: Path) -> list[str]:
    """Transcode to something GStreamer can always play; short GOP keeps scrubbing snappy."""
    ffmpeg = which_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-dn",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-vf", "scale=-2:'min(540,ih)'",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-g", "12",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(dest),
    ]


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


def output_path_for(source: Path, out_dir: Path, preset: Preset, *, stem_suffix: str = "") -> Path:
    stem = re.sub(r"[^\w.\-]+", "_", source.stem).strip("_") or "clip"
    if stem_suffix:
        stem = f"{stem}_{stem_suffix}"
    candidate = out_dir / f"{stem}_{preset.id}{preset.container}"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = out_dir / f"{stem}_{preset.id}_{n}{preset.container}"
        if not candidate.exists():
            return candidate
        n += 1


@dataclass(frozen=True)
class NormalizeSpec:
    width: int
    height: int
    fps: float = 30.0


@dataclass(frozen=True)
class EditOptions:
    """Per-clip edit state applied during Convert."""

    in_s: float | None = None
    out_s: float | None = None
    source_duration: float = 0.0
    mute: bool = False
    rotate: int = 0
    fade_in: float = 0.0
    fade_out: float = 0.0
    speed: float = 1.0

    def has_trim(self) -> bool:
        return self.in_s is not None or self.out_s is not None

    def effective_in(self) -> float:
        return 0.0 if self.in_s is None else max(0.0, self.in_s)

    def effective_out(self) -> float | None:
        if self.out_s is not None:
            return max(0.0, self.out_s)
        if self.source_duration > 0:
            return self.source_duration
        return None

    def trimmed_duration(self) -> float | None:
        """Duration after trim, before speed change."""
        if not self.has_trim():
            return self.source_duration if self.source_duration > 0 else None
        start = self.effective_in()
        end = self.effective_out()
        if end is None:
            if self.source_duration > 0:
                return max(0.0, self.source_duration - start)
            return None
        return max(0.0, end - start)

    def output_duration(self) -> float | None:
        trim = self.trimmed_duration()
        if trim is None:
            return None
        speed = self.speed if self.speed > 0 else 1.0
        return trim / speed


def _rotate_filters(rotate: int) -> list[str]:
    if rotate == 90:
        return ["transpose=1"]
    if rotate == 180:
        return ["transpose=1", "transpose=1"]
    if rotate == 270:
        return ["transpose=2"]
    return []


def _atempo_filters(speed: float) -> list[str]:
    if abs(speed - 1.0) < 1e-3:
        return []
    filters: list[str] = []
    remaining = speed
    while remaining > 2.0 + 1e-6:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-6:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.5f}")
    return filters


def _color_filters(preset: Preset, tonemap: bool, source: Path) -> list[str]:
    if tonemap and is_hdr_stream(source):
        return [
            "zscale=t=linear:npl=100,format=gbrpf32le,"
            "zscale=p=bt709,tonemap=hable:desat=0,"
            f"zscale=t=bt709:m=bt709:r=tv,format={preset.pix_fmt}"
        ]
    return [f"format={preset.pix_fmt}"]


def _normalize_filters(spec: NormalizeSpec) -> list[str]:
    w, h = spec.width, spec.height
    return [
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={spec.fps:.3f}"
    ]


def build_ffmpeg_cmd(
    source: Path,
    dest: Path,
    preset: Preset,
    tonemap: bool,
    edit: EditOptions | None = None,
    *,
    normalize: NormalizeSpec | None = None,
    join_segment: bool = False,
) -> list[str]:
    ffmpeg = which_ffmpeg()
    assert ffmpeg

    edit = edit or EditOptions()
    speed = edit.speed if edit.speed > 0 else 1.0
    needs_hdr_tonemap = bool(tonemap and is_hdr_stream(source))
    needs_effects = (
        edit.rotate != 0
        or needs_hdr_tonemap
        or abs(speed - 1.0) > 1e-3
        or edit.fade_in > 0
        or edit.fade_out > 0
        or normalize is not None
        or join_segment
    )
    use_input_seek = edit.has_trim() and not needs_effects

    cmd: list[str] = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
    ]

    in_s = edit.effective_in()
    out_s = edit.effective_out()
    trim_dur = edit.trimmed_duration()

    if use_input_seek and in_s > 0:
        cmd.extend(["-ss", f"{in_s:.3f}"])

    cmd.extend(["-i", str(source)])

    silent_audio = bool(join_segment and edit.mute)
    if silent_audio:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
            ]
        )

    if use_input_seek and trim_dur is not None and edit.has_trim():
        if edit.out_s is not None or edit.source_duration > 0:
            cmd.extend(["-t", f"{trim_dur:.3f}"])

    cmd.extend(["-map", "0:v:0"])
    if edit.mute and not join_segment:
        cmd.append("-an")
    elif silent_audio:
        cmd.extend(["-map", "1:a:0", "-shortest"])
    else:
        cmd.extend(["-map", "0:a:0?"])

    vf: list[str] = []
    af: list[str] = []

    if edit.has_trim() and not use_input_seek:
        end = out_s if out_s is not None else in_s + (trim_dur or 0)
        if out_s is None and trim_dur is None:
            vf.append(f"trim=start={in_s:.3f},setpts=PTS-STARTPTS")
            if not edit.mute:
                af.append(f"atrim=start={in_s:.3f},asetpts=PTS-STARTPTS")
        else:
            vf.append(f"trim=start={in_s:.3f}:end={end:.3f},setpts=PTS-STARTPTS")
            if not edit.mute:
                af.append(f"atrim=start={in_s:.3f}:end={end:.3f},asetpts=PTS-STARTPTS")

    vf.extend(_rotate_filters(edit.rotate))

    # Fades operate on post-trim, pre-speed timeline.
    seg_len = trim_dur
    if edit.fade_in > 0:
        vf.append(f"fade=t=in:st=0:d={edit.fade_in:.3f}")
        if not edit.mute and not silent_audio:
            af.append(f"afade=t=in:st=0:d={edit.fade_in:.3f}")
    if edit.fade_out > 0 and seg_len and seg_len > edit.fade_out:
        st = max(0.0, seg_len - edit.fade_out)
        vf.append(f"fade=t=out:st={st:.3f}:d={edit.fade_out:.3f}")
        if not edit.mute and not silent_audio:
            af.append(f"afade=t=out:st={st:.3f}:d={edit.fade_out:.3f}")

    if abs(speed - 1.0) > 1e-3:
        vf.append(f"setpts=PTS/{speed:.5f}")
        if not edit.mute and not silent_audio:
            af.extend(_atempo_filters(speed))

    if normalize is not None:
        vf.extend(_normalize_filters(normalize))

    vf.extend(_color_filters(preset, tonemap, source))

    cmd.extend(["-vf", ",".join(vf)])
    if af:
        cmd.extend(["-af", ",".join(af)])

    cmd.extend(preset.video_args)
    if not edit.mute or join_segment:
        cmd.extend(preset.audio_args)
    cmd.extend(preset.extra_args)
    cmd.append(str(dest))
    return cmd


def build_concat_cmd(segment_paths: list[Path], dest: Path, list_file: Path, preset: Preset) -> list[str]:
    """Concatenate already-normalized segments (re-encode for safety)."""
    ffmpeg = which_ffmpeg()
    assert ffmpeg
    lines = []
    for path in segment_paths:
        # concat demuxer needs escaped single quotes in paths
        escaped = str(path).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-nostats",
        "-progress",
        "pipe:1",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
    ]
    cmd.extend(preset.video_args)
    cmd.extend(preset.audio_args)
    cmd.extend(preset.extra_args)
    cmd.append(str(dest))
    return cmd
