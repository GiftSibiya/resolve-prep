# VidPrep (resolve-prep)

GTK4 / libadwaita utility that converts videos for **iPhone playback**, **DaVinci Resolve** editing, **ProRes** handoff, and **web / YouTube** sharing — with per-clip **trim / mute / rotate / speed / fades**, queue reorder, and optional **join into one file**.

> The installable command and repo folder are still named `resolve-prep`; the app displays as **VidPrep**.

## Requirements

- Python 3
- PyGObject with GTK 4 + libadwaita
- `ffmpeg` and `ffprobe` on `PATH`
- For clip preview: GStreamer + GTK4 plugin (optional)

On Arch / CachyOS:

```bash
sudo pacman -S python-gobject gtk4 libadwaita ffmpeg \
  gst-plugins-base gst-plugins-good gst-libav gst-plugin-gtk4
```

Without the GStreamer packages, Convert and In/Out timecode fields still work; the preview pane shows a fallback message.

Clips GStreamer can't decode (e.g. Samsung APV, some ProRes/DNxHR) are previewed through a low-res H.264 proxy that ffmpeg builds on first load and caches in `~/.cache/vidprep/proxies/`.

## Install

```bash
./install.sh
```

Links `~/.local/bin/vidprep` and `resolve-prep`, and installs a **VidPrep** desktop entry.

## Usage

```bash
vidprep
# or open files directly
resolve-prep clip.mov export.mp4
```

1. Add or drop videos into the **Queue**
2. **Move up / down** to set timeline order
3. Select a clip — preview/scrub, **Mark In/Out**, mute, rotate, speed, fades
4. Pick **Target** + **Preset** and an output folder
5. Optionally enable **Join queue into one file**
6. **Convert** — per-clip export, or one joined file; live speed / ETA / size stats

Outputs are named `{stem}_{preset}{ext}`, or `{stem}_joined_{preset}{ext}` when joining.

### Edit tips

| Action | Notes |
|--------|--------|
| Mark In / Out | From playhead, or type `M:SS.mmm` / `H:MM:SS.mmm` |
| Clear marks | Resets In/Out for the selected clip only |
| Mute | Drops audio on solo export; join keeps a silent track so concat stays aligned |
| Rotate / Speed / Fades | Applied per clip before export (or before join segments) |
| Move up / down | Queue order = join timeline order |
| Join | Encodes each clip (normalized to first clip’s size @ 30fps) then concatenates |
| Preview fallback | APV / DNxHR / ProRes often won’t preview — marks still work via ffprobe duration |

Crop UI, text/watermark overlays, and waveform thumbnails are not included yet.

### Common recipes

- **Resolve final → iPhone:** Target *iPhone*, Preset *H.264 — Most compatible*, trim as needed  
- **Phone/OBS → Resolve Free:** Target *DaVinci Resolve*, Preset *DNxHR SQ*  
- **Share online:** Target *Web / YouTube*, Preset *H.264 — YouTube / web*

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

## Project layout

```
resolve-prep.py          # thin launcher (install target)
vidprep/
  app.py                 # Adw.Application entry
  window.py              # main UI
  ui_preview.py          # Gtk.Video preview + scrub / Mark In-Out
  worker.py              # ffmpeg job runner
  ffmpeg_ops.py          # probe / build_ffmpeg_cmd + EditOptions
  presets.py             # targets + export presets
  models.py              # Job dataclass
  util.py                # formatting helpers
```

## License

MIT
