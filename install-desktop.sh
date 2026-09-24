#!/usr/bin/env bash
#
# install-desktop.sh - Put "Media Sorter" and "Stop Media Sorter" launchers in
# the desktop's application menu (any freedesktop DE: GNOME, KDE, XFCE, ...).
# Uses this checkout's absolute path, so re-run it if you move the folder.
#
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"

PY="$HERE/venv/bin/python"
[ -x "$PY" ] || PY="python3"

cat > "$APPS/media-sorter.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Media Sorter
GenericName=Image and Video Sorter
Comment=Browse images and videos as thumbnails and file them into folders with one key
Exec=env PYTHON=$PY $HERE/start.sh --background
Path=$HERE
Icon=$HERE/static/favicon.svg
Terminal=false
Categories=Utility;Graphics;Viewer;AudioVideo;
StartupNotify=false
EOF

cat > "$APPS/stop-media-sorter.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=Stop Media Sorter
Comment=Stop the background Media Sorter server
Exec=$HERE/start.sh --stop
Path=$HERE
Icon=$HERE/static/favicon.svg
Terminal=false
Categories=Utility;
StartupNotify=false
EOF

chmod +x "$APPS/media-sorter.desktop" "$APPS/stop-media-sorter.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true

echo "Installed:"
echo "  $APPS/media-sorter.desktop"
echo "  $APPS/stop-media-sorter.desktop"
echo "Look for 'Media Sorter' in your application menu."
