#!/usr/bin/env bash
# Dev-style install for Fedora: a venv with PySide6 and the rest from PyPI, an editable
# install, `lumina` on PATH, and a launcher entry with the app's icon. Safe to run again.
#
#   ./install.sh
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

bin_dir="$HOME/.local/bin"
apps_dir="$HOME/.local/share/applications"
icons_dir="$HOME/.local/share/icons/hicolor/scalable/apps"

# Metadata comes from exiftool; without it photos are listed but their details not read
if ! command -v exiftool >/dev/null; then
    echo "Installing exiftool with dnf"
    sudo dnf install -y perl-Image-ExifTool
fi

# PyPI's PySide6, not the distribution's: the two must not meet in one environment
# (see CLAUDE.md), so this venv does not see the system's site packages
if command -v uv >/dev/null; then
    uv venv --python python3 --allow-existing .venv
    uv pip install --python .venv/bin/python -e .
else
    [[ -x .venv/bin/python ]] || python3 -m venv .venv
    .venv/bin/python -m pip install -e .
fi

.venv/bin/python -c "import PySide6, rawpy, onnxruntime, pillow_heif" ||
    { echo "PySide6, rawpy, onnxruntime or pillow_heif is not importable from the venv" >&2; exit 1; }
# iPhone photos are HEVC: without a decoder in libheif the .heic files are listed but not shown
.venv/bin/python -c "import sys, pillow_heif; sys.exit(not pillow_heif.libheif_info()['decoders'])" ||
    echo "warning: libheif has no HEVC decoder, so .heic photos from phones will not open" >&2

mkdir -p "$bin_dir"
ln -sf "$PWD/.venv/bin/lumina" "$bin_dir/lumina"
install -Dm644 data/lumina.desktop "$apps_dir/lumina.desktop"
# The app's icon, under the app's name: the launcher entry and the windows ask the icon
# theme for it, and fall back to the theme's camera without it
install -Dm644 icons/lumina.svg "$icons_dir/lumina.svg"
command -v gtk-update-icon-cache >/dev/null &&
    gtk-update-icon-cache -q -t -f "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
command -v update-desktop-database >/dev/null && update-desktop-database "$apps_dir"

echo "Installed. Start Lumina from the launcher, or run: lumina [FOLDER]"
case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *) echo "Note: $bin_dir is not on your PATH" ;;
esac
