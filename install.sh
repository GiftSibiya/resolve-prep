#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR_SHARE="${HOME}/.local/share/applications"

mkdir -p "$BIN_DIR" "$APP_DIR_SHARE"
chmod +x "$APP_DIR/resolve-prep.py"

ln -sfn "$APP_DIR/resolve-prep.py" "$BIN_DIR/resolve-prep"
ln -sfn "$APP_DIR/resolve-prep.py" "$BIN_DIR/vidprep"
sed "s|^Exec=.*|Exec=${APP_DIR}/resolve-prep.py %F|" \
  "$APP_DIR/resolve-prep.desktop" > "$APP_DIR_SHARE/vidprep.desktop"
# Keep legacy desktop id for people who already pinned Resolve Prep
sed "s|^Exec=.*|Exec=${APP_DIR}/resolve-prep.py %F|; s|^Name=.*|Name=VidPrep|" \
  "$APP_DIR/resolve-prep.desktop" > "$APP_DIR_SHARE/resolve-prep.desktop"

update-desktop-database "$APP_DIR_SHARE" 2>/dev/null || true
echo "Installed: VidPrep"
echo "  launcher: $BIN_DIR/vidprep  (also: resolve-prep)"
echo "  menu:     VidPrep"
echo "Run: vidprep"
