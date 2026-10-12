from __future__ import annotations

import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gio", "2.0")

from gi.repository import Adw, Gio  # noqa: E402

from vidprep import APP_ID
from vidprep.window import VidPrepWindow


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
