#!/bin/sh
# Link tftbridge.py into Klipper's extras folder and keep Klipper's git clean.
# Usage: ./install.sh [klipper_dir]   (default ~/klipper)
set -e
KLIPPER_DIR="${1:-$HOME/klipper}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ln -sf "$HERE/tftbridge.py" "$KLIPPER_DIR/klippy/extras/tftbridge.py"
grep -qxF 'klippy/extras/tftbridge.py' "$KLIPPER_DIR/.git/info/exclude" 2>/dev/null \
  || echo 'klippy/extras/tftbridge.py' >> "$KLIPPER_DIR/.git/info/exclude"
echo "Linked. Restart Klipper."
