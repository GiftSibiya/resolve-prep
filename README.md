# VidPrep (resolve-prep)

GTK4 / libadwaita utility that converts videos for **iPhone playback**, **DaVinci Resolve** editing, **ProRes** handoff, and **web / YouTube** sharing.

> The installable command and repo folder are still named `resolve-prep`; the app displays as **VidPrep**.

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

Links `~/.local/bin/vidprep` and `resolve-prep`, and installs a **VidPrep** desktop entry.

## Usage

```bash
vidprep
# or
resolve-prep clip.mov export.mp4
```

1. Add or drop videos
2. Pick a **Target** (iPhone, Resolve, ProRes, Web)
3. Pick a **Preset** for that target
4. Choose an output folder (defaults follow the target)
5. Convert — live speed / ETA / size stats while it runs

Outputs are named `{stem}_{preset}{ext}`, e.g. `hoodwarefare_iphone_h264.mp4`.

## Targets & presets

### iPhone / iOS playback

| Preset | Notes |
|--------|-------|
| **H.264 — Most compatible** | Default. MP4 + AAC + faststart. Use for Resolve APV/FLAC finals. |
| H.264 — Smaller file | Same compatibility, higher CRF |
| HEVC — Smaller | Newer iPhones; tagged `hvc1` for Apple |

### DaVinci Resolve (edit)

| Preset | Notes |
|--------|-------|
| DNxHR LB / SQ / HQ / HQX | Edit intermediates for Resolve Free & Studio (SQ recommended) |
| GoPro CineForm | Fast 10-bit alternative |

### ProRes (Studio / Mac)

| Preset | Notes |
|--------|-------|
| ProRes 422 / 422 HQ | For macOS / Studio — Resolve Free on Linux usually cannot decode these |

### Web / YouTube / share

| Preset | Notes |
|--------|-------|
| H.264 — YouTube / web | Balanced upload MP4 |
| H.264 — Quick review proxy | Faster, smaller review encodes |

## Why

- Resolve Free on Linux often cannot decode iPhone HEVC/HDR or OBS H.264 well → use **DNxHR** before editing.
- Resolve delivery codecs like **APV + FLAC** will not play on iPhone → use **iPhone · H.264**.

## License

MIT
