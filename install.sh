#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
APP_DIR_SHARE="${HOME}/.local/share/applications"

mkdir -p "$BIN_DIR" "$APP_DIR_SHARE"
chmod +x "$APP_DIR/resolve-prep.py"

ln -sfn "$APP_DIR/resolve-prep.py" "$BIN_DIR/resolve-prep"
sed "s|^Exec=.*|Exec=${APP_DIR}/resolve-prep.py %F|" \
  "$APP_DIR/resolve-prep.desktop" > "$APP_DIR_SHARE/resolve-prep.desktop"

update-desktop-database "$APP_DIR_SHARE" 2>/dev/null || true
echo "Installed: resolve-prep"
echo "  launcher: $BIN_DIR/resolve-prep"
echo "  menu:     Resolve Prep"
echo "Run: resolve-prep"
