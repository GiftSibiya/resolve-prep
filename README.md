# Resolve Prep

GTK4 / libadwaita app that converts phone clips, screen recordings, and other H.264/H.265 media into **DNxHR `.mov`** intermediates that DaVinci Resolve (Free on Linux) can edit reliably.

## Requirements

- Python 3
- PyGObject with GTK 4 + libadwaita
- `ffmpeg` and `ffprobe` on `PATH`

On Arch / CachyOS:

```bash
sudo pacman -S python-gobject gtk4 libadwaita ffmpeg
```

## Install

```bash
./install.sh
```

This links `~/.local/bin/resolve-prep` and installs a desktop entry (**Resolve Prep** in the app menu).

## Usage

```bash
resolve-prep
# or open files directly
resolve-prep clip.mov recording.mkv
```

1. Add or drop videos
2. Choose an output folder (default `~/Downloads/davinci`)
3. Pick a DNxHR profile — **SQ** is recommended
4. Keep **Tone-map HDR to Rec.709** on for iPhone / HLG / Dolby Vision
5. Convert

Outputs are named `{stem}_dnxhr.mov`.

## Why

Resolve Free on Linux often cannot decode typical delivery codecs (iPhone HEVC/HDR, OBS H.264 in MKV, etc.). DNxHR in a QuickTime container with PCM audio is the usual edit intermediate.

## License

MIT
