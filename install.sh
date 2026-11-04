#!/bin/sh
# Link tftbridge.py into Klipper's extras folder and keep Klipper's git clean.
# Usage: ./install.sh [klipper_dir]   (default ~/klipper)
set -e
KLIPPER_DIR="${1:-$HOME/klipper}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ln -sf "$HERE/tftbridge.py" "$KLIPPER_DIR/klippy/extras/tftbridge.py"
grep -qxF 'klippy/extras/tftbridge.py' "$KLIPPER_DIR/.git/info/exclude" 2>/dev/null \
  || echo 'klippy/extras/tftbridge.py' >> "$KLIPPER_DIR/.git/info/exclude"

# Ensure user is in netdev group for nmcli access
if getent group netdev >/dev/null 2>&1; then
  if ! id -nG "$USER" | grep -qw "netdev"; then
    echo "Adding $USER to netdev group..."
    sudo usermod -aG netdev "$USER" || true
  fi
fi

# Install polkit rule for passwordless NetworkManager control by netdev group
POLKIT_DIR="/etc/polkit-1/rules.d"
POLKIT_RULE="$POLKIT_DIR/10-klipper-nmcli.rules"
if [ -d "$POLKIT_DIR" ] && [ ! -f "$POLKIT_RULE" ]; then
  echo "Installing polkit rule for nmcli Wi-Fi control..."
  sudo tee "$POLKIT_RULE" >/dev/null <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.isInGroup("netdev")) {
        return polkit.Result.YES;
    }
});
EOF
fi

echo "Linked and permissions configured. Restart Klipper."
