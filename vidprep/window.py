from __future__ import annotations

import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from vidprep import APP_NAME
from vidprep.ffmpeg_ops import probe_media, which_ffmpeg, which_ffprobe
from vidprep.models import ROTATE_OPTIONS, SPEED_OPTIONS, Job
from vidprep.presets import (
    DEFAULT_PRESET_ID,
    DEFAULT_TARGET_INDEX,
    PRESETS,
    TARGETS,
    Preset,
    Target,
    default_output_dir,
    presets_for_target,
)
from vidprep.ui_preview import PreviewPanel
from vidprep.util import (
    VIDEO_EXTENSIONS,
    format_bytes,
    format_duration,
    format_timecode,
    parse_timecode,
)
from vidprep.worker import ConvertWorker


class VidPrepWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=APP_NAME, default_width=980, default_height=700)
        self.set_icon_name("applications-multimedia")

        self.jobs: list[Job] = []
        self._busy = False
        self._preset_choices: list[Preset] = []
        self._auto_folder = True
        self._selected_job: Job | None = None
        self._syncing_edit_ui = False
        self._worker = ConvertWorker(
            on_job_status=self._set_job_status,
            on_job_progress=self._update_job_progress,
            on_live_stats=self._update_live_stats,
            on_finished=self._on_convert_finished,
            schedule=GLib.idle_add,
        )

        self.output_dir = default_output_dir(TARGETS[DEFAULT_TARGET_INDEX])

        self._build_ui()
        self._update_sensitivity()
        self._load_edit_panel(None)

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

        # Filled after prefs group exists — join toggle lives in Output prefs.

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

        self.join_row = Adw.SwitchRow(
            title="Join queue into one file",
            subtitle="Timeline order = queue order. Each clip keeps its marks; segments are normalized then concatenated.",
            active=False,
        )
        prefs.add(self.join_row)

        self.preset_info = Gtk.Label(wrap=True, xalign=0)
        self.preset_info.add_css_class("dim-label")
        self.preset_info.add_css_class("caption")
        self.preset_info.set_margin_start(4)
        self.preset_info.set_margin_end(4)
        outer.append(self.preset_info)

        self._reload_presets(preferred_id=DEFAULT_PRESET_ID)

        self.queue_summary = Gtk.Label(xalign=0, wrap=True)
        self.queue_summary.add_css_class("dim-label")
        self.queue_summary.set_visible(False)
        outer.append(self.queue_summary)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_vexpand(True)
        paned.set_wide_handle(True)
        outer.append(paned)

        # Left: queue
        queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        queue_label = Gtk.Label(label="Queue", xalign=0)
        queue_label.add_css_class("heading")
        queue_box.append(queue_label)

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
        scrolled.set_min_content_width(280)
        scrolled.set_min_content_height(280)
        scrolled.add_css_class("card")

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.list_box.set_placeholder(self.drop_hint)
        self.list_box.connect("row-selected", self._on_row_selected)
        scrolled.set_child(self.list_box)
        queue_box.append(scrolled)

        order_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.move_up_btn = Gtk.Button(label="Move up")
        self.move_up_btn.connect("clicked", lambda *_: self._move_selected(-1))
        self.move_down_btn = Gtk.Button(label="Move down")
        self.move_down_btn.connect("clicked", lambda *_: self._move_selected(1))
        order_box.append(self.move_up_btn)
        order_box.append(self.move_down_btn)
        queue_box.append(order_box)
        paned.set_start_child(queue_box)

        # Right: edit panel
        edit_scrolled = Gtk.ScrolledWindow()
        edit_scrolled.set_vexpand(True)
        edit_scrolled.set_min_content_width(360)
        edit_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        edit_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        edit_inner.set_margin_start(8)
        edit_scrolled.set_child(edit_inner)
        paned.set_end_child(edit_scrolled)

        edit_heading = Gtk.Label(label="Clip edit", xalign=0)
        edit_heading.add_css_class("heading")
        edit_inner.append(edit_heading)

        self.edit_clip_label = Gtk.Label(label="No clip selected", xalign=0, wrap=True)
        self.edit_clip_label.add_css_class("dim-label")
        edit_inner.append(self.edit_clip_label)

        self.edit_meta_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.edit_meta_label.add_css_class("dim-label")
        self.edit_meta_label.add_css_class("caption")
        edit_inner.append(self.edit_meta_label)

        self.preview = PreviewPanel()
        self.preview.connect_mark_in(self._on_mark_in)
        self.preview.connect_mark_out(self._on_mark_out)
        edit_inner.append(self.preview)

        edit_group = Adw.PreferencesGroup(title="Marks")
        edit_inner.append(edit_group)

        in_row = Adw.ActionRow(title="In", subtitle="Start of keep range (blank = start)")
        self.in_entry = Gtk.Entry(placeholder_text="0:00.000", width_chars=12)
        self.in_entry.set_valign(Gtk.Align.CENTER)
        self.in_entry.connect("changed", self._on_edit_fields_changed)
        self.in_entry.connect("activate", self._on_edit_fields_changed)
        in_row.add_suffix(self.in_entry)
        edit_group.add(in_row)

        out_row = Adw.ActionRow(title="Out", subtitle="End of keep range (blank = end)")
        self.out_entry = Gtk.Entry(placeholder_text="end", width_chars=12)
        self.out_entry.set_valign(Gtk.Align.CENTER)
        self.out_entry.connect("changed", self._on_edit_fields_changed)
        self.out_entry.connect("activate", self._on_edit_fields_changed)
        out_row.add_suffix(self.out_entry)
        edit_group.add(out_row)

        clear_marks = Gtk.Button(label="Clear marks")
        clear_marks.set_halign(Gtk.Align.START)
        clear_marks.connect("clicked", self._on_clear_marks)
        edit_inner.append(clear_marks)

        opts = Adw.PreferencesGroup(title="Options")
        edit_inner.append(opts)

        self.mute_row = Adw.SwitchRow(title="Mute audio", subtitle="Drop audio on export")
        self.mute_row.connect("notify::active", self._on_mute_changed)
        opts.add(self.mute_row)

        self.rotate_row = Adw.ComboRow(title="Rotate")
        rotate_model = Gtk.StringList()
        for deg in ROTATE_OPTIONS:
            rotate_model.append("None" if deg == 0 else f"{deg}°")
        self.rotate_row.set_model(rotate_model)
        self.rotate_row.set_selected(0)
        self.rotate_row.connect("notify::selected", self._on_rotate_changed)
        opts.add(self.rotate_row)

        self.speed_row = Adw.ComboRow(title="Speed")
        speed_model = Gtk.StringList()
        for spd in SPEED_OPTIONS:
            speed_model.append("1× (normal)" if abs(spd - 1.0) < 1e-9 else f"{spd:g}×")
        self.speed_row.set_model(speed_model)
        self.speed_row.set_selected(SPEED_OPTIONS.index(1.0))
        self.speed_row.connect("notify::selected", self._on_speed_changed)
        opts.add(self.speed_row)

        self.fade_in_row = Adw.SpinRow(
            title="Fade in",
            subtitle="Seconds (0 = none)",
            adjustment=Gtk.Adjustment(
                value=0, lower=0, upper=30, step_increment=0.1, page_increment=1
            ),
            digits=1,
        )
        self.fade_in_row.connect("notify::value", self._on_fade_changed)
        opts.add(self.fade_in_row)

        self.fade_out_row = Adw.SpinRow(
            title="Fade out",
            subtitle="Seconds (0 = none)",
            adjustment=Gtk.Adjustment(
                value=0, lower=0, upper=30, step_increment=0.1, page_increment=1
            ),
            digits=1,
        )
        self.fade_out_row.connect("notify::value", self._on_fade_changed)
        opts.add(self.fade_out_row)

        self.edit_error_label = Gtk.Label(label="", xalign=0, wrap=True)
        self.edit_error_label.add_css_class("error")
        self.edit_error_label.set_visible(False)
        edit_inner.append(self.edit_error_label)

        self.edit_note = Gtk.Label(
            label=(
                "Scrub or Mark In/Out, set mute/rotate/speed/fades per clip. "
                "Reorder the queue for timeline order; enable Join to export one concatenated file."
            ),
            wrap=True,
            xalign=0,
        )
        self.edit_note.add_css_class("dim-label")
        self.edit_note.add_css_class("caption")
        edit_inner.append(self.edit_note)

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

        GLib.idle_add(lambda: paned.set_position(340) or False)

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
        editing = self._selected_job is not None and not self._busy
        self.in_entry.set_sensitive(editing)
        self.out_entry.set_sensitive(editing)
        self.mute_row.set_sensitive(editing)
        self.rotate_row.set_sensitive(editing)
        self.speed_row.set_sensitive(editing)
        self.fade_in_row.set_sensitive(editing)
        self.fade_out_row.set_sensitive(editing)
        idx = self.jobs.index(self._selected_job) if self._selected_job in self.jobs else -1
        self.move_up_btn.set_sensitive(editing and idx > 0)
        self.move_down_btn.set_sensitive(editing and 0 <= idx < len(self.jobs) - 1)
        self.join_row.set_sensitive(not self._busy)

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
        first_new: Job | None = None
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
            if first_new is None:
                first_new = job
        self._update_sensitivity()
        self._update_queue_summary()
        if first_new and first_new.row:
            self.list_box.select_row(first_new.row)

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
            meta_bits.append(format_timecode(job.duration))
        if job.input_size:
            meta_bits.append(format_bytes(job.input_size))
        meta = Gtk.Label(label=" · ".join(meta_bits) or str(job.path), xalign=0)
        meta.add_css_class("dim-label")
        meta.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(meta)

        marks = Gtk.Label(label="", xalign=0)
        marks.add_css_class("caption")
        marks.add_css_class("dim-label")
        marks.set_ellipsize(Pango.EllipsizeMode.END)
        marks.set_visible(False)
        box.append(marks)

        status = Gtk.Label(label=job.status, xalign=0)
        box.append(status)

        bar = Gtk.ProgressBar()
        bar.set_fraction(0)
        box.append(bar)

        row.set_child(box)
        job.row = row
        job.status_label = status
        job.progress_bar = bar
        job.marks_label = marks
        self.list_box.append(row)
        self._refresh_job_marks_label(job)

    def _refresh_job_marks_label(self, job: Job) -> None:
        if not job.marks_label:
            return
        summary = job.marks_summary()
        job.marks_label.set_label(summary)
        job.marks_label.set_visible(bool(summary))

    def _job_for_row(self, row: Gtk.ListBoxRow | None) -> Job | None:
        if row is None:
            return None
        for job in self.jobs:
            if job.row is row:
                return job
        return None

    def _on_row_selected(self, _box: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        self._commit_edit_panel()
        self._load_edit_panel(self._job_for_row(row))

    def _load_edit_panel(self, job: Job | None) -> None:
        self._selected_job = job
        self._syncing_edit_ui = True
        try:
            if job is None:
                self.edit_clip_label.set_label("No clip selected")
                self.edit_meta_label.set_label(
                    "Select a clip in the queue to edit marks, speed, and fades."
                )
                self.in_entry.set_text("")
                self.out_entry.set_text("")
                self.mute_row.set_active(False)
                self.rotate_row.set_selected(0)
                self.speed_row.set_selected(SPEED_OPTIONS.index(1.0))
                self.fade_in_row.set_value(0)
                self.fade_out_row.set_value(0)
                self.edit_error_label.set_visible(False)
                self.preview.clear()
            else:
                self.edit_clip_label.set_label(job.path.name)
                meta = []
                if job.width and job.height:
                    meta.append(f"{job.width}×{job.height}")
                if job.codec:
                    meta.append(job.codec)
                meta.append(f"duration {format_timecode(job.duration)}")
                self.edit_meta_label.set_label(" · ".join(meta))
                self.in_entry.set_text("" if job.in_s is None else format_timecode(job.in_s))
                self.out_entry.set_text("" if job.out_s is None else format_timecode(job.out_s))
                self.mute_row.set_active(job.mute)
                try:
                    rot_idx = ROTATE_OPTIONS.index(job.rotate)
                except ValueError:
                    rot_idx = 0
                self.rotate_row.set_selected(rot_idx)
                try:
                    spd_idx = SPEED_OPTIONS.index(job.speed)
                except ValueError:
                    spd_idx = SPEED_OPTIONS.index(1.0)
                self.speed_row.set_selected(spd_idx)
                self.fade_in_row.set_value(job.fade_in)
                self.fade_out_row.set_value(job.fade_out)
                err = job.normalize_marks()
                self._set_edit_error(err)
                self.preview.load(job.path, duration=job.duration, codec=job.codec)
                in_s = job.in_s
                if in_s is not None:
                    GLib.idle_add(lambda s=in_s: self.preview.seek_seconds(s) or False)
        finally:
            self._syncing_edit_ui = False
        self._update_sensitivity()

    def _set_edit_error(self, message: str | None) -> None:
        if message:
            self.edit_error_label.set_label(message)
            self.edit_error_label.set_visible(True)
        else:
            self.edit_error_label.set_label("")
            self.edit_error_label.set_visible(False)

    def _commit_edit_panel(self) -> None:
        job = self._selected_job
        if job is None or self._syncing_edit_ui:
            return

        in_text = self.in_entry.get_text().strip()
        out_text = self.out_entry.get_text().strip()

        if in_text:
            parsed = parse_timecode(in_text)
            if parsed is None:
                self._set_edit_error("Invalid In timecode")
                return
            job.in_s = parsed
        else:
            job.in_s = None

        if out_text:
            parsed = parse_timecode(out_text)
            if parsed is None:
                self._set_edit_error("Invalid Out timecode")
                return
            job.out_s = parsed
        else:
            job.out_s = None

        job.mute = self.mute_row.get_active()
        rot_idx = self.rotate_row.get_selected()
        if 0 <= rot_idx < len(ROTATE_OPTIONS):
            job.rotate = ROTATE_OPTIONS[rot_idx]
        else:
            job.rotate = 0
        spd_idx = self.speed_row.get_selected()
        if 0 <= spd_idx < len(SPEED_OPTIONS):
            job.speed = SPEED_OPTIONS[spd_idx]
        else:
            job.speed = 1.0
        job.fade_in = float(self.fade_in_row.get_value())
        job.fade_out = float(self.fade_out_row.get_value())

        err = job.normalize_marks()
        self._set_edit_error(err)
        self._refresh_job_marks_label(job)

    def _on_edit_fields_changed(self, *_args) -> None:
        if self._syncing_edit_ui:
            return
        self._commit_edit_panel()

    def _on_mute_changed(self, *_args) -> None:
        if self._syncing_edit_ui:
            return
        self._commit_edit_panel()

    def _on_rotate_changed(self, *_args) -> None:
        if self._syncing_edit_ui:
            return
        self._commit_edit_panel()

    def _on_speed_changed(self, *_args) -> None:
        if self._syncing_edit_ui:
            return
        self._commit_edit_panel()

    def _on_fade_changed(self, *_args) -> None:
        if self._syncing_edit_ui:
            return
        self._commit_edit_panel()

    def _move_selected(self, delta: int) -> None:
        if self._busy or self._selected_job is None:
            return
        job = self._selected_job
        idx = self.jobs.index(job)
        new_idx = idx + delta
        if new_idx < 0 or new_idx >= len(self.jobs):
            return
        self._commit_edit_panel()
        self.jobs[idx], self.jobs[new_idx] = self.jobs[new_idx], self.jobs[idx]
        # Rebuild list rows in new order while preserving Job widget refs.
        selected = job
        while True:
            row = self.list_box.get_row_at_index(0)
            if row is None:
                break
            self.list_box.remove(row)
        for j in self.jobs:
            if j.row is not None:
                self.list_box.append(j.row)
        if selected.row is not None:
            self.list_box.select_row(selected.row)
        self._update_queue_summary()
        self._update_sensitivity()

    def _on_clear_marks(self, _btn: Gtk.Button) -> None:
        job = self._selected_job
        if job is None or self._busy:
            return
        job.clear_marks()
        self._syncing_edit_ui = True
        try:
            self.in_entry.set_text("")
            self.out_entry.set_text("")
        finally:
            self._syncing_edit_ui = False
        self._set_edit_error(None)
        self._refresh_job_marks_label(job)

    def _apply_mark_from_playhead(self, which: str) -> None:
        job = self._selected_job
        if job is None or self._busy:
            return
        seconds = self.preview.get_position_seconds()
        self._syncing_edit_ui = True
        try:
            if which == "in":
                job.in_s = seconds
                self.in_entry.set_text(format_timecode(seconds))
            else:
                job.out_s = seconds
                self.out_entry.set_text(format_timecode(seconds))
        finally:
            self._syncing_edit_ui = False
        err = job.normalize_marks()
        # If normalize clamped, refresh entries from job.
        self._syncing_edit_ui = True
        try:
            self.in_entry.set_text("" if job.in_s is None else format_timecode(job.in_s))
            self.out_entry.set_text("" if job.out_s is None else format_timecode(job.out_s))
        finally:
            self._syncing_edit_ui = False
        self._set_edit_error(err)
        self._refresh_job_marks_label(job)

    def _on_mark_in(self, _btn: Gtk.Button) -> None:
        self._apply_mark_from_playhead("in")

    def _on_mark_out(self, _btn: Gtk.Button) -> None:
        self._apply_mark_from_playhead("out")

    def _remove_job(self, job: Job) -> None:
        if self._busy:
            return
        was_selected = self._selected_job is job
        if job in self.jobs:
            self.jobs.remove(job)
        if job.row:
            self.list_box.remove(job.row)
        if was_selected:
            self._selected_job = None
            self._load_edit_panel(None)
        self._update_sensitivity()
        self._update_queue_summary()

    def _on_clear_clicked(self, _btn: Gtk.Button) -> None:
        if self._busy:
            return
        self.preview.clear()
        self.jobs.clear()
        while True:
            row = self.list_box.get_row_at_index(0)
            if row is None:
                break
            self.list_box.remove(row)
        self._selected_job = None
        self._load_edit_panel(None)
        self.overall.set_fraction(0)
        self.overall.set_text("Idle")
        self.stats_label.set_visible(False)
        self._update_sensitivity()
        self._update_queue_summary()

    def _on_cancel_clicked(self, _btn: Gtk.Button) -> None:
        self._worker.cancel()

    def _on_convert_clicked(self, _btn: Gtk.Button) -> None:
        if self._busy or not self.jobs:
            return
        if not which_ffmpeg():
            self._toast("ffmpeg is required")
            return
        self._commit_edit_panel()
        for job in self.jobs:
            err = job.normalize_marks()
            if err:
                self._toast(f"{job.path.name}: {err}")
                if job.row:
                    self.list_box.select_row(job.row)
                return
        join = self.join_row.get_active()
        if join and len(self.jobs) < 2:
            self._toast("Join needs at least 2 clips in the queue")
            return
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._busy = True
        self._update_sensitivity()
        preset = self._selected_preset()
        tonemap = self.tonemap_row.get_active()
        jobs = list(self.jobs)
        self.stats_label.set_visible(True)
        self._worker.start(jobs, preset, tonemap, self.output_dir, join=join)

    def _on_convert_finished(
        self,
        cancelled: bool,
        done: int,
        failed: int,
        total: int,
        total_input_bytes: int,
        completed_output_bytes: int,
        elapsed_total: float,
    ) -> None:
        self._busy = False
        if cancelled:
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
