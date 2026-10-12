from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from vidprep.ffmpeg_ops import (
    EditOptions,
    NormalizeSpec,
    build_concat_cmd,
    build_ffmpeg_cmd,
    output_path_for,
)
from vidprep.models import Job
from vidprep.presets import Preset
from vidprep.util import format_bytes, format_duration


def edit_options_for(job: Job) -> EditOptions:
    job.normalize_marks()
    return EditOptions(
        in_s=job.in_s,
        out_s=job.out_s,
        source_duration=job.duration,
        mute=job.mute,
        rotate=job.rotate,
        fade_in=job.fade_in,
        fade_out=job.fade_out,
        speed=job.speed,
    )


TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def safe_float(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value.rstrip("x"))
    except ValueError:
        return 0.0


def safe_int(value: str | None) -> int:
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


class ConvertWorker:
    """Runs ffmpeg conversions on a background thread with UI callbacks."""

    def __init__(
        self,
        *,
        on_job_status: Callable[[Job, str, float], None],
        on_job_progress: Callable[[Job, float, float, float, float | None], None],
        on_live_stats: Callable[[int, int, str, float, int, float], None],
        on_finished: Callable[[bool, int, int, int, int, int, float], None],
        schedule: Callable[..., None],
    ) -> None:
        self._on_job_status = on_job_status
        self._on_job_progress = on_job_progress
        self._on_live_stats = on_live_stats
        self._on_finished = on_finished
        self._schedule = schedule
        self._proc: subprocess.Popen | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def start(
        self,
        jobs: list[Job],
        preset: Preset,
        tonemap: bool,
        out_dir: Path,
        *,
        join: bool = False,
    ) -> None:
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(jobs, preset, tonemap, out_dir, join),
            daemon=True,
        )
        self._thread.start()

    def _run_cmd(
        self,
        cmd: list[str],
        *,
        progress_duration: float,
        job: Job | None,
        job_index: int,
        total_jobs: int,
        label: str,
        completed_output_bytes: int,
        run_start: float,
        job_start: float,
    ) -> tuple[int, list[str], int]:
        """Run one ffmpeg command; returns (exit_code, stderr_tail, bytes_written_hint)."""
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return 1, [str(exc)], 0

        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        stderr_chunks: list[str] = []

        def read_stderr() -> None:
            assert self._proc and self._proc.stderr
            for line in self._proc.stderr:
                stderr_chunks.append(line)

        err_thread = threading.Thread(target=read_stderr, daemon=True)
        err_thread.start()

        out_bytes = 0
        snapshot: dict[str, str] = {}
        for raw_line in self._proc.stdout:
            line = raw_line.strip()
            if not line or "=" not in line:
                continue
            key, _, value = line.partition("=")
            snapshot[key] = value
            if key == "progress":
                frac = frac_from_snapshot(snapshot, progress_duration) or 0.0
                if job is not None:
                    job.progress = frac
                fps = safe_float(snapshot.get("fps"))
                speed = safe_float(snapshot.get("speed"))
                out_bytes = safe_int(snapshot.get("total_size"))
                eta = None
                if frac and frac > 0.01:
                    eta = (time.monotonic() - job_start) * (1 / frac - 1)
                if job is not None:
                    self._schedule(
                        self._on_job_progress, job, job.progress, fps, speed, eta
                    )
                self._schedule(
                    self._on_live_stats,
                    job_index,
                    total_jobs,
                    label,
                    frac,
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
        return code, stderr_chunks[-20:], out_bytes

    def _run(
        self,
        jobs: list[Job],
        preset: Preset,
        tonemap: bool,
        out_dir: Path,
        join: bool,
    ) -> None:
        if join:
            self._run_join(jobs, preset, tonemap, out_dir)
        else:
            self._run_batch(jobs, preset, tonemap, out_dir)

    def _run_batch(
        self,
        jobs: list[Job],
        preset: Preset,
        tonemap: bool,
        out_dir: Path,
    ) -> None:
        total = len(jobs)
        run_start = time.monotonic()
        total_input_bytes = sum(j.input_size for j in jobs)
        completed_output_bytes = 0
        done = 0
        failed = 0

        for index, job in enumerate(jobs):
            if self._cancel.is_set():
                self._schedule(self._on_job_status, job, "Cancelled", job.progress)
                break

            if job.normalize_marks():
                failed += 1
                self._schedule(self._on_job_status, job, "Failed: invalid edit options", 0.0)
                continue

            edit = edit_options_for(job)
            dest = output_path_for(job.path, out_dir, preset)
            job_start = time.monotonic()
            progress_duration = edit.output_duration() or job.duration
            self._schedule(self._on_job_status, job, f"Converting → {dest.name}", 0.0)
            self._schedule(
                self._on_live_stats,
                index,
                total,
                job.path.name,
                0.0,
                completed_output_bytes,
                run_start,
            )

            cmd = build_ffmpeg_cmd(job.path, dest, preset, tonemap, edit)
            code, err_chunks, _ = self._run_cmd(
                cmd,
                progress_duration=progress_duration,
                job=job,
                job_index=index,
                total_jobs=total,
                label=job.path.name,
                completed_output_bytes=completed_output_bytes,
                run_start=run_start,
                job_start=job_start,
            )

            if self._cancel.is_set():
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                self._schedule(self._on_job_status, job, "Cancelled", job.progress)
                break

            if code == 0 and dest.exists():
                output_size = dest.stat().st_size
                job.output_size = output_size
                completed_output_bytes += output_size
                done += 1
                elapsed_job = time.monotonic() - job_start
                media_len = progress_duration if progress_duration > 0 else job.duration
                speed = media_len / elapsed_job if elapsed_job > 0 else 0.0
                ratio_pct = (output_size / job.input_size * 100) if job.input_size else 0.0
                status = (
                    f"Done — {dest.name} · {format_bytes(output_size)} "
                    f"({ratio_pct:.0f}% of source) · {format_duration(elapsed_job)} "
                    f"· {speed:.2g}x realtime"
                )
                self._schedule(self._on_job_status, job, status, 1.0)
            else:
                failed += 1
                err = "".join(err_chunks).strip() or f"ffmpeg exited {code}"
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                self._schedule(
                    self._on_job_status, job, f"Failed: {err.splitlines()[-1]}", 0.0
                )

        elapsed_total = time.monotonic() - run_start
        self._schedule(
            self._on_finished,
            self._cancel.is_set(),
            done,
            failed,
            total,
            total_input_bytes,
            completed_output_bytes,
            elapsed_total,
        )

    def _run_join(
        self,
        jobs: list[Job],
        preset: Preset,
        tonemap: bool,
        out_dir: Path,
    ) -> None:
        run_start = time.monotonic()
        total_input_bytes = sum(j.input_size for j in jobs)
        completed_output_bytes = 0
        done = 0
        failed = 0
        total_steps = len(jobs) + 1  # segments + concat

        if len(jobs) < 2:
            if jobs:
                self._schedule(
                    self._on_job_status,
                    jobs[0],
                    "Failed: join needs at least 2 clips",
                    0.0,
                )
            self._schedule(
                self._on_finished,
                False,
                0,
                1,
                len(jobs),
                total_input_bytes,
                0,
                time.monotonic() - run_start,
            )
            return

        for job in jobs:
            if job.normalize_marks():
                self._schedule(
                    self._on_job_status, job, "Failed: invalid edit options", 0.0
                )
                self._schedule(
                    self._on_finished,
                    False,
                    0,
                    1,
                    len(jobs),
                    total_input_bytes,
                    0,
                    time.monotonic() - run_start,
                )
                return

        first = jobs[0]
        norm_w, norm_h = first.output_size_wh()
        if norm_w <= 0 or norm_h <= 0:
            norm_w, norm_h = 1280, 720
        normalize = NormalizeSpec(width=norm_w, height=norm_h, fps=30.0)

        tmp_dir = Path(tempfile.mkdtemp(prefix="vidprep-join-"))
        segments: list[Path] = []
        try:
            for index, job in enumerate(jobs):
                if self._cancel.is_set():
                    self._schedule(self._on_job_status, job, "Cancelled", job.progress)
                    break

                edit = edit_options_for(job)
                seg = tmp_dir / f"seg_{index:03d}{preset.container}"
                job_start = time.monotonic()
                progress_duration = edit.output_duration() or job.duration or 1.0
                self._schedule(
                    self._on_job_status,
                    job,
                    f"Join segment {index + 1}/{len(jobs)} → {seg.name}",
                    0.0,
                )
                self._schedule(
                    self._on_live_stats,
                    index,
                    total_steps,
                    job.path.name,
                    0.0,
                    completed_output_bytes,
                    run_start,
                )

                cmd = build_ffmpeg_cmd(
                    job.path,
                    seg,
                    preset,
                    tonemap,
                    edit,
                    normalize=normalize,
                    join_segment=True,
                )
                code, err_chunks, _ = self._run_cmd(
                    cmd,
                    progress_duration=progress_duration,
                    job=job,
                    job_index=index,
                    total_jobs=total_steps,
                    label=job.path.name,
                    completed_output_bytes=completed_output_bytes,
                    run_start=run_start,
                    job_start=job_start,
                )

                if self._cancel.is_set():
                    self._schedule(self._on_job_status, job, "Cancelled", job.progress)
                    break

                if code != 0 or not seg.exists():
                    failed += 1
                    err = "".join(err_chunks).strip() or f"ffmpeg exited {code}"
                    self._schedule(
                        self._on_job_status,
                        job,
                        f"Failed: {err.splitlines()[-1]}",
                        0.0,
                    )
                    self._schedule(
                        self._on_finished,
                        False,
                        done,
                        failed,
                        len(jobs),
                        total_input_bytes,
                        completed_output_bytes,
                        time.monotonic() - run_start,
                    )
                    return

                segments.append(seg)
                self._schedule(self._on_job_status, job, f"Segment ready · {seg.name}", 1.0)

            if self._cancel.is_set() or len(segments) != len(jobs):
                self._schedule(
                    self._on_finished,
                    True,
                    done,
                    failed,
                    len(jobs),
                    total_input_bytes,
                    completed_output_bytes,
                    time.monotonic() - run_start,
                )
                return

            dest = output_path_for(first.path, out_dir, preset, stem_suffix="joined")
            list_file = tmp_dir / "concat.txt"
            concat_cmd = build_concat_cmd(segments, dest, list_file, preset)
            join_start = time.monotonic()
            # Rough progress duration = sum of segment outputs
            join_dur = sum(
                (edit_options_for(j).output_duration() or j.duration or 0) for j in jobs
            ) or 1.0
            self._schedule(
                self._on_live_stats,
                len(jobs),
                total_steps,
                dest.name,
                0.0,
                completed_output_bytes,
                run_start,
            )
            for job in jobs:
                self._schedule(self._on_job_status, job, f"Concatenating → {dest.name}", 0.95)

            code, err_chunks, _ = self._run_cmd(
                concat_cmd,
                progress_duration=join_dur,
                job=jobs[-1],
                job_index=len(jobs),
                total_jobs=total_steps,
                label=dest.name,
                completed_output_bytes=completed_output_bytes,
                run_start=run_start,
                job_start=join_start,
            )

            if self._cancel.is_set():
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                self._schedule(
                    self._on_finished,
                    True,
                    done,
                    failed,
                    len(jobs),
                    total_input_bytes,
                    completed_output_bytes,
                    time.monotonic() - run_start,
                )
                return

            if code == 0 and dest.exists():
                output_size = dest.stat().st_size
                completed_output_bytes = output_size
                done = len(jobs)
                for job in jobs:
                    job.output_size = output_size
                    self._schedule(
                        self._on_job_status,
                        job,
                        f"Joined — {dest.name} · {format_bytes(output_size)}",
                        1.0,
                    )
            else:
                failed = 1
                err = "".join(err_chunks).strip() or f"ffmpeg exited {code}"
                if dest.exists():
                    try:
                        dest.unlink()
                    except OSError:
                        pass
                for job in jobs:
                    self._schedule(
                        self._on_job_status,
                        job,
                        f"Join failed: {err.splitlines()[-1]}",
                        0.0,
                    )

            self._schedule(
                self._on_finished,
                False,
                done,
                failed,
                len(jobs),
                total_input_bytes,
                completed_output_bytes,
                time.monotonic() - run_start,
            )
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
